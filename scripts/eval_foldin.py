#!/usr/bin/env python3
"""Eval-case fold-in tooling (#1d) — grows evals/cases.jsonl 41 -> >=150 from
logged signal. Spec: docs/superpowers/specs/2026-07-20-eval-foldin-design.md.

mine   — deterministic candidate mining (no LLM): auto-fold operator-attested
         rows, render the review checklist for everything else.
fold   — merge staged auto-fold + confirmed checklist rows into cases.jsonl,
         session-grouped split, then eval_split.seed_file().
status — distance to the >=150 floor with pos/neg balance.

Hard rules: burn-exclusion (fresh HIGH gate keys, incl. their whole
(session, ts) group) beats everything; auto-fold takes only never-revised
operator-attested labels; all exclusions are counted, never silent.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict


def candidate_id(session_id: str, ts: str) -> str:
    return 'fold-' + hashlib.sha1(f'{session_id}|{ts}'.encode()).hexdigest()[:10]


def build_burn_keys(v1_rows: list[dict], v2_rows: list[dict],
                    cal_keys: set, inj_rows: 'list[dict] | tuple' = ()) -> set:
    """(session_id, ts) of every fresh HIGH gate row — the whole prompt-moment
    is off limits (spec §4), so burning is at group granularity. `inj_rows`
    covers HIGH injections whose SessionEnd outcome row does not exist yet
    (session still running / logger missed it): those are FUTURE gate rows."""
    burn = set()
    for r in list(v1_rows) + list(v2_rows) + list(inj_rows):
        if r.get('tier') != 'high' or not r.get('session_id'):
            continue
        if (r['session_id'], r.get('ts'), r.get('injected')) in cal_keys:
            continue
        burn.add((r['session_id'], r.get('ts')))
    return burn


def calibration_autofold(cal_rows: list[dict],
                         burn_keys: set) -> tuple[list[dict], list[dict]]:
    """Spec §3.1: auto-fold = uniformly engaged=true, never-revised groups.
    relabel-flagged engaged=true rows and mixed-label groups -> checklist."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in cal_rows:
        groups[(r.get('session_id'), r.get('ts'))].append(r)
    auto, checklist = [], []
    for key, rows in sorted(groups.items()):
        if key in burn_keys:
            continue
        pos = [r for r in rows if r.get('label_engaged')]
        if not pos:
            continue
        revised = [r for r in pos if 'relabel' in r]
        mixed = len(pos) != len(rows)
        src = {'session_id': key[0], 'ts': key[1]}
        expect = sorted(r['injected'] for r in pos)
        if revised:
            checklist.append({'src': src, 'expect_proposed': expect,
                              'signal': 'calibration engaged=yes but REVISED '
                                        'post-judge — independent judgment required'})
        elif mixed:
            checklist.append({'src': src, 'expect_proposed': expect,
                              'signal': 'calibration MIXED labels (sibling slug '
                                        'not engaged) — set the expect list'})
        else:
            auto.append({'id': candidate_id(*key), 'expect': expect,
                         'provenance': 'calibration', 'src': src})
    return auto, checklist


import re

_TS_RE = re.compile(r'\bts=([0-9T:\-\.]+)')
_INJ_RE = re.compile(r'\binjected=([A-Za-z0-9_\-]+)')


def parse_hooktuning_traces(md_text: str) -> list[dict]:
    """One (ts, injected) per '###' entry whose Log trace is structured.
    Prose-only entries are ignored (spec §3.2 — honest-yield note)."""
    traces = []
    for entry in re.split(r'(?m)^### ', md_text):
        ts, inj = _TS_RE.search(entry), _INJ_RE.search(entry)
        if ts and inj:
            traces.append({'ts': ts.group(1).rstrip('.,'), 'injected': inj.group(1)})
    return traces


def hooktuning_autofold(traces: list[dict], inj_rows: list[dict],
                        burn_keys: set) -> tuple[list[dict], int]:
    by_ts = {(r.get('ts'), r.get('injected')): r for r in inj_rows}
    auto, burned = [], 0
    for t in traces:
        r = by_ts.get((t['ts'], t['injected']))
        if r is None:
            continue
        key = (r.get('session_id'), r.get('ts'))
        if key in burn_keys:
            burned += 1
            continue
        auto.append({'id': candidate_id(*key), 'expect': [],
                     'provenance': 'hook-tuning',
                     'src': {'session_id': key[0], 'ts': key[1]}})
    return auto, burned


from pathlib import Path

import engagement_judge as ej
from injection_outcome import parse_turns
from outcome_matching import slug_tokens, tokenize
from memory_inject_hook import is_synthetic

PROMPT_CAP = 1000


def recover_prompt(inj_row: dict) -> tuple[str, bool]:
    head = inj_row.get('prompt_head') or ''
    sid = inj_row.get('session_id') or ''
    tp = ej.resolve_transcript(sid) if sid else None
    if tp is not None:
        turns = parse_turns(tp)
        ti = ej._find_trigger(turns, head)
        if ti is not None:
            return ej._norm(turns[ti]['text'])[:PROMPT_CAP], False
    return head, True


def propose_kind(prompt: str, expect: list) -> str:
    if not expect:
        return 'negative'
    ptoks = tokenize(prompt)
    if any(slug_tokens(s) and slug_tokens(s) <= ptoks for s in expect):
        return 'direct'
    return 'paraphrase'


def _outcome_join(v1_rows: list[dict], v2_rows: list[dict]) -> dict:
    """(sid, ts, slug) -> (v1_engaged, v2_engaged) for rows present in both."""
    k = lambda r: (r.get('session_id'), r.get('ts'), r.get('injected'))
    v1 = {k(r): bool(r.get('engaged_in_assistant')) for r in v1_rows}
    return {key: (v1[key], bool(r.get('engaged_in_assistant')))
            for r in v2_rows if (key := k(r)) in v1}


def mine_candidates(inj_rows: list[dict], v1_rows: list[dict],
                    v2_rows: list[dict], burn_keys: set,
                    taken_keys: set) -> list[dict]:
    """Checklist candidate classes 3-5 (spec §3), grouped per (session, ts)."""
    joined = _outcome_join(v1_rows, v2_rows)
    out = []
    for r in inj_rows:
        key = (r.get('session_id') or '', r.get('ts'))
        if (key in burn_keys) or (key in taken_keys):
            continue
        tier = r.get('tier')
        if tier == 'skipped' or is_synthetic(r.get('prompt_head') or ''):
            continue
        base = {'src': {'session_id': key[0], 'ts': key[1]},
                'project': r.get('project'), 'prompt_head': r.get('prompt_head'),
                'hide_score': False}
        if tier == 'high' and not key[0]:
            out.append({**base, 'expect_proposed': [r.get('injected')],
                        'hide_score': True,
                        'signal': 'HIGH fire with NO outcome signal — judge '
                                  'independently; the scorer fire is NOT evidence'})
        elif tier == 'none':
            out.append({**base, 'expect_proposed': [],
                        'signal': f'tier={tier}: nothing advertised — silence right?'})
        elif tier == 'moderate':
            slugs = [s for s in (r.get('advertised') or [])
                     if joined.get((key[0], key[1], s)) == (True, True)]
            not_eng = [s for s in (r.get('advertised') or [])
                       if joined.get((key[0], key[1], s)) == (False, False)]
            if slugs:
                out.append({**base, 'expect_proposed': sorted(slugs),
                            'signal': 'moderate: both proxies say engaged — should '
                                      'THIS prompt retrieve these slug(s)?'})
            elif not_eng:
                out.append({**base, 'expect_proposed': [],
                            'signal': 'moderate: both proxies say NOT engaged — '
                                      'silence, or a different slug?'})
    return out


def _norm_prompt(p: 'str | None') -> str:
    return ' '.join((p or '').lower().split()).strip('!?. ')


def dedup_candidates(cands: list[dict], existing_cases: list[dict]
                     ) -> tuple[list[dict], list[dict], dict]:
    """Spec §5 dedup: silent skip ONLY on true duplicates (id, or prompt+expect
    both matching); prompt-match-with-different-expect -> collision row."""
    known_ids = {c.get('id') for c in existing_cases}
    by_prompt = {_norm_prompt(c.get('prompt')): c for c in existing_cases}
    kept, collisions = [], []
    counts = {'dup_id': 0, 'dup_exact': 0}
    for c in cands:
        cid = candidate_id(c['src']['session_id'], c['src']['ts'])
        if cid in known_ids:
            counts['dup_id'] += 1
            continue
        np = _norm_prompt(c.get('prompt'))
        other = by_prompt.get(np)
        if other is not None:
            if sorted(other.get('expect') or []) == sorted(c['expect_proposed']):
                counts['dup_exact'] += 1
            else:
                collisions.append({**c, 'collision': {
                    'case_id': other.get('id'),
                    'their_expect': list(other.get('expect') or [])}})
            continue
        by_prompt[np] = {'id': cid, 'prompt': c.get('prompt'),
                         'expect': c['expect_proposed']}
        kept.append(c)
    return kept, collisions, counts


_CHECK_HEADER = """\
# EVAL FOLD-IN — review checklist
#
# For each row answer the ONE question: **should THIS prompt retrieve the
# slug(s) listed?** Engagement/signal lines are context, not the question.
#  - keep: yes|no      (no = do not fold this case at all)
#  - expect: comma-separated slugs; make it EMPTY for a negative
#  - kind: direct | paraphrase | negative   (pre-filled proposal, editable)
# Then run: python scripts/eval_foldin.py fold
"""


def render_checklist(cands: list[dict]) -> str:
    lines = [_CHECK_HEADER]
    for i, c in enumerate(cands, 1):
        cid = candidate_id(c['src']['session_id'], c['src']['ts'])
        lines += [f"## {i}. {cid}  (project: {c.get('project')})",
                  f"_src: `{c['src']['session_id']}` · ts {c['src']['ts']}_",
                  f"_signal: {c['signal']}_"]
        if c.get('collision'):
            col = c['collision']
            lines.append(f"_COLLISION: existing case `{col['case_id']}` expects "
                         f"{col['their_expect']} — merge or pick_")
        if c.get('head_only'):
            lines.append('_prompt is the 120-char head only (transcript gone)_')
        lines += [f"**Prompt:** {c['prompt']}",
                  '**Should THIS prompt retrieve the slug(s) below?** '
                  '(edit the list; empty = negative)',
                  f"- expect: {', '.join(c['expect_proposed'])}",
                  '- keep: ?',
                  f"- kind: {propose_kind(c['prompt'], c['expect_proposed'])}",
                  '', '---', '']
    return '\n'.join(lines)


_ROW_HEAD_RE = re.compile(r'^## (\d+)\. (fold-[0-9a-f]{10})')
_SRC_RE = re.compile(r'^_src: `(.*)` · ts (\S+)_')


def parse_checklist(md: str) -> tuple[list[dict], list[int]]:
    rows, cur = [], None
    for line in md.splitlines():
        line = line.strip()
        m = _ROW_HEAD_RE.match(line)
        if m:
            cur = {'ordinal': int(m.group(1))}
            rows.append(cur)
            continue
        if cur is None:
            continue
        m = _SRC_RE.match(line)
        if m:
            cur['src'] = {'session_id': m.group(1), 'ts': m.group(2)}
        elif line.startswith('- keep:'):
            v = line.split(':', 1)[1].strip().lower()
            if v in ('yes', 'no'):
                cur['keep'] = (v == 'yes')
        elif line.startswith('- expect:'):
            raw = line.split(':', 1)[1].strip()
            cur['expect'] = [s.strip() for s in raw.split(',') if s.strip()]
        elif line.startswith('- kind:'):
            v = line.split(':', 1)[1].strip().lower()
            if v in ('direct', 'paraphrase', 'negative'):
                cur['kind'] = v
    answers, incomplete = [], []
    for r in rows:
        if 'keep' in r and 'expect' in r and 'kind' in r and 'src' in r:
            answers.append({k: r[k] for k in ('src', 'keep', 'expect', 'kind')})
        else:
            incomplete.append(r['ordinal'])
    return answers, incomplete


def checklist_in_progress(md: str) -> bool:
    return bool(re.search(r'(?m)^- keep: (yes|no)\b', md))


import argparse
import json

import eval_split

BASE_DIR = Path(__file__).resolve().parent.parent
CASES_PATH = BASE_DIR / 'evals' / 'cases.jsonl'
INJECTION_LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
V1_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
V2_PATH = BASE_DIR / 'logs' / 'injection_outcomes_rescored.jsonl'
CAL_PATH = BASE_DIR / 'evals' / 'fixtures' / 'engagement_calibration_2026-07-03.jsonl'
HOOKTUNING_PATH = BASE_DIR / 'docs' / 'hook-tuning' / 'memory-injection.md'
AUTO_STAGE_PATH = BASE_DIR / 'evals' / 'candidates' / 'eval-foldin-auto.jsonl'
REVIEW_PATH = BASE_DIR / 'evals' / 'candidates' / 'eval-foldin-review.md'
BATCH_SIZE = 40
TARGET = 150


def _read_jsonl(path: Path) -> list[dict]:
    out = []
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


def assign_fold_split(row: dict) -> str:
    key = (row.get('src') or {}).get('session_id') or row.get('id')
    b = eval_split.stable_bucket(key)
    held = eval_split.FRACTIONS['held_out']
    val = eval_split.FRACTIONS['val']
    if b < 1 - held - val:
        return 'train'
    return 'val' if b < 1 - held else 'held_out'


def finish_case(row: dict, inj_by_key: dict) -> dict | None:
    inj = inj_by_key.get((row['src']['session_id'], row['src']['ts']))
    if inj is None:
        return None
    prompt, head_only = recover_prompt(inj)
    case = {'id': row['id'], 'kind': row.get('kind') or propose_kind(prompt, row['expect']),
            'project': inj.get('project'), 'prompt': prompt,
            'expect': row['expect'], 'split': assign_fold_split(row),
            'provenance': row['provenance'], 'src': row['src']}
    if head_only:
        case['head_only'] = True
    return case


def fold(answers: list[dict]) -> dict:
    inj_by_key = {(r.get('session_id'), r.get('ts')): r
                  for r in _read_jsonl(INJECTION_LOG_PATH)}
    staged = _read_jsonl(AUTO_STAGE_PATH)
    confirmed = [{'id': candidate_id(a['src']['session_id'], a['src']['ts']),
                  'expect': a['expect'], 'kind': a['kind'],
                  'provenance': 'foldin-review', 'src': a['src']}
                 for a in answers if a['keep']]
    counts = {'folded': 0, 'no_injection_row': 0,
              'rejected': sum(1 for a in answers if not a['keep'])}
    existing = _read_jsonl(CASES_PATH)
    existing_ids = {c.get('id') for c in existing}
    sessions = defaultdict(set)
    for c in existing:
        sid = c.get('src', {}).get('session_id')
        if sid:
            sessions[sid].add(c.get('split'))
    new_lines = []
    for row in staged + confirmed:
        if row['id'] in existing_ids:
            continue
        case = finish_case(row, inj_by_key)
        if case is None:
            counts['no_injection_row'] += 1
            continue
        sid = case['src']['session_id']
        splits = sessions.get(sid, set()) if sid else set()
        if len(splits) > 1 or (splits and None in splits):
            raise ValueError(f'existing session has inconsistent splits: {sid}')
        if splits:
            case['split'] = next(iter(splits))
        if sid:
            sessions[sid].add(case['split'])
        new_lines.append(json.dumps(case))
        existing_ids.add(row['id'])
        counts['folded'] += 1
    if new_lines:
        text = CASES_PATH.read_text()
        CASES_PATH.write_text(text.rstrip('\n') + '\n' + '\n'.join(new_lines) + '\n')
        eval_split.seed_file(CASES_PATH)
    # Spec §5: fold-time echo of run_eval's unknown-slug warning (fail-open —
    # a missing/unreadable index warns but never blocks the fold).
    try:
        import memory_index as mi
        known = {e['slug'] for e in mi.load_index()['entries']}
        unknown = sorted({s for line in new_lines
                          for s in json.loads(line)['expect'] if s not in known})
        counts['unknown_slugs'] = unknown
        for s in unknown:
            print(f'  ! WARN expect slug not in index -> {s}')
    except Exception as e:
        print(f'  ! WARN could not check slugs against memory index: {e}')
        counts['unknown_slugs'] = None
    return counts


def status_counts(cases: list[dict]) -> dict:
    pos = sum(1 for c in cases if c.get('expect'))
    by = lambda k: {v: sum(1 for c in cases if c.get(k) == v)
                    for v in sorted({c.get(k) for c in cases}, key=str)}
    return {'n': len(cases), 'pos': pos, 'neg': len(cases) - pos,
            'by_kind': by('kind'), 'by_split': by('split'),
            'by_project': by('project'),
            'remaining_to_target': max(0, TARGET - len(cases))}


def read_reviewed(path: Path) -> list[dict]:
    """Reviewed input must fail loudly on missing files or malformed JSON."""
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip() and not line.startswith('#')]


def fold_reviewed(rows: list[dict]) -> dict:
    """Promote explicit reviewed labels, preserving evidence and excluding held-out.

    This path never consumes the unrelated auto-stage or checklist. Validate the
    entire batch before writing, so a bad label cannot cause a partial promotion.
    """
    existing = _read_jsonl(CASES_PATH)
    ids = {r['id'] for r in existing}
    sessions = defaultdict(set)
    for r in existing:
        sid = r.get('src', {}).get('session_id')
        if sid:
            sessions[sid].add(r.get('split'))
    inj = _read_jsonl(INJECTION_LOG_PATH)
    inj_keys = {(r.get('session_id') or '', r.get('ts')) for r in inj}
    cal_keys = {(r.get('session_id'), r.get('ts'), r.get('injected'))
                for r in _read_jsonl(CAL_PATH)}
    burn = build_burn_keys(_read_jsonl(V1_PATH), _read_jsonl(V2_PATH), cal_keys, inj)
    import memory_index as mi
    known = {e['slug'] for e in mi.load_index()['entries']}
    new = []
    counts = {'folded': 0, 'already_present': 0}
    for r in rows:
        src = r['src']
        sid, ts = src['session_id'], src['ts']
        if r['id'] != candidate_id(sid, ts):
            raise ValueError('reviewed id does not match source')
        if r['id'] in ids:
            prior = next(c for c in existing + new if c['id'] == r['id'])
            if any(prior.get(k) != r.get(k) for k in
                   ('prompt', 'expect', 'kind', 'project', 'provenance',
                    'operator_attested', 'reviewer', 'reason', 'basis',
                    'operator_adjudication')):
                raise ValueError(f"conflicting reviewed label: {r['id']}")
            counts['already_present'] += 1
            continue
        if (sid, ts) in burn or (sid, ts) not in inj_keys:
            raise ValueError(f"burned or missing injection source: {r['id']}")
        provenance = r.get('provenance')
        if (provenance not in ('agent-review', 'operator-reviewed') or
                r.get('operator_attested') is not (provenance == 'operator-reviewed')):
            raise ValueError('invalid review provenance/attestation')
        if provenance == 'operator-reviewed' and not r.get('operator_adjudication'):
            raise ValueError('operator-reviewed label needs adjudication record')
        if any(not isinstance(r.get(k), str) or not r[k].strip()
               for k in ('prompt', 'reviewer', 'reason', 'basis')):
            raise ValueError('reviewed label needs prompt, reviewer, reason and basis')
        expect = r.get('expect')
        if (not isinstance(expect, list) or
                any(not isinstance(s, str) or s not in known for s in expect)):
            raise ValueError('invalid or unknown expected slug')
        if r.get('kind') not in (('direct', 'paraphrase') if expect else ('negative',)):
            raise ValueError('kind does not match expected slugs')
        if is_synthetic(r['prompt']):
            raise ValueError('synthetic turns are not retrieval cases')
        splits = sessions.get(sid, set()) if sid else set()
        if splits and (len(splits) != 1 or not splits <= {'train', 'val'}):
            raise ValueError(f'cannot add reviewed label to held-out/unassigned session: {sid}')
        split = next(iter(splits)) if splits else (
            'train' if eval_split.stable_bucket(sid or r['id']) < .75 else 'val')
        case = {k: r[k] for k in ('id', 'kind', 'project', 'prompt', 'expect',
                                  'provenance', 'src', 'reviewer',
                                  'operator_attested', 'reason', 'basis')}
        for k in ('operator_adjudication', 'head_only', 'reviewed_at'):
            if k in r:
                case[k] = r[k]
        case['split'] = split
        new.append(case)
        ids.add(case['id'])
        if sid:
            sessions[sid].add(split)
    if new:
        text = CASES_PATH.read_text()
        CASES_PATH.write_text(text.rstrip('\n') + '\n' +
                             '\n'.join(json.dumps(r) for r in new) + '\n')
    counts['folded'] = len(new)
    return counts


def select_batch(candidates: list[dict], size: int) -> list[dict]:
    """Positive proposals first; stable order within each class."""
    return sorted(candidates, key=lambda c: (
        not bool(c['expect_proposed']), eval_split.stable_bucket(
            candidate_id(c['src']['session_id'], c['src']['ts']))))[:size]


def main() -> None:
    ap = argparse.ArgumentParser(description='#1d eval-case fold-in (mine/fold/status).')
    ap.add_argument('cmd', choices=('mine', 'fold', 'status'))
    ap.add_argument('--reviewed', type=Path, help='fold reviewed JSONL into train/val only')
    ap.add_argument('--review-history', type=Path, action='append', default=[],
                    help='mine: skip source events already adjudicated in this JSONL (repeatable)')
    ap.add_argument('--batch-size', type=int, default=BATCH_SIZE)
    args = ap.parse_args()
    if args.reviewed and args.cmd != 'fold':
        ap.error('--reviewed requires fold')
    if args.review_history and args.cmd != 'mine':
        ap.error('--review-history requires mine')
    if args.batch_size < 1:
        ap.error('--batch-size must be positive')
    if args.cmd == 'status':
        print(json.dumps(status_counts(_read_jsonl(CASES_PATH)), indent=2))
        return
    if args.cmd == 'mine':
        if REVIEW_PATH.exists() and checklist_in_progress(REVIEW_PATH.read_text()):
            raise SystemExit(f'{REVIEW_PATH} has filled answers — run `fold` first '
                             '(clobber guard, spec §5)')
        v1, v2 = _read_jsonl(V1_PATH), _read_jsonl(V2_PATH)
        cal = _read_jsonl(CAL_PATH)
        cal_keys = {(r.get('session_id'), r.get('ts'), r.get('injected')) for r in cal}
        inj = _read_jsonl(INJECTION_LOG_PATH)
        burn = build_burn_keys(v1, v2, cal_keys, inj_rows=inj)
        auto_cal, check_cal = calibration_autofold(cal, burn)
        traces = parse_hooktuning_traces(
            HOOKTUNING_PATH.read_text(errors='replace') if HOOKTUNING_PATH.exists() else '')
        auto_ht, n_burned_ht = hooktuning_autofold(traces, inj, burn)
        auto = auto_cal + auto_ht
        reviewed_keys = {(r['src']['session_id'], r['src']['ts'])
                         for path in args.review_history for r in read_reviewed(path)}
        auto = [r for r in auto if (r['src']['session_id'], r['src']['ts'])
                not in reviewed_keys]
        taken = {(r['src']['session_id'], r['src']['ts']) for r in auto}
        taken |= reviewed_keys
        cands = check_cal + mine_candidates(inj, v1, v2, burn, taken)
        inj_by_key = {(r.get('session_id'), r.get('ts')): r for r in inj}
        existing = _read_jsonl(CASES_PATH)
        held_sessions = {r['src']['session_id'] for r in existing
                         if r.get('split') == 'held_out' and r.get('src', {}).get('session_id')}
        suppressed = {'synthetic_or_skipped': sum(
            r.get('tier') == 'skipped' or is_synthetic(r.get('prompt_head') or '') for r in inj),
            'held_out_candidates': 0}
        full = []
        for c in cands:
            if (c['src']['session_id'], c['src']['ts']) in reviewed_keys:
                continue
            if c['src']['session_id'] in held_sessions:
                suppressed['held_out_candidates'] += 1
                continue
            r = inj_by_key.get((c['src']['session_id'], c['src']['ts']))
            if r is None:
                continue
            c = dict(c)
            c['prompt'], c['head_only'] = recover_prompt(r)
            if r.get('tier') == 'skipped' or is_synthetic(c['prompt']):
                continue
            c.setdefault('project', r.get('project'))
            c.setdefault('hide_score', False)
            full.append(c)
        kept, collisions, dd = dedup_candidates(full, existing)
        batch = select_batch(kept + collisions, args.batch_size)
        AUTO_STAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTO_STAGE_PATH.write_text('\n'.join(json.dumps(r) for r in auto) + '\n' if auto else '')
        REVIEW_PATH.write_text(render_checklist(batch))
        print(f'prior review sources excluded: {len(reviewed_keys)}; '
              f'positive proposals in batch: {sum(bool(c["expect_proposed"]) for c in batch)}')
        print(f'mining exclusions: {suppressed}')
        print(f'auto-staged {len(auto)} (calibration {len(auto_cal)}, hook-tuning '
              f'{len(auto_ht)}; burned-ht {n_burned_ht}) -> {AUTO_STAGE_PATH}')
        print(f'checklist {len(batch)}/{len(kept) + len(collisions)} candidates '
              f'(collisions {len(collisions)}; dup_id {dd["dup_id"]}, '
              f'dup_exact {dd["dup_exact"]}) -> {REVIEW_PATH}')
        return
    # fold
    if args.reviewed:
        counts = fold_reviewed(read_reviewed(args.reviewed))
        print(json.dumps({**counts, **status_counts(_read_jsonl(CASES_PATH))}, indent=2))
        return
    answers, incomplete = ([], [])
    if REVIEW_PATH.exists():
        answers, incomplete = parse_checklist(REVIEW_PATH.read_text())
    if incomplete:
        print(f'skipping incomplete checklist rows: {incomplete}')
    counts = fold(answers)
    if REVIEW_PATH.exists():
        first = answers[0]['src']['session_id'][:8] if answers else 'empty'
        REVIEW_PATH.rename(REVIEW_PATH.with_name(
            f'eval-foldin-review-{first}.done.md'))
    AUTO_STAGE_PATH.unlink(missing_ok=True)
    print(json.dumps({**counts, **status_counts(_read_jsonl(CASES_PATH))}, indent=2))


if __name__ == '__main__':
    main()
