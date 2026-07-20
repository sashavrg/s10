#!/usr/bin/env python3
"""Rebuild v2 outcome rows for every session in logs/memory_injection.jsonl whose
transcript is still on disk (~/.claude/projects/*/<session_id>.jsonl).

Wholesale-rewrites logs/injection_outcomes_rescored.jsonl on every run (idempotent —
a pure function of the injection log + transcripts + harvest state, same discipline
as topic_records). This file is the ANALYSIS dataset; the live SessionEnd log
(injection_outcomes.jsonl) stays the durable-capture / governance dataset.

Run it promptly and re-run nightly if desired: transcripts expire under Claude
Code's retention cleanup, and an expired transcript is a permanently lost row.
Also prints a v1-vs-v2 agreement report so the proxy change is quantified.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import engagement_judge as ej          # stdlib-only; module-level (no ej-unbound wart)
import outcome_matching
from injection_outcome import correction_text_for_session, parse_turns
from scorecard import read_outcomes

BASE_DIR = Path(__file__).resolve().parent.parent
INJECTION_LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
OUTCOME_V1_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
RESCORED_PATH = BASE_DIR / 'logs' / 'injection_outcomes_rescored.jsonl'


def find_transcript(session_id: str) -> Path | None:
    """Live projects dir first, then the retention-proof archive (ej9 gate
    addendum A5.3): an archived transcript keeps rescoring after Claude
    Code's retention cleanup expires the live copy."""
    return ej.resolve_transcript(session_id)


def group_sessions(rows: list[dict]) -> tuple[dict[str, list[dict]], int]:
    """session_id -> injection-decision rows (tier high/moderate). Session-less
    rows are counted, not silently dropped — they are unrescoreable."""
    sessions: dict[str, list[dict]] = {}
    sessionless = 0
    for r in rows:
        if r.get('tier') not in ('high', 'moderate'):
            continue
        sid = r.get('session_id')
        if not sid:
            sessionless += 1
            continue
        sessions.setdefault(sid, []).append(r)
    return sessions, sessionless


def rescore(sessions: dict, transcript_for, correction_for) -> tuple[list[dict], dict]:
    out: list[dict] = []
    missing_session_ids: set[str] = set()
    stats = {'sessions_scored': 0, 'sessions_missing_transcript': 0, 'rows': 0}
    for sid in sorted(sessions):
        tp = transcript_for(sid)
        if tp is None:
            stats['sessions_missing_transcript'] += 1
            missing_session_ids.add(sid)
            continue
        turns = parse_turns(Path(tp))
        assistant_text = ' '.join(t['text'] for t in turns if t['role'] == 'assistant')
        n_user = sum(1 for t in turns if t['role'] == 'user')
        corr = correction_for(sid)
        stats['sessions_scored'] += 1
        for r in sessions[sid]:
            row = outcome_matching.score_row(r, assistant_text, corr)
            row['n_user_turns'] = n_user
            out.append(row)
    stats['rows'] = len(out)
    stats['missing_session_ids'] = missing_session_ids
    out.sort(key=lambda r: r.get('ts') or '')
    return out, stats


def _row_key(r: dict) -> tuple:
    return (r.get('session_id'), r.get('ts'), r.get('injected'))


def merge_previous(new_rows: list[dict], previous_rows: list[dict],
                   missing_sessions: set) -> tuple[list[dict], int]:
    """F3: union-merge retention. Rewriting RESCORED_PATH from scratch every run
    silently drops rows whose transcripts have since expired under Claude Code's
    retention cleanup — a permanently lost row, since a transcript that's gone
    can never be rescored again. Instead: `new_rows` always win for keys present
    in both (a re-scoreable session's row is overwritten, never duplicated); a
    `previous_rows` row is carried forward UNCHANGED only when its key is absent
    from `new_rows` AND its session is in `missing_sessions` (transcript gone
    this run) — a previous row for a session that simply fell out of scope
    entirely (e.g. no longer has injection-decision rows at all) is NOT carried,
    since that isn't the "expired transcript" case this exists to protect.
    Returns (rows ts-sorted, carried_forward count)."""
    new_keys = {_row_key(r) for r in new_rows}
    carried = [r for r in previous_rows
              if _row_key(r) not in new_keys and r.get('session_id') in missing_sessions]
    merged = new_rows + carried
    merged.sort(key=lambda r: r.get('ts') or '')
    return merged, len(carried)


def agreement(v1_rows: list[dict], v2_rows: list[dict]) -> dict:
    """Field-level agreement on rows present in both, joined on (session_id, ts, injected)."""
    def key(r):
        return (r.get('session_id'), r.get('ts'), r.get('injected'))
    v1 = {key(r): r for r in v1_rows}
    rep = {'joined': 0, 'engaged_agree': 0, 'engaged_v1_only': 0, 'engaged_v2_only': 0,
           'corrected_agree': 0, 'corrected_v1_only': 0, 'corrected_v2_only': 0}
    for r2 in v2_rows:
        r1 = v1.get(key(r2))
        if r1 is None:
            continue
        rep['joined'] += 1
        e1, e2 = bool(r1.get('engaged_in_assistant')), bool(r2.get('engaged_in_assistant'))
        c1, c2 = bool(r1.get('topic_corrected')), bool(r2.get('topic_corrected'))
        rep['engaged_agree'] += e1 == e2
        rep['engaged_v1_only'] += e1 and not e2
        rep['engaged_v2_only'] += e2 and not e1
        rep['corrected_agree'] += c1 == c2
        rep['corrected_v1_only'] += c1 and not c2
        rep['corrected_v2_only'] += c2 and not c1
    return rep


def _atomic_write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    os.replace(tmp, path)


# ------------------------------------------------------------------ judge pass (#1e)

def graft_previous_judgments(rows: list[dict], previous_rows: list[dict],
                             judge_version: str) -> int:
    """Carry judged verdicts forward (topic_records-style idempotence): a previous
    scorer_version-3 row at the CURRENT judge_version transfers its verdict fields
    verbatim — zero GPU, and an LLM verdict is a cached fact, never re-rolled."""
    prev = {_row_key(r): r for r in previous_rows
            if r.get('scorer_version') == 3 and r.get('judge_version') == judge_version}
    n = 0
    for row in rows:
        p = prev.get(_row_key(row))
        if p is None:
            continue
        for f in ('engaged_in_assistant', 'scorer_version', 'judge_version',
                  'judge_rationale', 'judge_evidence'):
            if f in p:
                row[f] = p[f]
        n += 1
    return n


def select_judge_queue(rows: list[dict], judge_version: str) -> list[dict]:
    """Rows still needing judgment under the current version, oldest-first so a
    capped backfill completes deterministically across nights."""
    q = [r for r in rows
         if not (r.get('scorer_version') == 3 and r.get('judge_version') == judge_version)]
    q.sort(key=lambda r: r.get('ts') or '')
    return q


def apply_judgment(row: dict, verdict: dict, judge_version: str,
                   evidence_meta: dict) -> None:
    row['engaged_in_assistant'] = verdict['engaged']
    row['scorer_version'] = 3
    row['judge_version'] = judge_version
    row['judge_rationale'] = verdict.get('rationale', '')[:200]
    row['judge_evidence'] = evidence_meta


def run_judge_pass(rows: list[dict], previous_rows: list[dict],
                   judge_version: str, max_calls: int, judge_row_fn) -> dict:
    """Graft, then judge up to max_calls queued rows. judge_row_fn(row) ->
    (verdict|None, evidence_meta). None verdict leaves the row at v2 — a 3 is
    never faked. Rows beyond the cap stay v2 for the next night."""
    stats = {'grafted': graft_previous_judgments(rows, previous_rows, judge_version),
             'judged': 0, 'failed': 0, 'remaining': 0}
    queue = select_judge_queue(rows, judge_version)
    for row in queue:
        if stats['judged'] + stats['failed'] >= max_calls:
            stats['remaining'] = len(queue) - stats['judged'] - stats['failed']
            break
        verdict, meta = judge_row_fn(row)
        if verdict is None:
            stats['failed'] += 1
            continue
        apply_judgment(row, verdict, judge_version, meta)
        stats['judged'] += 1
    return stats


def _max_calls_from_env() -> int:
    """Parse KB_JUDGE_MAX_CALLS env var, clamping negative values to 0.

    pre-gate default: 0 (judge unvalidated — do not write verdicts into the
    analysis dataset until the gate passes; flip at Task 9/10). Operator
    decision 2026-07-02 (final-review Important #3)."""
    raw = os.environ.get('KB_JUDGE_MAX_CALLS', '0')
    return max(0, int(raw))


def _production_judge_row_fn(inj_by_key: dict, turns_cache: dict):
    """Resolve transcript -> evidence -> judge for one outcome row (production path).

    inj_by_key holds the RAW injection-log row, which for a moderate tier carries
    `advertised: [...]` but no `injected` key. build_evidence needs `injected` (it
    becomes topic_slug and feeds slug_tokens for nomination), so mirror the --dev
    harness: build an effective row with the OUTCOME row's `injected`/`tier`
    layered on top of the injection-log row, and judge THAT (prod/dev evidence
    parity — final-review C1)."""
    def judge_row(row):
        key = (row.get('session_id'), row.get('ts'))
        inj = inj_by_key.get(key)
        if inj is None:
            return None, {}
        sid = row.get('session_id')
        if sid not in turns_cache:
            tp = ej.resolve_transcript(sid)
            turns_cache[sid] = parse_turns(tp) if tp else []
        turns = turns_cache[sid]
        if not turns:
            return None, {}
        inj_eff = {**inj, 'injected': row.get('injected'), 'tier': row.get('tier')}
        page = None if row.get('tier') == 'high' else ej.load_topic_page(row.get('injected') or '')
        ev = ej.build_evidence(inj_eff, turns, page)
        meta = {'n_snippets': ev['n_snippets'], 'nomination_empty': ev['nomination_empty'],
                'anchored': ev['anchored']}
        return ej.judge_engagement(ev), meta
    return judge_row


def main() -> None:
    injections = read_outcomes(INJECTION_LOG_PATH)   # same tolerant jsonl reader
    sessions, sessionless = group_sessions(injections)
    rows, stats = rescore(
        sessions,
        transcript_for=find_transcript,
        correction_for=lambda sid: correction_text_for_session(sid),
    )
    previous_rows = read_outcomes(RESCORED_PATH)      # read BEFORE overwriting
    merged, carried_forward = merge_previous(rows, previous_rows, stats['missing_session_ids'])

    max_calls = _max_calls_from_env()
    inj_by_key = {(r.get('session_id'), r.get('ts')): r for r in injections}
    # Unconditional: graft (cached verdicts) must survive even when the judge is
    # disabled (max_calls=0) — only the judging LOOP is gated by max_calls, inside
    # run_judge_pass itself (final-review C2).
    jstats = run_judge_pass(merged, previous_rows, ej.JUDGE_VERSION,
                            max_calls, _production_judge_row_fn(inj_by_key, {}))
    label = 'disabled' if max_calls == 0 else ej.JUDGE_VERSION

    _atomic_write(merged, RESCORED_PATH)
    rep = agreement(read_outcomes(OUTCOME_V1_PATH), merged)
    print(f"rescored: {stats['rows']} rows / {stats['sessions_scored']} sessions "
          f"(+{carried_forward} carried forward from a previous run) "
          f"-> {len(merged)} total -> {RESCORED_PATH.name}")
    print(f"unrescoreable: {stats['sessions_missing_transcript']} sessions missing "
          f"transcripts, {sessionless} session-less injection rows")
    print(f"judge [{label}]: "
          f"grafted {jstats['grafted']}, judged {jstats['judged']}, "
          f"failed(stay v2) {jstats['failed']}, remaining {jstats['remaining']}")
    j = rep['joined'] or 1
    print(f"v1/v2 agreement over {rep['joined']} joined rows: "
          f"engaged {rep['engaged_agree']}/{j} "
          f"(v1-only {rep['engaged_v1_only']}, v2-only {rep['engaged_v2_only']}), "
          f"corrected {rep['corrected_agree']}/{j} "
          f"(v1-only {rep['corrected_v1_only']}, v2-only {rep['corrected_v2_only']})")


if __name__ == '__main__':
    main()
