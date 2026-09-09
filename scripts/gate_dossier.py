#!/usr/bin/env python3
"""Blind gate-labeling dossier generator (#1e round 2, ej9) — implements A4 of
the operator-signed addendum (docs/superpowers/specs/2026-07-20-ej9-gate-read-addendum.md).

`make`  — deterministic, no LLM anywhere: select the judgeable fresh HIGH gate
          set (the tripwire definition + transcript-resolvable + injection-row
          join, addendum A2), build FULLER labeler-side evidence than the judge
          sees (wider excerpts, longer reply — asymmetry intentional, round-2
          freeze record), render the per-slug blind dossier to
          evals/candidates/. Prints aggregate counts ONLY, never row content.
`parse` — read the operator's filled labels back, report incomplete rows by
          ordinal, and write the label rows jsonl for the gate read.
`count` — the same selection, counts only (judgeable total on stdout, breakdown
          on stderr, nothing written). This is what `gate_accrual_check.sh`
          gates on: accrual must track the JUDGEABLE set, not raw fresh HIGH.

BLINDNESS CONTRACT (test-pinned): the dossier must never contain any scorer or
judge output — no `engaged_in_assistant`, `engaged_matched`, `judge_*`, no
scores. Labels are recorded BEFORE the judge runs (A4 operations order).

`gate_verdict` encodes the amended A3 decision rule (stage-1 bars 0.80/0.70
with band floors 0.70/0.60; stage-2 bars 0.82/0.72, pass/fail only).

The output files are DATA (evals/candidates/ is gitignored) — never commit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import engagement_judge as ej
from injection_outcome import parse_turns

BASE_DIR = Path(__file__).resolve().parent.parent
V1_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
V2_PATH = BASE_DIR / 'logs' / 'injection_outcomes_rescored.jsonl'
CAL_PATH = BASE_DIR / 'evals' / 'fixtures' / 'engagement_calibration_2026-07-03.jsonl'
INJECTION_LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
OUT_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-dossier.md'
LABELS_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-labels.jsonl'

def _target() -> int:
    """Full-power gate threshold; KB_GATE_HIGH_TARGET overrides (the accrual
    tripwire reads the same env var, so the two can never disagree)."""
    try:
        return int(os.environ.get('KB_GATE_HIGH_TARGET') or 35)
    except ValueError:
        return 35


TARGET = _target()

# Labeler-side evidence is deliberately FULLER than the judge's (300-char/8
# snippets, 1400-char reply) — round-2 freeze record.
SNIPPET_WIDTH = 600
MAX_SNIPPETS = 12
TRIGGER_CAP = 1500
REPLY_CAP = 3000


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for line in path.read_text(errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _key(r: dict) -> tuple:
    return (r.get('session_id'), r.get('ts'), r.get('injected'))


def select_gate_rows(v1_rows: list[dict], v2_rows: list[dict], cal_keys: set,
                     inj_by_key: dict, resolve_fn) -> tuple[list[dict], dict]:
    """The gate set: fresh HIGH keys (union of both outcome files, key-deduped,
    calibration excluded — the gate_accrual_check definition) that are also
    JUDGEABLE (transcript resolvable + injection-log row present, A2)."""
    merged: dict[tuple, dict] = {}
    for r in list(v1_rows) + list(v2_rows):
        if r.get('tier') != 'high' or not r.get('session_id'):
            continue
        k = _key(r)
        if k in cal_keys:
            continue
        merged.setdefault(k, r)
    stats = {'counted': len(merged), 'judgeable': 0,
             'no_transcript': 0, 'no_injection_row': 0}
    rows: list[dict] = []
    for k in sorted(merged, key=lambda k: (k[1] or '', k[2] or '')):
        if resolve_fn(k[0]) is None:
            stats['no_transcript'] += 1
            continue
        if (k[0], k[1]) not in inj_by_key:
            stats['no_injection_row'] += 1
            continue
        rows.append(merged[k])
    stats['judgeable'] = len(rows)
    return rows, stats


def build_labeler_evidence(inj_row: dict, turns: list[dict]) -> dict:
    """Same anchoring and nomination as the judge's build_evidence, wider caps.
    HIGH rows only: the identity is the injected facts, no topic page needed."""
    identity = list(inj_row.get('injected_facts') or [])
    ti = ej._find_trigger(turns, inj_row.get('prompt_head', ''))
    trigger = reply = ''
    if ti is not None:
        trigger = ej._norm(turns[ti]['text'])[:TRIGGER_CAP]
        for j in range(ti + 1, len(turns)):
            if turns[j]['role'] == 'assistant':
                reply = ej._norm(turns[j]['text'])[:REPLY_CAP]
                break
    terms = ej._topic_terms(inj_row, None)
    snippets: list[str] = []
    start = (ti + 2) if ti is not None else 0
    for t in turns[start:]:
        if len(snippets) >= MAX_SNIPPETS:
            break
        if t['role'] != 'assistant':
            continue
        low = t['text'].lower()
        hit = terms & set(ej._WORD.findall(low))
        if not hit:
            continue
        m = re.search(rf"\b{re.escape(sorted(hit)[0])}\b", low)
        i = m.start() if m else 0
        s, e = max(0, i - SNIPPET_WIDTH // 2), min(len(t['text']), i + SNIPPET_WIDTH // 2)
        snippets.append(('…' if s else '') + ej._norm(t['text'][s:e])
                        + ('…' if e < len(t['text']) else ''))
    return {'tier': 'high', 'topic_slug': inj_row.get('injected'),
            'topic_identity': identity, 'trigger_prompt': trigger,
            'assistant_reply': reply, 'later_snippets': snippets,
            'anchored': ti is not None}


_HEADER = """\
# EJ9 ENGAGEMENT GATE — blind labeling dossier
#
# Judge FROZEN (ej9-claude-sonnet, model pinned claude-sonnet-5) BEFORE this
# file was generated. PROTOCOL (addendum A4, operator-signed 2026-07-20):
#  - PER-SLUG: each row asks about EXACTLY ONE topic slug. Not "any topic".
#  - For each row, fill THREE fields, in order, BEFORE any verdict exists:
#      identity_matches_subject: yes|no — do the shown facts coherently
#        represent this slug's actual subject? (Pre-flagging staleness NOW is
#        what makes a later exclusion legitimate — the timing is the guard.)
#      label_engaged: yes|no — did the assistant genuinely work THIS topic's
#        subject at that moment (used the injected facts OR worked their
#        subject)? Not word overlap; not "answered the user well".
#      label_corrected: yes|no — was the topic's CONTENT wrong/contested by
#        you that session? (Almost always no.)
#  - The evidence below is FULLER than what the judge receives (wider
#    excerpts, longer reply — asymmetry intentional, round-2 freeze record).
#    If it still feels thin or misleading, OPEN THE TRANSCRIPT (path per row)
#    and/or the full topic page (path per row).
"""


def render_dossier(entries: list[dict]) -> str:
    lines = [_HEADER]
    for i, e in enumerate(entries, 1):
        r, ev = e['row'], e['evidence']
        slug = r.get('injected')
        lines += [
            f"## Row {i} — `{slug}`  [HIGH]",
            f"_session `{r.get('session_id')}` · ts {r.get('ts')} · "
            f"transcript: {e['transcript']}_",
            f"_full topic page: compiled/topics/{slug}.md_",
            '',
            '**Injected facts (this is the identity you are rating):**',
        ]
        lines += [f'  - {f}' for f in ev.get('topic_identity') or []]
        lines += ['', f"**Trigger prompt:** {ev.get('trigger_prompt') or '(unanchored)'}",
                  '', f"**Assistant reply:** {ev.get('assistant_reply') or '(unanchored)'}"]
        if ev.get('later_snippets'):
            lines.append('**Later mentions:**')
            lines += [f'  · {s}' for s in ev['later_snippets']]
        lines += ['', '- identity_matches_subject: ?', '- label_engaged: ?',
                  '- label_corrected: ?', '', '---', '']
    return '\n'.join(lines)


_ROW_RE = re.compile(r'^## Row (\d+) — `([^`]+)`')
_SESSION_RE = re.compile(r'^_session `([^`]+)` · ts (\S+) ·')
_FIELDS = ('identity_matches_subject', 'label_engaged', 'label_corrected')


def parse_dossier(md: str) -> tuple[list[dict], list[int]]:
    """Filled labels back out. A row missing any of the three fields (still
    `?` or malformed) is reported in `incomplete` by its ordinal."""
    rows: list[dict] = []
    cur: dict | None = None
    for line in md.splitlines():
        line = line.strip()
        m = _ROW_RE.match(line)
        if m:
            cur = {'ordinal': int(m.group(1)), 'injected': m.group(2), 'tier': 'high'}
            rows.append(cur)
            continue
        if cur is None:
            continue
        m = _SESSION_RE.match(line)
        if m:
            cur['session_id'], cur['ts'] = m.group(1), m.group(2)
            continue
        for f in _FIELDS:
            if line.startswith(f'- {f}:'):
                v = line.split(':', 1)[1].strip().lower()
                if v in ('yes', 'no'):
                    cur[f] = (v == 'yes')
    labeled, incomplete = [], []
    for r in rows:
        if all(f in r for f in _FIELDS):
            labeled.append({k: r[k] for k in
                            ('session_id', 'ts', 'injected', 'tier') + _FIELDS})
        else:
            incomplete.append(r['ordinal'])
    return labeled, incomplete


def gate_verdict(p: float, r: float, stage: int = 1) -> str:
    """Amended A3 decision rule (point estimates). Stage 1: pass at 0.80/0.70,
    hard fail below either band floor (0.70/0.60), else expand. Stage 2 (the
    one re-read at n=50): pass at the adjusted 0.82/0.72 bars, else fail."""
    if stage == 2:
        return 'pass' if p >= 0.82 and r >= 0.72 else 'fail'
    if p >= 0.80 and r >= 0.70:
        return 'pass'
    if p < 0.70 or r < 0.60:
        return 'fail'
    return 'expand'


def _select() -> tuple[list[dict], dict, dict]:
    """The A2 gate set + its counts + the injection-log index (shared by make/count)."""
    cal_keys = {_key(r) for r in _read_jsonl(CAL_PATH)}
    inj_by = {(r.get('session_id'), r.get('ts')): r
              for r in _read_jsonl(INJECTION_LOG_PATH)}
    rows, stats = select_gate_rows(_read_jsonl(V1_PATH), _read_jsonl(V2_PATH),
                                   cal_keys, inj_by, ej.resolve_transcript)
    return rows, stats, inj_by


def count() -> dict:
    """Selection counts only — no rendering, no writes, no row content.

    This is the number the accrual tripwire must gate on: a fresh HIGH row whose
    transcript this machine cannot resolve is unjudgeable (A2) and can never be
    labeled, so counting it would trip the gate under-powered."""
    return _select()[1]


def make() -> dict:
    rows, stats, inj_by = _select()
    entries, turns_cache = [], {}
    for r in rows:
        sid = r['session_id']
        tp = ej.resolve_transcript(sid)
        if tp is None:  # selection guaranteed a transcript; vanishing mid-run is fatal
            raise RuntimeError(f'transcript vanished during make(): {sid}')
        if sid not in turns_cache:
            turns_cache[sid] = parse_turns(tp)
        inj_eff = {**inj_by[(sid, r.get('ts'))],
                   'injected': r.get('injected'), 'tier': 'high'}
        entries.append({'row': r,
                        'evidence': build_labeler_evidence(inj_eff, turns_cache[sid]),
                        'transcript': str(tp)})
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render_dossier(entries))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description='ej9 blind gate dossier (A4).')
    ap.add_argument('cmd', choices=('make', 'parse', 'count'),
                    help="count = judgeable total on stdout (tripwire input), "
                         "breakdown on stderr; writes nothing")
    args = ap.parse_args()
    if args.cmd == 'count':
        stats = count()
        print(f"counted {stats['counted']} · judgeable {stats['judgeable']} "
              f"(no_transcript {stats['no_transcript']}, "
              f"no_injection_row {stats['no_injection_row']}) · target {TARGET}",
              file=sys.stderr)
        print(stats['judgeable'])
    elif args.cmd == 'make':
        stats = make()
        print(f"gate dossier -> {OUT_PATH}")
        print(f"  counted {stats['counted']} · judgeable {stats['judgeable']} "
              f"(no_transcript {stats['no_transcript']}, "
              f"no_injection_row {stats['no_injection_row']}) · target {TARGET}")
        if stats['judgeable'] < TARGET:
            print(f"  WARNING: below full-power target ({stats['judgeable']} < "
                  f"{TARGET}) — the gate read must not run yet (addendum A2).")
    else:
        labeled, incomplete = parse_dossier(OUT_PATH.read_text())
        if incomplete:
            print(f"INCOMPLETE rows (fill all three fields): {incomplete}")
            return
        LABELS_PATH.write_text('\n'.join(json.dumps(r) for r in labeled) + '\n')
        print(f"{len(labeled)} labeled rows -> {LABELS_PATH}")


if __name__ == '__main__':
    main()
