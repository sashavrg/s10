#!/usr/bin/env python3
"""Engagement judge (#1e-narrow) — replaces the failed v2 token-overlap engagement proxy.

Semantic per-row judgment: did the assistant genuinely ENGAGE the injected (HIGH) /
advertised (MODERATE) topic's subject, vs coincidental word overlap? Runs ONLY in the
offline re-scorer (rescore_outcomes.py); the live SessionEnd writer is untouched.

Design + pre-committed validation bar: docs/superpowers/specs/2026-07-02-engagement-judge-design.md.
Transport mirrors reranker.py (keep_alive, temperature 0, fail-open). Null-bias is
structural (only an exact "ENGAGED: yes" parses True), prompted (NULL_BIAS_CLAUSE,
test-pinned), and behavioral (dev harness reports FP-on-not-engaged per iteration).

JUDGE_VERSION covers prompt + evidence-builder + model: bump it on ANY semantic change
and the nightly re-judges (bounded by KB_JUDGE_MAX_CALLS per run).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

# Round 2: the rubric judge. Model is claude:sonnet via the cloud driver (dev/gate);
# production backend wiring lands only on a gate pass and MUST match this version's model.
JUDGE_VERSION = 'ej9-claude-sonnet'

OLLAMA_URL = os.environ.get('KB_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
ENGAGE_MODEL = os.environ.get('KB_ENGAGE_MODEL', 'qwen2.5:7b-instruct-q4_K_M')
ENGAGE_TIMEOUT = float(os.environ.get('KB_ENGAGE_TIMEOUT', '60'))
_RATIONALE_CAP = 200

# ej6: the judge quotes the OPERATOR-SIGNED rubric (docs/engagement-rubric.md) verbatim.
# The constants below are test-pinned against that file (tests/test_engagement_judge.py):
# any rubric edit fails the pin -> requires a JUDGE_VERSION bump + a fresh gate set.
NULL_BIAS_CLAUSE = """- **Null-bias tiebreak, strengthened:** R1–R3 must be honestly attempted BEFORE any
  tiebreak — a session that works the subject under any of them is engaged. "Genuinely
  torn" means torn after applying R1–R7, not uncertain on first read. The tiebreak is a
  last resort, never an exit from hard R2/R5 calls (the prior judge failed by being
  systematically stricter than the operator; a tiebreak reached early re-creates that
  failure under rubric cover)."""

RUBRIC_IDENTITY_NOTE = """- When the identity is stale or mismatched to the slug (pre-flag it per §2.8 of the judge
  spec), judge the slug's subject anyway — the identity never overrides the name (R1)."""

RUBRIC_TEXT = """## The question being judged

Per (session, moment, exactly ONE topic slug): **did the assistant genuinely ENGAGE the
topic's SUBJECT?**

- **HIGH rows** (facts were injected): engaged = the assistant **used the injected facts OR
  genuinely worked their subject**.
- **MODERATE rows** (nothing injected — control arm): engaged = the topic's subject
  **genuinely came up / was worked on its own**.

The topic's **name** (the de-slugged slug) names the subject. The identity lines shown with
it (injected facts / topic-page excerpts) are a **pointer to the subject, not its
definition** — they are often narrower, staler, or one old session's rendering.

**Scope: the MOMENT, not the whole session.** The judged object is the moment — the
triggering prompt, the assistant's reply, and the work that flows from that reply. The same
slug can be engaged at one moment and not-engaged at another within a single session. Later
excerpts corroborate the moment's work; they are NOT independent evidence of engagement
elsewhere in the session.
> Worked: `acme-widgets`, one session, two moments — the adapter-design moment:
> **ENGAGED** (dev r6); the honesty-exchange moment about a product link: **NOT ENGAGED**
> (dev r2).

## Rules

**R1 — Judge at the slug's subject grain, not the identity text's grain.**
If the session works the subject the *name* points at, it is engaged even when the
identity's specifics never appear.
> Worked: `config-api-migration` — the session migrated a client onto the config-driven
> system; the injected facts were doc-status meta-facts. **ENGAGED.** (gate r1)
> Worked: `time-estimate-mechanics` — the session designed estimate-vs-actual tooling; the
> facts were ms-conversion mechanics. **ENGAGED.** (gate r9)

**R2 — Concrete instances of the subject COUNT.**
Doing the kind of work the topic is about is engagement; the topic's name need never be
spoken.
> Worked: `product-regeneration` — a live forced-config regen of one product IS product
> regeneration. **ENGAGED.** (dev r13)
> Worked: `supply-chain-subtask` — creating a supply-chain subtask under the go-live parent
> is this subject being worked, even though it was a different ticket than the identity's.
> **ENGAGED.** (gate r5, r35)

**R3 — Correct APPLICATION of the content counts — including applying it to rule it out.**
If the assistant uses the topic's content to reason, scope, or disambiguate — even
concluding "this doesn't apply here" — that is engagement: the injection did its job.
> Worked: `ready-for-review-exclusion` — "the RFR-excluded idea is for schedulable-hours
> math; this completion metric counts RFR." The fact was applied correctly to separate two
> metrics. **ENGAGED.** (gate r15)
> Contrast: merely counting RFR tickets in a dashboard tally, never touching the exclusion
> idea, is NOT engagement with this topic. (gate r13, r14, r16)

**R4 — Responsiveness is not engagement.**
Answering the user's request well is orthogonal. An excellent reply on a different subject
is NOT engaged.
> Worked: `acme-widgets` facts injected during an honesty exchange about a product link —
> good reply, subject unworked. **NOT ENGAGED.** (dev r2)

**R5 — Word overlap, shared vocabulary, and merely adjacent areas do NOT count.**
Same infra words (gate/log/config) or neighboring work is not the subject.
> Worked: `admission-gate-design` (music-pipeline enqueue gate) injected on LLM-judge-gate
> design prompts — vocabulary collision. **NOT ENGAGED.** (gate r17, r19)
The R5↔R2/R3 boundary: the test is whether the subject is **WORKED**, not whether it is
named or near. Designing regen-automation safety rails inside a broader framework IS working
`api-automation` (gate r24); a passing mention of a component inside unrelated analysis is
not.

**R6 — An explicit dismissal of the injection does not decide the verdict; the session's
subject does.**
"Ignoring the injected memory" with a session whose own subject IS the topic → engaged
(dev r4: the correction-harvesting-workflow session). Dismissing an off-topic injection →
not engaged (dev r6-class misfires).

**R7 — Per-slug discipline.**
Each judgment is about exactly one slug. A sibling or co-advertised topic engaging does not
transfer.
> Worked: `iam` is NOT engaged by embedding-model work, even though sibling `llm-models`
> would be. (dev-era finding, rows 42/46 class)

**R8 — Project-name and catch-all slugs: ubiquity is not engagement.**
When a slug names an entire project, repo, or workspace (e.g. `acme-portal-api`), merely
working WITHIN that project does not engage the topic — otherwise every session engages it
by definition and the verdict carries no information. Engagement requires the session to
work the topic AS A SUBJECT — its architecture, configuration, or behavior as a system —
or to use the injected facts.
> Worked: merging WIP inside the portal-api clone — **NOT ENGAGED** (dev r3).
(Same logic as the retrieval catch-all blocklist: an always-true signal is informationless
— same topic class, same cure.)
R8 applies ONLY when the slug names an entire project, repo, or workspace. It does NOT
extend to systems, features, migrations, or subjects WITHIN a project (e.g.
`config-api-migration` is a subject, not a catch-all — R1 governs it, not R8)."""

_ENGAGED_LINE = re.compile(r'^\s*ENGAGED\s*:\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE)
_WHY_LINE = re.compile(r'^\s*WHY\s*:\s*(.+?)\s*$', re.IGNORECASE | re.MULTILINE)


class EngageError(RuntimeError):
    pass


def _payload(prompt: str) -> dict:
    return {
        'model': ENGAGE_MODEL,
        'prompt': prompt,
        'stream': False,
        'keep_alive': os.environ.get('KB_ENGAGE_KEEPALIVE', '10m'),
        'options': {'temperature': 0.0, 'num_predict': 80, 'num_ctx': 8192},
    }


def _generate(prompt: str) -> str:
    data = json.dumps(_payload(prompt)).encode()
    req = urllib.request.Request(
        f'{OLLAMA_URL}/api/generate',
        data=data, headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=ENGAGE_TIMEOUT) as r:
            return json.loads(r.read()).get('response', '')
    except Exception as e:
        raise EngageError(f'engage generate failed: {e}') from e


def parse_verdict(text: str | None) -> dict | None:
    """Mechanical null-bias boundary (spec §3.1): an `ENGAGED:` line with value
    exactly `yes` (case-insensitive) → True; ANY other value on that line → False;
    no `ENGAGED:` line at all → None (caller keeps the row at scorer_version 2)."""
    m = _ENGAGED_LINE.search(text or '')
    if not m:
        return None
    wm = _WHY_LINE.search(text or '')
    return {
        'engaged': m.group(1).strip().lower() == 'yes',
        'rationale': (wm.group(1).strip() if wm else '')[:_RATIONALE_CAP],
    }


def _build_prompt(ev: dict) -> str:
    subject = (ev.get('topic_slug') or '').replace('-', ' ')
    lines = [
        'You judge whether an AI coding assistant genuinely ENGAGED with a knowledge-base',
        "topic during a work session. Apply the OPERATOR'S RUBRIC below exactly — it is the",
        "ground-truth standard, extracted from the operator's own labeled cases. Where your",
        'instinct and the rubric differ, the rubric wins.',
        '',
        RUBRIC_TEXT,
        '',
        'Operative notes:',
        RUBRIC_IDENTITY_NOTE,
        NULL_BIAS_CLAUSE,
        '',
        '---',
        'Now the case:',
        '',
        f"Topic subject: {subject} (`{ev.get('topic_slug')}`)",
    ]
    if ev['tier'] == 'high':
        lines.append('HIGH row — these topic facts were INJECTED into the assistant\'s '
                     'context at that moment:')
    else:
        lines.append('MODERATE row — NOTHING was injected (control). Identity lines below '
                     'are a pointer to the subject, possibly stale (R1):')
    lines += [f'  - {f}' for f in (ev.get('topic_identity') or [])[:6]]
    lines += ['', 'The user prompt at that moment:', ev.get('trigger_prompt') or '(unknown)',
              '', "The assistant's reply:", ev.get('assistant_reply') or '(unknown)']
    if ev.get('later_snippets'):
        lines += ['', 'Later assistant excerpts that MIGHT relate (recall-biased nomination '
                      '— excerpts about OTHER work do NOT count against engagement):']
        lines += [f'  · {s}' for s in ev['later_snippets']]
    lines += ['',
              'Decision protocol — walk these steps IN ORDER, one short line each, before '
              'answering. Do not write the string "ENGAGED:" anywhere except the final '
              'answer line.',
              "STEP 1 — Name the moment's work in one phrase (the reply and the work "
              'flowing from it).',
              "STEP 2 — Name the slug's subject in one phrase (from the topic NAME; the "
              'identity lines are only a pointer).',
              'STEP 3 — Same subject at R1/R2 grain? A concrete instance counts (R2); the '
              "identity's specifics need not appear (R1); correct application of the "
              'content, even to rule it out, counts (R3).',
              'STEP 4 — Exception checks: R4 (responsive but different subject?), R5 (only '
              'vocabulary/adjacent?), R7 (sibling slug, not this one?), R8 (project-name '
              'slug + merely working within it?).',
              'STEP 5 — Precedent check LAST: if this case matches a worked example or '
              "contrast case in the rubric, your verdict MUST equal that example's verdict "
              '— the example overrides your own reasoning from steps 1-4.',
              '',
              'Then answer in EXACTLY this format (two final lines):',
              'ENGAGED: yes|no',
              'WHY: <one short sentence, naming the rule you applied (e.g. R2) or the '
              'matched example>']
    return '\n'.join(lines)


def judge_engagement(evidence: dict, generate_fn=None) -> dict | None:
    """One judged verdict or None (fail-open: transport error, garbage output).
    None means the caller must leave the row at scorer_version 2 — never fake a 3."""
    fn = generate_fn or _generate
    try:
        return parse_verdict(fn(_build_prompt(evidence)))
    except Exception:
        return None


# --------------------------------------------------------------------- evidence
from pathlib import Path

from outcome_matching import slug_tokens, tokenize

BASE_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_ROOT = Path.home() / '.claude' / 'projects'
ARCHIVE_ROOT = BASE_DIR / 'logs' / 'transcript_archive'
TOPICS_DIR = BASE_DIR / 'compiled' / 'topics'

_MAX_SNIPPETS = 8          # recall-biased: nominate generously, the judge discards
_SNIPPET_WIDTH = 300
_WORD = re.compile(r'[a-z0-9]+')


def resolve_transcript(session_id: str) -> Path | None:
    """Live projects dir first, then the retention-proof archive (2026-07-02)."""
    for root in (TRANSCRIPT_ROOT, ARCHIVE_ROOT):
        hits = sorted(root.glob(f'*/{session_id}.jsonl')) if root.exists() else []
        if hits:
            return hits[0]
    return None


_OVERVIEW_HEADINGS = {'topic overview', 'overview', 'summary'}


def _heading_kind(s: str) -> str | None:
    """Classify a heading line once (F1): 'overview' for an H1 or H2 heading whose
    text — stripped of leading '#'s, lowercased — exactly matches one of
    _OVERVIEW_HEADINGS ('topic overview' / 'overview' / 'summary'); 'key_points' for
    an H2 '## Key points'; 'section' for any other H2 (generic transition, unchanged
    from before); None otherwise — including non-overview H1 lines (e.g. a document
    title), which are not section boundaries, matching pre-fix behavior."""
    if (s.startswith('# ') or s.startswith('## ')) and s.lstrip('#').strip().lower() in _OVERVIEW_HEADINGS:
        return 'overview'
    if s.startswith('## '):
        return 'key_points' if s[3:].strip().lower() == 'key points' else 'section'
    return None


def load_topic_page(slug: str) -> dict:
    """Overview + key points from compiled/topics/<slug>.md (fail-open to empty)."""
    p = TOPICS_DIR / f'{slug}.md'
    out = {'overview': '', 'points': []}
    if not p.exists():
        return out
    body = p.read_text(errors='replace')
    body = body.split('---', 2)[-1] if body.count('---') >= 2 else body
    in_over = in_pts = False
    over: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        kind = _heading_kind(s)
        if kind == 'overview':
            in_over, in_pts = True, False
            continue
        if kind is not None:  # 'key_points' or 'section'
            in_over, in_pts = False, kind == 'key_points'
            continue
        if in_over and s:
            over.append(s)
        elif in_pts and s[:2] in ('- ', '* '):
            out['points'].append(' '.join(s[2:].split()))
    out['overview'] = ' '.join(' '.join(over).split())[:400]
    return out


def _norm(s: str | None) -> str:
    return ' '.join((s or '').split())


def _find_trigger(turns: list[dict], prompt_head: str) -> int | None:
    key = _norm(prompt_head)[:60].lower()
    if not key:
        return None
    for i, t in enumerate(turns):
        if t['role'] == 'user' and key in _norm(t['text']).lower():
            return i
    return None


def _topic_terms(inj_row: dict, topic_page: dict | None) -> set:
    terms = slug_tokens(inj_row.get('injected'))
    for f in (inj_row.get('injected_facts') or []):
        terms |= tokenize(f)
    if topic_page:
        for pt in topic_page.get('points', []):
            terms |= tokenize(pt)
    return terms


def build_evidence(inj_row: dict, turns: list[dict], topic_page: dict | None) -> dict:
    tier = inj_row.get('tier')
    slug = inj_row.get('injected')
    if tier == 'high':
        identity = list(inj_row.get('injected_facts') or [])
    else:
        tp = topic_page or {}
        identity = ([tp['overview']] if tp.get('overview') else []) + tp.get('points', [])[:5]
    ti = _find_trigger(turns, inj_row.get('prompt_head', ''))
    trigger = reply = ''
    if ti is not None:
        trigger = _norm(turns[ti]['text'])[:700]
        for j in range(ti + 1, len(turns)):
            if turns[j]['role'] == 'assistant':
                reply = _norm(turns[j]['text'])[:1400]
                break
    terms = _topic_terms(inj_row, topic_page)
    snippets: list[str] = []
    start = (ti + 2) if ti is not None else 0
    for t in turns[start:]:
        if len(snippets) >= _MAX_SNIPPETS:
            break
        if t['role'] != 'assistant':
            continue
        low = t['text'].lower()
        hit = terms & set(_WORD.findall(low))
        if not hit:                       # >=1 hit nominates (recall-biased; judge decides)
            continue
        m = re.search(rf"\b{re.escape(sorted(hit)[0])}\b", low)
        i = m.start() if m else 0
        s, e = max(0, i - _SNIPPET_WIDTH // 2), min(len(t['text']), i + _SNIPPET_WIDTH // 2)
        snippets.append(('…' if s else '') + _norm(t['text'][s:e]) + ('…' if e < len(t['text']) else ''))
    return {
        'tier': tier, 'topic_slug': slug, 'topic_identity': identity,
        'trigger_prompt': trigger, 'assistant_reply': reply,
        'later_snippets': snippets, 'n_snippets': len(snippets),
        'nomination_empty': not snippets, 'anchored': ti is not None,
    }

# ------------------------------------------------------------------ dev harness
import argparse

FIXTURE_PATH = BASE_DIR / 'evals' / 'fixtures' / 'engagement_gate_2026-07-02.jsonl'
INJECTION_LOG = BASE_DIR / 'logs' / 'memory_injection.jsonl'
# Frozen with the fixture (see its header) — the dev read is against THAT snapshot,
# not today's population. The GATE harness will carry its own sizes.
POP_SIZES = {'high': 8, 'disagree': 47, 'agree_engaged': 96, 'agree_not': 16}


def _pr(tp: int, fp: int, fn: int) -> dict:
    return {'tp': tp, 'fp': fp, 'fn': fn,
            'precision': round(tp / (tp + fp), 4) if tp + fp else None,
            'recall': round(tp / (tp + fn), 4) if tp + fn else None}


def score_dev(results: list[dict]) -> dict:
    """Pure scorer for judge-vs-fixture results. Unjudged (None) rows are counted
    and EXCLUDED from P/R (they stay v2 in production — a separate failure axis)."""
    strata: dict[str, dict] = {}
    unjudged = 0
    for r in results:
        s = strata.setdefault(r['stratum'], {'n': 0, 'judged': 0, 'tp': 0, 'fp': 0, 'fn': 0})
        s['n'] += 1
        if r['judged_engaged'] is None:
            unjudged += 1
            continue
        s['judged'] += 1
        if r['judged_engaged'] and r['label_engaged']:
            s['tp'] += 1
        elif r['judged_engaged'] and not r['label_engaged']:
            s['fp'] += 1
        elif not r['judged_engaged'] and r['label_engaged']:
            s['fn'] += 1
    by = {k: {**v, **_pr(v['tp'], v['fp'], v['fn'])} for k, v in sorted(strata.items())}
    tot = {x: sum(v[x] for v in strata.values()) for x in ('tp', 'fp', 'fn')}
    wtp = wfp = wfn = 0.0
    for k, v in strata.items():
        if not v['judged']:
            continue
        w = POP_SIZES.get(k, v['n']) / v['judged']
        wtp += v['tp'] * w; wfp += v['fp'] * w; wfn += v['fn'] * w
    return {
        'by_stratum': by,
        'pooled': _pr(tot['tp'], tot['fp'], tot['fn']),
        'pop_weighted': {
            'precision': round(wtp / (wtp + wfp), 4) if wtp + wfp else None,
            'recall': round(wtp / (wtp + wfn), 4) if wtp + wfn else None},
        'fp_not_engaged': tot['fp'],
        'unjudged': unjudged,
    }


def _load_fixture(path: Path = FIXTURE_PATH) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        rows.append(json.loads(line))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description='Engagement judge dev harness (#1e).')
    ap.add_argument('--dev', action='store_true',
                    help='judge the 48-row fixture with real Ollama and score vs labels')
    args = ap.parse_args()
    if not args.dev:
        ap.print_help()
        return
    from injection_outcome import parse_turns          # stdlib-only sibling
    inj_rows = []
    for line in INJECTION_LOG.read_text(errors='replace').splitlines():
        try:
            inj_rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    inj_by_key = {(r.get('session_id'), r.get('ts')): r for r in inj_rows}
    results, misses, turns_cache = [], [], {}
    for fx in _load_fixture():
        inj = inj_by_key.get((fx['session_id'], fx['ts']))
        sid = fx['session_id']
        if sid not in turns_cache:
            tp = resolve_transcript(sid)
            turns_cache[sid] = parse_turns(tp) if tp else []
        verdict = None
        ev = None
        if inj and turns_cache[sid]:
            # fixture names the specific slug under judgment; the inj row for
            # moderate has injected=None — evidence keys on the fixture's slug.
            inj_eff = {**inj, 'injected': fx['injected'], 'tier': fx['tier']}
            page = None if fx['tier'] == 'high' else load_topic_page(fx['injected'])
            ev = build_evidence(inj_eff, turns_cache[sid], page)
            verdict = judge_engagement(ev)
        judged = verdict['engaged'] if verdict else None
        results.append({'stratum': fx['stratum'], 'label_engaged': fx['label_engaged'],
                        'judged_engaged': judged})
        if judged is not None and judged != fx['label_engaged']:
            # evidence meta (n_snippets, reply length, anchored) so the iteration-2
            # owner can tell evidence starvation from prompt strictness at a glance.
            misses.append((fx['stratum'], fx['injected'], judged,
                           (verdict or {}).get('rationale', ''),
                           ev['n_snippets'] if ev else 0,
                           len(ev['assistant_reply']) if ev else 0,
                           ev['anchored'] if ev else False))
    s = score_dev(results)
    print(f"dev [{JUDGE_VERSION}] vs {FIXTURE_PATH.name}")
    for k, v in s['by_stratum'].items():
        print(f"  {k:>14}: n={v['n']} judged={v['judged']} "
              f"P={v['precision']} R={v['recall']} (tp{v['tp']}/fp{v['fp']}/fn{v['fn']})")
    print(f"  pooled: {s['pooled']} · pop-weighted: {s['pop_weighted']}")
    print(f"  FP-on-not-engaged: {s['fp_not_engaged']} · unjudged(stay v2): {s['unjudged']}")
    for m in misses:
        print(f"  MISS [{m[0]}] {m[1]}: judged={m[2]} — {m[3]} "
              f"(n_snippets={m[4]} reply_len={m[5]} anchored={m[6]})")


if __name__ == '__main__':
    main()
