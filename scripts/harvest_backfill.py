#!/usr/bin/env python3
"""Bounded backlog driver for the correction harvester (collection-compatible).

The SessionEnd hook harvests sessions as they end, but ~3,200 transcripts predate
it (or ended while the machine was off). This drives `harvest_session.harvest()`
over that backlog a bounded slice per night — the judge-cap pattern: capped cloud
spend, resumable, never fatal, rides the 22:00 pipeline.

Ordering is NEWEST-FIRST: recent sessions' corrections are the most likely to
still be true, and the eval-case miners feed on recent traffic first.

Everything hard is delegated to `harvest()` itself, which is idempotent (content-
hashed), records state ONLY on success, and skips without state on cloud failure
— so re-running is always safe and an outage costs nothing but time. This driver
adds only: enumeration (live ~/.claude/projects + the retention-proof archive,
live copy wins), the cap, and a stop-early rule so an outage doesn't burn the
whole cap on doomed calls.

Explicitly sanctioned in-window (2026-08-05 registration, collection-compatible
list): "transcript backfill — grows Gate-1; cannot backfill the curve".

  python scripts/harvest_backfill.py                # up to KB_HARVEST_BACKFILL_CAP (60)
  python scripts/harvest_backfill.py --cap 5        # small manual slice
  python scripts/harvest_backfill.py --dry-run      # count only, no cloud calls
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harvest_session import DEFAULT_SPEC, HARVEST_STATE_PATH, harvest

BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_ROOT = Path.home() / '.claude' / 'projects'
ARCHIVE_ROOT = BASE_DIR / 'logs' / 'transcript_archive'

DEFAULT_CAP = 60          # sonnet calls/night; KB_HARVEST_BACKFILL_CAP overrides
STOP_AFTER_SKIPS = 3      # consecutive cloud skips -> stop, retry next night

# Machine-session directories, never harvested: '-tmp' is where the KB's OWN
# headless `claude -p` calls transcribe (kb.claude_code_payload runs from
# tempfile.gettempdir(); KB_HEADLESS -> no hooks, no human corrections). They
# were 92% of the raw backlog, and each harvest call CREATES one — unexcluded,
# the queue feeds itself (observed 2026-08-20: 60/60 calls burned for nothing).
EXCLUDE_DIRS = {'-tmp'}


def _harvested_ids() -> set[str]:
    """Session ids already in the harvest state (read via THIS module's
    HARVEST_STATE_PATH so tests can repoint it; harvest() itself keeps writing
    through harvest_session's own accessors)."""
    try:
        return set(json.loads(HARVEST_STATE_PATH.read_text()).get('sessions', {}))
    except (OSError, ValueError):
        return set()


def backlog() -> list[Path]:
    """Unharvested transcripts, newest-first. Live copy wins over the archive
    (same session id = same content lineage; live may be newer)."""
    done = _harvested_ids()
    by_sid: dict[str, Path] = {}
    for root in (ARCHIVE_ROOT, LIVE_ROOT):          # live second -> live wins
        if root.exists():
            for p in root.glob('*/*.jsonl'):
                if p.parent.name in EXCLUDE_DIRS:
                    continue
                by_sid[p.stem] = p
    todo = [p for sid, p in by_sid.items() if sid not in done]
    return sorted(todo, key=lambda p: p.stat().st_mtime, reverse=True)


def run(cap: int, spec: str, dry_run: bool = False) -> dict:
    items = backlog()
    s = {'attempted': 0, 'ok': 0, 'skipped': 0, 'errors': 0,
         'corrections_written': 0, 'remaining': len(items)}
    if dry_run:
        return s
    consecutive_skips = 0
    for tx in items[:cap]:
        res = harvest(tx, project=tx.parent.name, spec=spec)
        s['attempted'] += 1
        s['remaining'] -= 1
        status = res.get('status')
        if status == 'ok':
            s['ok'] += 1
            s['corrections_written'] += res.get('corrections_written', 0)
            consecutive_skips = 0
        elif status == 'skipped':
            s['skipped'] += 1
            consecutive_skips += 1
            if consecutive_skips >= STOP_AFTER_SKIPS:
                s['remaining'] = len(items) - s['attempted']
                break
        else:                                        # 'empty' / 'error'
            s['errors'] += 1
            consecutive_skips = 0
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description='Bounded correction-harvest backfill.')
    ap.add_argument('--cap', type=int,
                    default=int(os.environ.get('KB_HARVEST_BACKFILL_CAP', DEFAULT_CAP)))
    ap.add_argument('--spec', default=DEFAULT_SPEC)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    s = run(cap=max(0, args.cap), spec=args.spec, dry_run=args.dry_run)
    verb = 'backlog' if args.dry_run else 'harvested'
    print(f"harvest-backfill: {verb} — attempted {s['attempted']} · ok {s['ok']} · "
          f"skipped {s['skipped']} · errors {s['errors']} · "
          f"corrections written {s['corrections_written']} · remaining {s['remaining']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
