#!/usr/bin/env python3
"""Production scorecard (self-improvement front #1c).

Computes the Perplexity-derived quality signals (borrow-map #7), TIER-STRATIFIED
(HIGH = the injected population under test; MODERATE = advertised-only control —
organic recurrence, no memory in context) — the metric definitions gate-1's eval
set will eventually measure on held-out cases, computed here on production
traffic so "are we improving?" is answerable from data we already have:

  * **A — correctness-on-seen:** engaged-without-correction rate on *repeat-topic*
    injects (a slug injected in a session where it already appeared in ≥1 prior session).
  * **B — recall:** ``hits/(hits+misses)`` over the correction population. Computed
    by ``recall_miss.stats()`` (symmetric hits AND misses on corrections, plus an
    unattributable/not_fired accounting — see that module's docstring) and passed
    in here as ``recall_stats``; ``main()`` wires the live call, tests pass the
    dict directly so ``scorecard()`` itself stays index-free.
  * **D — the compounding curve:** engagement/useful rate bucketed by
    ``prior_session_depth`` (distinct prior sessions that touched the same topic,
    keyed per (slug, project)). "Improves the longer you use it" — the operational
    signature of open-endedness.

Honestly unavailable (reported as such, never faked):
  * **C — cost** (tokens-per-resolved-task): needs per-task token counts we don't log.

``prior_session_depth`` is *derived*, no new logging: order outcomes by ``ts`` and count
the distinct prior sessions a slug appeared in. The useful/harmful verdict is imported
from ``topic_records`` so the definition stays canonical across the whole system.

``main()`` defaults ``--log`` to ``logs/injection_outcomes_rescored.jsonl`` (the v2
analysis dataset, union-merge retained by ``rescore_outcomes.py``) when it exists,
falling back to the live ``injection_outcomes.jsonl`` otherwise. Analysis is pinned
to (scorer_version=3, current judge_version); --all-versions is diagnostic only.
The live log supplies deduplicated event identity and depth BEFORE time, tier or
judge filtering. Coverage reports reconstructed analysis-only events and pending
judgments separately from the tripwire's accrual count. --window START / --until
END select [START, END), without losing earlier history. Explicit --log supplies
its own history unless --history-log is given, so isolated reads stay isolated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

from topic_records import outcome_verdict   # canonical useful/harmful/neutral verdict

BASE_DIR = Path(__file__).resolve().parent.parent
OUTCOME_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
RESCORED_PATH = BASE_DIR / 'logs' / 'injection_outcomes_rescored.jsonl'
# Written by measurement-v2 Task 10 Step 4 when the collection window is declared
# open (one ISO date). Absent = no window yet, and the compounding-read tripwire
# stays silent rather than counting against a window that does not exist.
WINDOW_MARKER = BASE_DIR / 'state' / 'window_open'

# Compounding-curve depth buckets (Perplexity borrow: 0 / 1-3 / 4-10 / 10+).
DEPTH_BUCKETS = (
    ('0', 0, 0),
    ('1-3', 1, 3),
    ('4-10', 4, 10),
    ('10+', 11, None),
)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial rate. None when n == 0."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((centre - margin) / denom, 4), round((centre + margin) / denom, 4))


def _bucket_for(depth: int) -> str:
    for label, lo, hi in DEPTH_BUCKETS:
        if depth >= lo and (hi is None or depth <= hi):
            return label
    return DEPTH_BUCKETS[-1][0]


def enrich_depth(outcomes: list[dict]) -> list[dict]:
    """Annotate each outcome with ``prior_session_depth`` (distinct prior sessions that
    touched the same slug WITHIN THE SAME PROJECT) and ``repeat_topic``. Pure: walks a
    ts-sorted copy, so input order is irrelevant and same-session repeats don't inflate
    depth. Keyed on (slug, project) so a slug recurring in one project never counts as
    depth for the same slug in a different project."""
    # SessionEnd can append the same event again. Count events, not log writes;
    # later copies carry the latest annotation but do not create new exposure.
    ordered = sorted(unique_rows(outcomes), key=lambda r: r.get('ts') or '')
    seen: dict[tuple, set] = {}
    out = []
    for row in ordered:
        slug = row.get('injected') or ''
        key = (slug, row.get('project'))
        sid = row.get('session_id')
        sessions = seen.setdefault(key, set())
        depth = len(sessions - {sid})   # distinct PRIOR sessions, excluding the current one
        out.append({**row, 'prior_session_depth': depth, 'repeat_topic': depth > 0})
        if sid is not None:
            sessions.add(sid)
    return out


def event_key(row: dict) -> tuple:
    """Same durable event key as rescore_outcomes._row_key."""
    return (row.get('session_id'), row.get('ts'), row.get('injected'))


def unique_rows(rows: list[dict]) -> list[dict]:
    return list({event_key(r): r for r in rows}.values())


def with_depth(rows: list[dict]) -> list[dict]:
    """Keep depth computed BEFORE a caller's window/tier/version selection.

    Reject partially enriched populations: recomputing would erase history for
    some rows; trusting them would mix incompatible depth definitions.
    """
    annotated = ['prior_session_depth' in r for r in rows]
    if any(annotated) and not all(annotated):
        raise ValueError('Mixed raw and depth-enriched rows; enrich full history first')
    if all(annotated):
        return [{**r, 'repeat_topic': r['prior_session_depth'] > 0}
                for r in unique_rows(rows)]
    return enrich_depth(rows)


def in_window(row: dict, start: str | None, end: str | None = None) -> bool:
    ts = row.get('ts') or ''
    return (start is None or ts >= start) and (end is None or ts < end)


def correctness_on_seen(outcomes: list[dict]) -> dict:
    """A — engaged-without-correction (useful) rate over repeat-topic injects only."""
    repeats = [r for r in with_depth(outcomes) if r['repeat_topic']]
    useful = sum(1 for r in repeats if outcome_verdict(r) == 'useful')
    n = len(repeats)
    return {'repeat_injects': n, 'useful': useful, 'rate': (useful / n) if n else None,
            'ci': wilson(useful, n)}


# The reopened collection window's pre-registered read point (measurement-v2 Task 10):
# depth>=1 HIGH n >= 40 — enough for a ±15pp Wilson CI on the repeat bucket, which is
# the bucket the compounding thesis actually turns on.
WINDOW_TARGET = 40


def window_open_date() -> str | None:
    """ISO date the reopened collection window was declared open, or None.

    Source of truth is state/window_open; KB_WINDOW_OPEN overrides it (testing, and
    a dry-run before Task 10 commits the marker)."""
    env = (os.environ.get('KB_WINDOW_OPEN') or '').strip()
    if env:
        return env
    try:
        return WINDOW_MARKER.read_text().strip() or None
    except OSError:
        return None


def window_progress(outcomes: list[dict], window_start: str | None,
                    window_end: str | None = None) -> dict:
    """How close the reopened window is to its first compounding read.

    Counts HIGH-tier rows *inside* the window whose topic is a repeat. Depth is
    computed over the FULL history first and only then filtered to the window: a
    topic first touched before the window is still a repeat when it recurs inside it
    — depth is a property of the topic's history, not of the window.

    ``window_start`` is an ISO date/timestamp (prefix-compared against row ts); None
    means no window has been declared open yet, which is 0 and never ready."""
    if not window_start:
        return {'n': 0, 'target': WINDOW_TARGET, 'ready': False, 'window_start': None}
    n = sum(1 for r in with_depth(outcomes)
            if r['repeat_topic'] and r.get('tier') == 'high'
            and in_window(r, window_start, window_end))
    return {'n': n, 'target': WINDOW_TARGET, 'ready': n >= WINDOW_TARGET,
            'window_start': window_start}


def compounding_curve(outcomes: list[dict]) -> list[dict]:
    """D — engagement/useful rate per prior-session-depth bucket. Empty buckets omitted
    (reporting a 0/0 bucket would read as a real measurement; it isn't)."""
    buckets: dict[str, dict] = {}
    for r in with_depth(outcomes):
        label = _bucket_for(r['prior_session_depth'])
        b = buckets.setdefault(label, {'bucket': label, 'n': 0, 'engaged': 0, 'useful': 0})
        b['n'] += 1
        verdict = outcome_verdict(r)
        if verdict in ('useful', 'harmful'):    # both mean the model engaged
            b['engaged'] += 1
        if verdict == 'useful':
            b['useful'] += 1
    order = {label: i for i, (label, _, _) in enumerate(DEPTH_BUCKETS)}
    rows = sorted(buckets.values(), key=lambda b: order[b['bucket']])
    for b in rows:
        b['engaged_rate'] = round(b['engaged'] / b['n'], 4) if b['n'] else None
        b['useful_rate'] = round(b['useful'] / b['n'], 4) if b['n'] else None
        b['useful_ci'] = wilson(b['useful'], b['n'])
    return rows


def signals_for(outcomes: list[dict]) -> dict:
    """A + D for ONE tier population. Never call on mixed tiers."""
    outcomes = with_depth(outcomes)
    useful = sum(1 for r in outcomes if outcome_verdict(r) == 'useful')
    return {
        'n': len(outcomes),
        'useful': useful,
        'useful_rate': round(useful / len(outcomes), 4) if outcomes else None,
        'useful_ci': wilson(useful, len(outcomes)),
        'correctness_on_seen': correctness_on_seen(outcomes),
        'compounding_curve': compounding_curve(outcomes),
    }


def scorecard(outcomes: list[dict], recall_stats: dict | None = None, *,
              window_start: str | None = None, window_end: str | None = None,
              history: list[dict] | None = None, judge_version: str | None = None) -> dict:
    """Tier-stratified scorecard. HIGH = injected population (the thesis's subject);
    MODERATE = advertised-only control arm (organic recurrence, no memory in context).
    injection_lift retains the descriptive all-depth difference; the registered
    repeat-only comparison is repeat_injection_lift. history supplies the full
    accrual population; analysis rows are aligned to it before window selection.
    Signal B uses the symmetric correction-population stats from recall_miss.stats()."""
    # One history defines exposure for both the tripwire and the readout. Judge
    # annotations may lag or contain reconstructed events absent from that log.
    # Match only identical events and scope; expose every exclusion as coverage.
    history_input = outcomes if history is None else history
    canonical = with_depth(history_input)
    by_key = {event_key(r): r for r in canonical}
    selected = current_rows(outcomes, judge_version) if judge_version else outcomes
    aligned = []
    coverage = {'analysis_only': 0, 'metadata_mismatch': 0}
    for row in unique_rows(selected):
        if not in_window(row, window_start, window_end):
            continue
        source = by_key.get(event_key(row))
        if source is None:
            coverage['analysis_only'] += 1
            continue
        if any(row.get(f) != source.get(f) for f in ('project', 'tier')):
            coverage['metadata_mismatch'] += 1
            continue
        aligned.append({**row, 'prior_session_depth': source['prior_session_depth'],
                        'repeat_topic': source['repeat_topic']})
    outcomes = aligned
    population = [r for r in canonical if in_window(r, window_start, window_end)]
    eligible = sum(r.get('tier') == 'high' and r['repeat_topic'] for r in population)
    covered = sum(r.get('tier') == 'high' and r['repeat_topic'] for r in outcomes)
    coverage.update(history_rows=len(population), analyzed_rows=len(outcomes),
                    high_repeat_eligible=eligible, high_repeat_analyzed=covered,
                    high_repeat_unjudged=eligible - covered,
                    duplicate_history_rows=len(history_input) - len(canonical),
                    duplicate_analysis_rows=len(selected) - len(unique_rows(selected)))
    high = [r for r in outcomes if r.get('tier') == 'high']
    moderate = [r for r in outcomes if r.get('tier') == 'moderate']
    hi, mo = signals_for(high), signals_for(moderate)
    ha, ma = hi['correctness_on_seen'], mo['correctness_on_seen']
    repeat_lift = (round(ha['rate'] - ma['rate'], 4)
                   if ha['rate'] is not None and ma['rate'] is not None else None)
    lift = None
    if hi['useful_rate'] is not None and mo['useful_rate'] is not None:
        lift = round(hi['useful_rate'] - mo['useful_rate'], 4)
    if recall_stats is None:
        recall = {'available': False, 'needs': 'recall_miss.stats() (Task 6)'}
    else:
        h, m = recall_stats['hits'], recall_stats['misses']
        recall = {
            'available': True, 'hits': h, 'misses': m,
            'unattributable': recall_stats.get('unattributable', 0),
            'recall': round(h / (h + m), 4) if (h + m) else None,
            'ci': wilson(h, h + m),
            'note': 'correction-grounded, correction population only',
        }
    return {
        'n_outcomes': len(outcomes),
        'window': {'start': window_start, 'end_exclusive': window_end},
        'judge_version': judge_version,
        'coverage': coverage,
        'by_tier': {'high': hi, 'moderate': mo},
        'injection_lift': {'lift': lift, 'high_ci': hi['useful_ci'],
                           'moderate_ci': mo['useful_ci']},
        'repeat_injection_lift': {'lift': repeat_lift, 'high_ci': ha['ci'],
                                  'moderate_ci': ma['ci']},
        'recall': recall,
        'cost': {'available': False, 'needs': 'per-task token logging'},
    }


def read_outcomes(path: Path | str = OUTCOME_PATH) -> list[dict]:
    """Parse injection_outcomes.jsonl; skip blank/malformed lines (fail-open)."""
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


def _fmt_ci(ci) -> str:
    return f" ({ci[0]:.0%}–{ci[1]:.0%})" if ci else ""


def _format(card: dict) -> str:
    lines = [f"Production scorecard — {card['n_outcomes']} outcomes "
             f"(HIGH = injected; MODERATE = advertised-only control)"]
    window = card['window']
    if window['start'] or window['end_exclusive']:
        lines.append(f"Window: {window['start'] or 'beginning'} <= ts < "
                     f"{window['end_exclusive'] or 'now'}; depth from full history")
    c = card['coverage']
    lines.append(f"HIGH repeats: accrued {c['high_repeat_eligible']}; "
                 f"analyzed {c['high_repeat_analyzed']}; "
                 f"unjudged/excluded {c['high_repeat_unjudged']}")
    lines.append(f"Analysis exclusions: {c['analysis_only']} absent from accrual history; "
                 f"{c['metadata_mismatch']} scope/tier mismatches")
    lines.append(f"Duplicate log writes removed (full input): "
                 f"{c['duplicate_history_rows']} accrual; {c['duplicate_analysis_rows']} analysis")
    for tier in ('high', 'moderate'):
        s = card['by_tier'][tier]
        a = s['correctness_on_seen']
        ur = f"{s['useful_rate']:.0%}" if s['useful_rate'] is not None else "n/a"
        lines += ["", f"[{tier.upper()}]  n={s['n']}  useful {ur}{_fmt_ci(s['useful_ci'])}",
                  "  A · correctness-on-seen: "
                  + (f"{a['useful']}/{a['repeat_injects']} = {a['rate']:.0%}"
                     if a['rate'] is not None else "n/a (no repeat injects)"),
                  "  D · compounding curve:   depth     n   useful   95% CI"]
        for b in s['compounding_curve']:
            ur = f"{b['useful_rate']:.0%}" if b['useful_rate'] is not None else "  - "
            lines.append(f"        {b['bucket']:<6} {b['n']:>4}   {ur:>5} {_fmt_ci(b['useful_ci'])}")
    lf = card['repeat_injection_lift']
    lines += ["", "injection lift, depth>=1 (high − moderate useful rate): "
              + (f"{lf['lift']:+.0%}" if lf['lift'] is not None else "n/a")
              + f"   high{_fmt_ci(lf['high_ci'])} vs moderate{_fmt_ci(lf['moderate_ci'])}"]
    lf = card['injection_lift']
    lines += ["injection lift, all depths (descriptive): "
              + (f"{lf['lift']:+.0%}" if lf['lift'] is not None else "n/a")
              + f"   high{_fmt_ci(lf['high_ci'])} vs moderate{_fmt_ci(lf['moderate_ci'])}"]
    rec = card['recall']
    if rec['available']:
        r = f"{rec['recall']:.0%}" if rec['recall'] is not None else "n/a"
        lines.append(f"B · recall: {rec['hits']}/{rec['hits'] + rec['misses']} = {r}"
                     f"{_fmt_ci(rec.get('ci'))}  "
                     f"(misses={rec['misses']}, unattributable={rec['unattributable']})")
    else:
        lines.append(f"B · recall: unavailable — {rec['needs']}")
    lines.append(f"C · cost:   unavailable — {card['cost']['needs']}")
    return "\n".join(lines)


def current_rows(rows: list[dict], judge_version: str) -> list[dict]:
    """Analysis filter for judged rows. Key on the PAIR (scorer_version, judge_version):
    during a bounded re-judge the dataset legitimately holds mixed judge_versions on
    scorer_version-3 rows, and keying on scorer_version==3 alone silently blends two
    judges — the exact thing versioning exists to prevent."""
    return [r for r in rows
            if r.get('scorer_version') == 3 and r.get('judge_version') == judge_version]


def main() -> None:
    ap = argparse.ArgumentParser(description='Tier-stratified production scorecard.')
    default_log = RESCORED_PATH if RESCORED_PATH.exists() else OUTCOME_PATH
    ap.add_argument('--log', help='analysis log; an explicit log also supplies history unless --history-log is set')
    ap.add_argument('--history-log', help='canonical accrual log used for event identity and full-history depth')
    ap.add_argument('--window', type=_iso_bound, metavar='START',
                    help='inclusive ISO date/timestamp window start')
    ap.add_argument('--until', type=_iso_bound, metavar='END',
                    help='exclusive ISO date/timestamp window end')
    from engagement_judge import JUDGE_VERSION
    ap.add_argument('--judge-version', default=JUDGE_VERSION,
                    help='analysis requires scorer_version=3 and this judge version')
    ap.add_argument('--all-versions', action='store_true',
                    help='diagnostic only: include mixed instruments (not a registered readout)')
    ap.add_argument('--json', action='store_true', help='print the report as JSON')
    ap.add_argument('--window-count', action='store_true',
                    help="print the reopened window's depth>=1 HIGH count on stdout "
                         '(the compounding-read tripwire input); writes nothing')
    args = ap.parse_args()
    if args.window and args.until and args.until <= args.window:
        ap.error('--until must be later than --window')
    if args.window_count:
        # Explicit --log must be honored even when it equals default_log.
        path = args.history_log or args.log or OUTCOME_PATH
        outcomes = read_outcomes(path)
        p = window_progress(outcomes, args.window or window_open_date(), args.until)
        print(f"window {p['window_start'] or '(not open)'} · depth>=1 HIGH "
              f"{p['n']}/{p['target']} · accrual {path}", file=sys.stderr)
        print(p['n'])
        return
    log_path = args.log or default_log
    history_path = args.history_log or args.log or OUTCOME_PATH
    outcomes = read_outcomes(log_path)
    history = read_outcomes(history_path)
    from collections import Counter
    pairs = Counter(
        (r.get('scorer_version') if 'scorer_version' in r else 1, r.get('judge_version'))
        for r in outcomes)
    def _pair_label(p):
        sv, jv = p
        return f"v3/{jv}" if sv == 3 else (f"v{sv}" if sv != 1 else "v1(untagged)")
    mix = ' · '.join(f"{_pair_label(p)}: {n}" for p, n in sorted(
        pairs.items(), key=lambda kv: str(kv[0]), reverse=True))
    # Recall uses a separate correction population with no matching time selector.
    # Never present its all-time value as a window measurement (or consult live
    # retrieval when the caller explicitly requested an isolated log).
    rstats = None
    if not (args.window or args.until or args.log or args.history_log):
        try:
            import recall_miss
            rstats = recall_miss.stats()
        except Exception:
            pass
    card = scorecard(outcomes, recall_stats=rstats, history=history,
                     window_start=args.window, window_end=args.until,
                     judge_version=None if args.all_versions else args.judge_version)
    if rstats is None and (args.window or args.until):
        card['recall']['needs'] = 'window-scoped correction statistics (all-time recall omitted)'
    card['analysis_log'] = str(log_path)
    card['history_log'] = str(history_path)
    card['instrument'] = 'mixed (diagnostic)' if args.all_versions else f'v3/{args.judge_version}'
    if args.json:
        print(json.dumps(card, indent=2))
        return
    print(f"[reading {log_path}; accrual history {history_path}]")
    print(f"[rows: {len(outcomes)} · {mix}]")
    print(f"[instrument: {card['instrument']}]")
    print(_format(card))


def _iso_bound(value: str) -> str:
    """Logs use local, offset-free ISO timestamps; avoid lexical timezone traps."""
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError('expected an ISO date or timestamp') from e
    if parsed.tzinfo is not None:
        raise argparse.ArgumentTypeError('use local timestamps without a timezone offset, matching the logs')
    return parsed.isoformat()


if __name__ == '__main__':
    main()
