#!/usr/bin/env python3
"""Task 9 burn-in probe: the rerank fallback rate, against the pre-committed
reopen threshold (operator-signed 2026-07-29, BEFORE burn-in data existed).

Definition: rate = rerank_timeout rows / rows that ATTEMPTED the judge, where an
attempt is a post-flip retrieval row whose scorer was rerank (retrieval_mode ==
'rerank') or that fell back FROM rerank (rerank_timeout == True). Rows without a
retrieval_mode predate the flip and are excluded — they say nothing about the
deploy. tier='error' rows (retrieve() crashed) are reported separately and never
enter the rate.

Threshold: window reopen requires rate < 0.15 over the 3-day burn-in. Below it,
rerank is live de facto and Task 10 proceeds with per-row scorer provenance
covering the residue; above it, the flip is fictional — the trigger to design a
faster rerank variant, which then goes through the FULL eval gate (golden set +
real-negative set) as a new scorer before deployment.

  python scripts/fallback_rate.py                      # all post-flip rows
  python scripts/fallback_rate.py --since 2026-07-30   # burn-in window only
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'

THRESHOLD = 0.15

# Prompt-length strata (chars). Prefill scales with prompt tokens and the rerank
# prompt embeds the user turn, so paste-turns are the predicted fallback cluster.
# Concentrated-in-long vs uniform argue for different fixes (a length-based
# early-fallback rule vs a smaller judge).
LENGTH_BUCKETS = (('short', 0, 500), ('medium', 501, 2000), ('long', 2001, None))


def _length_bucket(row: dict) -> str:
    n = row.get('prompt_chars')
    if not isinstance(n, int):
        return 'unknown'
    for label, lo, hi in LENGTH_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return label
    return 'unknown'


def _hour(row: dict) -> str:
    """'HH' from the row's ISO ts, 'unknown' when absent/malformed."""
    ts = row.get('ts') or ''
    hh = ts[11:13]
    return hh if len(hh) == 2 and hh.isdigit() else 'unknown'


def stats(rows: list[dict], since: str | None = None) -> dict:
    """Pure: fallback-rate accounting over injection-log rows — overall, by prompt
    length, and by hour-of-day. Hour + length together separate the two timeout
    causes: a cluster at a fixed hour (nightly pipeline loading another model, GPU
    contention) is a schedule collision; timeouts tracking long prompts across all
    hours are prefill physics. Short prompt + timeout = cold start/eviction.

    rate is None (never a fake 0% pass) when nothing attempted the judge."""
    if since:
        rows = [r for r in rows if (r.get('ts') or '') >= since]
    errors = sum(1 for r in rows if r.get('tier') == 'error')

    def _count(rs):
        timeouts = sum(1 for r in rs if r.get('rerank_timeout'))
        attempted = sum(1 for r in rs if r.get('retrieval_mode') == 'rerank') + timeouts
        return {'attempted': attempted, 'timeouts': timeouts,
                'rate': (timeouts / attempted) if attempted else None}

    overall = _count(rows)
    by_length = []
    for label in [b[0] for b in LENGTH_BUCKETS] + ['unknown']:
        c = _count([r for r in rows if _length_bucket(r) == label])
        if c['attempted']:
            by_length.append({'bucket': label, **c})
    by_hour = []
    for hh in sorted({_hour(r) for r in rows}):
        c = _count([r for r in rows if _hour(r) == hh])
        if c['attempted']:
            by_hour.append({'hour': hh, **c})
    rate = overall['rate']
    return {**overall, 'errors': errors, 'threshold': THRESHOLD,
            'below_threshold': (rate < THRESHOLD) if rate is not None else None,
            'by_length': by_length, 'by_hour': by_hour}


def brief(s: dict) -> str:
    """One-line form for the nightly Telegram notice: 'timeouts/attempts (rate)',
    'n/a' before any judge attempt, with an error flag that must never be silent
    (error rows are zero by construction — any count is a bug surfacing)."""
    if s['rate'] is None:
        out = 'n/a'
    else:
        out = f"{s['timeouts']}/{s['attempted']} ({s['rate']:.0%})"
    if s['errors']:
        out += f" ⚠{s['errors']}err"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description='Task 9 rerank fallback-rate probe.')
    ap.add_argument('--since', help='ISO date/ts floor (burn-in window start)')
    ap.add_argument('--log', default=str(LOG_PATH))
    ap.add_argument('--brief', action='store_true',
                    help='one line for the nightly notice; nothing else')
    args = ap.parse_args()
    rows = []
    try:
        for line in Path(args.log).read_text(errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    s = stats(rows, since=args.since)
    if args.brief:
        print(brief(s))
        return
    print(f"judge attempts: {s['attempted']} · timeouts (lexical fallback): "
          f"{s['timeouts']} · retrieve errors: {s['errors']}")
    if s['rate'] is None:
        print('fallback rate: n/a (no post-flip judge attempts yet)')
    else:
        verdict = 'BELOW threshold — reopen OK' if s['below_threshold'] else \
                  'ABOVE threshold — flip is fictional; faster variant via the eval gate'
        print(f"fallback rate: {s['rate']:.1%} vs {s['threshold']:.0%} -> {verdict}")
        for b in s['by_length']:
            print(f"  {b['bucket']:<8} attempts {b['attempted']:>4} · timeouts "
                  f"{b['timeouts']:>3} · rate {b['rate']:.1%}")
        print('by hour (timeout cluster at a fixed hour = schedule collision, '
              'not prefill physics):')
        for b in s['by_hour']:
            mark = '  <-- ' + '×' * b['timeouts'] if b['timeouts'] else ''
            print(f"  {b['hour']:>2}h attempts {b['attempted']:>4} · timeouts "
                  f"{b['timeouts']:>3} · rate {b['rate']:.1%}{mark}")


if __name__ == '__main__':
    main()
