#!/usr/bin/env python3
"""
SessionEnd companion to the memory-injection hook: turn each injection DECISION
into an injection OUTCOME, so the system finally has a utility/reward signal.

Today the pipeline logs *what it injected* (logs/memory_injection.jsonl) but
never *whether it helped*. This script closes that gap. For one ended session it:

  1. reads the injection decisions logged THIS session (joined by `session_id`,
     which memory_inject_hook.py now records on every row),
  2. reads the transcript and any correction harvested this session, and
  3. writes one OUTCOME row per injection to logs/injection_outcomes.jsonl:

       engaged_in_assistant - v2 (2026-07-02, via outcome_matching.score_row):
                              WORD-BOUNDARY + QUORUM match — at least min(2,
                              len(probe)) distinct probe tokens (slug + fact
                              tokens) must appear as whole words in an assistant
                              turn (v1 matched as a raw substring, so "api"
                              matched "therapist"). If FALSE on a HIGH inject,
                              that is a likely off-topic false positive — the 44%
                              problem made measurable.
       topic_corrected      - the same word-boundary + quorum match, against the
                              correction text harvested this session (negative
                              signal: the injected "fact" was wrong or contested).
       scorer_version        - 2 for rows produced by this path (mixed-version
                              logs stay analyzable — see outcome_matching.py).
                              Every row also carries engaged_matched /
                              corrected_matched, the actual tokens that matched,
                              as an audit trail so hand-audits (label_sample.py)
                              can check the proxy instead of trusting it.
       n_user_turns         - session size, for weighting.

These are deliberately COARSE proxies. The point is not a perfect label — it is
to start *capturing outcome at all*, since it cannot be reconstructed
retroactively. Hand-labelling (Phase 1 eval set) and the Phase 2 experiment loop
consume this stream; a future batch job can recompute richer signals from the
same persisted rows. This step changes no behaviour, never blocks the session,
and fails open: any error -> emit a status line, exit 0.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import outcome_matching
from outcome_matching import tokenize, slug_tokens  # re-export (v1 compat; tests use these)
from transcript_turns import parse_turns as parse_agent_turns

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
OUTCOME_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
HARVEST_STATE_PATH = BASE_DIR / 'state' / 'harvested_sessions.json'


def read_session_injections(session_id: str, log_path: Path = LOG_PATH) -> list[dict]:
    """Injection rows for this session that actually selected a topic (high/moderate)."""
    rows: list[dict] = []
    if not session_id or not log_path.exists():
        return rows
    for line in log_path.read_text(errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get('session_id') == session_id and r.get('tier') in ('high', 'moderate'):
            rows.append(r)
    return rows


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
            elif isinstance(block, str):
                parts.append(block)
        return '\n'.join(parts)
    return ''


def parse_turns(transcript_path: Path) -> list[dict]:
    """Return user/assistant turns from a supported agent transcript JSONL."""
    return parse_agent_turns(Path(transcript_path))


def correction_text_for_session(session_id: str,
                                state_path: Path = HARVEST_STATE_PATH,
                                base_dir: Path = BASE_DIR) -> str:
    """The text of the correction note harvested this session, if any (else '').

    ``note`` is recorded at harvest time as raw/inbox/<stem>.md, but the
    nightly sync-inbox (kb.py: sync_inbox -> ingest_inbox_file ->
    ingest_text_body) MOVES it into raw/web/inbox-<sync-ts>-<slug>.md, where
    <slug> is kb.py's slugify(<stem>, 36) — <stem> (already dash/lowercase
    from harvest_session.write_correction_note) truncated to 36 chars. If the
    recorded path is gone, fall back to locating it there by that derived
    suffix. Still fails open to '' if nothing is found or reading fails.
    """
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        return ''
    s = state.get('sessions', {}).get(session_id)
    if not s or not s.get('note'):
        return ''
    note_path = base_dir / s['note']
    try:
        return note_path.read_text(errors='replace')
    except Exception:
        pass
    try:
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', note_path.stem).strip('-').lower()[:36]
        if not slug:
            return ''
        matches = sorted((base_dir / 'raw' / 'web').glob(f'inbox-*-{slug}.md'))
        if matches:
            return matches[0].read_text(errors='replace')
    except Exception:
        pass
    return ''


def compute_outcomes(rows: list[dict], turns: list[dict], correction_text: str) -> list[dict]:
    """Pure core: injection rows + transcript turns + correction text -> outcome rows.
    v2 (2026-07-02): scoring delegates to outcome_matching (word-boundary + quorum +
    audit trail); rows carry scorer_version so mixed-version logs stay analyzable."""
    assistant_text = ' '.join(t['text'] for t in turns if t['role'] == 'assistant')
    n_user = sum(1 for t in turns if t['role'] == 'user')
    outcomes = []
    for r in rows:
        row = outcome_matching.score_row(r, assistant_text, correction_text)
        row['n_user_turns'] = n_user
        outcomes.append(row)
    return outcomes


def append_outcomes(outcomes: list[dict], path: Path = OUTCOME_PATH) -> None:
    if not outcomes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec='seconds')
    with path.open('a') as f:
        for o in outcomes:
            o = {**o, 'scored_at': stamp}
            f.write(json.dumps(o) + '\n')


def score_session(transcript: str, session_id: str) -> dict:
    # Read module path globals at call time (not via default args) so the paths
    # stay overridable for tests/smoke runs.
    rows = read_session_injections(session_id, LOG_PATH)
    if not rows:
        return {'status': 'noop', 'reason': 'no injections logged for session',
                'session': session_id}
    turns = parse_turns(Path(transcript))
    corr = correction_text_for_session(session_id, HARVEST_STATE_PATH, BASE_DIR)
    outcomes = compute_outcomes(rows, turns, corr)
    append_outcomes(outcomes, OUTCOME_PATH)
    return {
        'status': 'ok', 'session': session_id, 'scored': len(outcomes),
        'engaged': sum(1 for o in outcomes if o['engaged_in_assistant']),
        'corrected': sum(1 for o in outcomes if o['topic_corrected']),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description='Score this session\'s memory injections into logs/injection_outcomes.jsonl.')
    ap.add_argument('--transcript', required=True, help='Path to the session .jsonl transcript')
    ap.add_argument('--session', default='', help='Session id (defaults to transcript filename stem)')
    ap.add_argument('--project', default='', help='Project tag (unused in scoring; carried for logs)')
    args = ap.parse_args()
    session_id = args.session or Path(args.transcript).stem
    try:
        result = score_session(args.transcript, session_id)
    except Exception as e:  # measurement must never disrupt teardown
        result = {'status': 'error', 'reason': str(e), 'session': session_id}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
