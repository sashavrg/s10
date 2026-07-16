#!/usr/bin/env python3
"""recall_miss — the under-injection signal (self-improvement front #1b).

The complement of the false-HIGH (over-injection) signal: cases where the KB *held* the
relevant fact but the hook didn't surface it. Grounded in corrections (ground truth that
the assistant got something wrong): a ``recall_miss`` is a harvested correction that, fed
as a query to the LIVE retriever, would inject a topic at HIGH — yet that topic was NOT
injected HIGH in the session the correction came from. The KB could have prevented the
mistake and didn't.

**Why reuse retrieve() instead of token overlap.** A first cut matched correction tokens
against slug tokens directly; on real data it produced ~33 "misses" from one correction,
almost all matching only generic project tokens (api/config/portal/acme) — the same
failure as the lexical broad-slug bug. Feeding the correction through ``retrieve()``
inherits every precision guard the live system already applies (catch-all demotion,
distinctiveness, thresholds) and makes the definition self-consistent with what actually
gets injected. It is **binary per correction** (one missed info-need), not one-per-entry.

**Precision-first by design** → it under-counts (it ignores the harder "user re-stated KB
info without correcting" case), so recall computed from it is an honest **lower bound**.
Computed live from existing logs — no new log file, no new nightly step; consumed by
``scorecard.py`` for metric B.

**Symmetric stats — hits AND misses (`score_correction`/`stats`, Task 6).**
``detect_session_miss``/``recall_misses`` above only ever emit MISSES — there is no
notion of a "hit" to divide by, so a recall number built from them alone would be
silently pinned near 1.0 (nothing in the denominator to pull it down). ``score_correction``
scores a correction ``'hit' | 'miss' | None`` over the SAME correction population (hit =
the retriever's top HIGH match for the correction WAS surfaced in that session; miss =
it wasn't), and ``stats()`` aggregates hits + misses across all harvested corrections,
plus two exclusion buckets so nothing is silently dropped: ``unattributable`` (the
correction's session predates ``session_id`` logging entirely — not in
``memory_injection.jsonl`` at all, so neither hit nor miss is knowable) and
``not_fired`` (the retriever wouldn't fire HIGH for this correction at all — the KB
doesn't claim to know it, so it's excluded from both numerator and denominator, same
precision-first spirit as the binary functions above). This is scorecard's signal B.

**Forward link:** a repeatedly-missed slug is the symmetric counterpart to
``outcome_demoted`` — a future governance action could *promote* it (lower its threshold /
enrich its facts), the under-injection cure. Not built here.
"""
from __future__ import annotations

import json
from pathlib import Path

from injection_outcome import HARVEST_STATE_PATH, correction_text_for_session

BASE_DIR = Path(__file__).resolve().parent.parent
INJECTION_LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'


def detect_session_miss(correction_text: str, project: str | None,
                        session_high_slugs: set, retrieve_fn) -> dict | None:
    """Would the live retriever inject a topic HIGH for this correction that the session
    didn't surface? Returns {slug, score} for the miss, else None. Binary per correction."""
    result = retrieve_fn(correction_text, project)
    if result.get('top_tier') != 'high':
        return None                         # system wouldn't inject anything HIGH -> no miss
    matches = result.get('matches') or []
    if not matches:
        return None
    top = matches[0]
    if top.get('slug') in session_high_slugs:
        return None                         # we DID surface it -> not a miss
    return {'slug': top.get('slug'), 'score': top.get('score')}


def high_injects_by_session(injection_rows: list[dict]) -> dict:
    """session_id -> set of slugs injected HIGH (the topics we DID surface)."""
    out: dict[str, set] = {}
    for r in injection_rows:
        sid = r.get('session_id')
        if sid and r.get('tier') == 'high' and r.get('injected'):
            out.setdefault(sid, set()).add(r['injected'])
    return out


def recall_misses(sessions_with_corrections: list[tuple], injection_rows: list[dict],
                  retrieve_fn) -> list[dict]:
    """Orchestrate over corrected sessions. sessions_with_corrections = [(session_id,
    project, correction_text), ...]. Returns ≤1 miss per correction, with session/project."""
    high_by_sess = high_injects_by_session(injection_rows)
    out = []
    for sid, project, text in sessions_with_corrections:
        m = detect_session_miss(text, project, high_by_sess.get(sid, set()), retrieve_fn)
        if m:
            out.append({'session_id': sid, 'project': project, **m})
    return out


# --------------------------------------------------------------------------- I/O (fail-open)

def load_injection_rows(path: Path | str = INJECTION_LOG_PATH) -> list[dict]:
    rows: list[dict] = []
    try:
        text = Path(path).read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_sessions_with_corrections(state_path: Path | str = HARVEST_STATE_PATH,
                                   base_dir: Path = BASE_DIR) -> list[tuple]:
    """[(session_id, project, correction_text)] for every harvested session with a note."""
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for sid, s in state.get('sessions', {}).items():
        if not s.get('note'):
            continue
        text = correction_text_for_session(sid, state_path=Path(state_path), base_dir=base_dir)
        if text:
            out.append((sid, s.get('project'), text))
    return out


def _live_retrieve(text: str, project: str | None):
    """Bridge to the live retriever (imported lazily so unit tests stay index-free)."""
    import memory_index
    return memory_index.retrieve(text, project=project)


def compute(retrieve_fn=_live_retrieve) -> list[dict]:
    """All recall_misses across harvested sessions, computed live from existing logs."""
    return recall_misses(load_sessions_with_corrections(), load_injection_rows(), retrieve_fn)


def count(retrieve_fn=_live_retrieve) -> int:
    return len(compute(retrieve_fn))


# ----------------------------------------------------------- symmetric correction-population recall (Task 6)

def known_sessions(injection_rows: list[dict]) -> set:
    """Sessions the injection log can attribute (has session_id-tagged rows)."""
    return {r['session_id'] for r in injection_rows if r.get('session_id')}


def score_correction(correction_text: str, project: str | None,
                     session_high_slugs: set, retrieve_fn) -> str | None:
    """'hit' | 'miss' | None per correction, on the correction population only.
    None = the retriever wouldn't fire HIGH for this correction (the KB doesn't
    claim to know it) — excluded from both numerator and denominator."""
    result = retrieve_fn(correction_text, project)
    if result.get('top_tier') != 'high':
        return None
    matches = result.get('matches') or []
    if not matches:
        return None
    return 'hit' if matches[0].get('slug') in session_high_slugs else 'miss'


def stats(retrieve_fn=_live_retrieve) -> dict:
    """Symmetric recall stats over corrected sessions (scorecard signal B).
    Corrections from sessions absent from the injection log entirely are
    'unattributable' (pre-session_id history), never scored as misses.
    `not_fired` counts corrections the retriever wouldn't fire HIGH for at all
    (score_correction -> None) — the KB doesn't claim to know it, so it's
    neither a hit nor a miss; counted so it isn't silently indistinguishable
    from 'nothing to score'."""
    injection_rows = load_injection_rows()
    high_by_sess = high_injects_by_session(injection_rows)
    known = known_sessions(injection_rows)
    out = {'hits': 0, 'misses': 0, 'unattributable': 0, 'not_fired': 0, 'details': []}
    for sid, project, text in load_sessions_with_corrections():
        if sid not in known:
            out['unattributable'] += 1
            continue
        verdict = score_correction(text, project, high_by_sess.get(sid, set()), retrieve_fn)
        if verdict is None:
            out['not_fired'] += 1
            continue
        out['misses' if verdict == 'miss' else 'hits'] += 1
        out['details'].append({'session_id': sid, 'project': project, 'verdict': verdict})
    return out


def main() -> None:
    s = stats()
    total = s['hits'] + s['misses']
    recall = f"{s['hits']}/{total}" if total else "n/a (no scored corrections)"
    print(f"recall_miss: hits={s['hits']} misses={s['misses']} "
          f"unattributable={s['unattributable']}  recall={recall}")
    for d in s['details']:
        print(f"    [{d['project']}] {d['session_id']}  {d['verdict']}")


if __name__ == '__main__':
    main()
