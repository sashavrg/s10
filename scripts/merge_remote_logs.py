#!/usr/bin/env python3
"""Merge a remote host's append-only collection logs into this machine's logs.

**Why this exists.** The self-improvement loop's *collection* hooks
(`memory_inject_hook.py` at UserPromptSubmit, `injection_outcome.py` at SessionEnd)
fire wherever an *interactive* Claude Code session runs — including the always-on
home server, which is used interactively (not just as a pipeline runner). But the
nightly *consumers* (`audit_hooks.py`, `topic_records.py`, `scorecard.py`, ...) run
in exactly one place (the PC), reading this machine's `logs/*.jsonl`. `logs/` is
excluded from the cache rsync, so the server's collected rows would otherwise be
stranded and the loop would learn only from PC traffic. This step pulls the server's
collection logs and folds them into the PC's, so the single nightly consumes the
union. Harvested *corrections* already reach the PC via the `raw/inbox` symlink →
dashboard-inbox → the wrapper's inbox pull, so only the two JSONL logs need merging.

**Idempotence is the load-bearing property** (mirrors `topic_records.py` /
`audit_hooks.py`). Merge = append-only, deduped on the exact JSONL line. Server and
PC session_ids are disjoint, so a re-run appends nothing new, and the PC's own
SessionEnd scorer — which filters `memory_injection.jsonl` by the `--session` stem —
simply never matches the foreign rows. Re-running over an unchanged remote is a
byte-level no-op.

**Transcripts travel with the rows.** A merged outcome row is only *judgeable* if
its transcript is reachable on this machine — `engagement_judge.resolve_transcript`
looks in `~/.claude/projects` and `logs/transcript_archive`, neither of which ever
saw the server's sessions. Rows merged without their transcript still count toward
gate accrual but can never be labeled or judged (this stranded 8 of 35 fresh HIGH
rows at the 2026-07-29 ej9 accrual trip). So after merging the logs we fetch the
transcripts for every remote session referenced by `injection_outcomes.jsonl` that
doesn't already resolve locally, into `logs/transcript_archive/<remote-project-dir>/`
— the same retention-proof archive the local scorer falls back to. Scope is the
outcome log because that is what the judge/gate consumers key off; already-resolvable
sessions are skipped, so a steady-state run makes no SSH call at all.

Runs on the PC only (the wrapper gates it on `KB_DASHBOARD_DIR` being unset). Stdlib
only, apart from the transcript resolver imported from `engagement_judge` (itself
stdlib) so the "is it reachable?" rule can never drift from the consumers'. Never
aborts the pipeline: a missing remote file or an SSH failure is logged and skipped,
and `main()` exits 0 unless given a bad flag.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

from engagement_judge import resolve_transcript

BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / 'logs'
# Raw pulled copies are kept here for provenance/debugging (state/ is gitignored data).
STAGING_ROOT = BASE_DIR / 'state' / 'remote_logs'

# The append-only collection logs worth merging. Corrections travel a different path
# (raw/inbox symlink), and shadow_retrieval.jsonl is analysis-only; keep this list to
# the two logs the governance consumers actually read.
MERGE_LOGS = ('memory_injection.jsonl', 'injection_outcomes.jsonl')
# The log whose session_ids decide which transcripts are worth fetching.
OUTCOME_LOG = 'injection_outcomes.jsonl'
# Fetched transcripts land here, mirroring the remote's project-dir layout;
# resolve_transcript globs `*/<session_id>.jsonl` under it. Session ids are
# disjoint across hosts, so mirroring the remote dir name cannot collide.
ARCHIVE_DIR = BASE_DIR / 'logs' / 'transcript_archive'

REMOTE_HOST = os.environ.get('KB_REMOTE_HOST', '')
REMOTE_USER = os.environ.get('KB_REMOTE_USER', os.environ.get('USER', ''))
REMOTE_KB_DIR = os.environ.get('KB_REMOTE_KB_DIR', '')
REMOTE_PROJECTS_DIR = os.environ.get('KB_REMOTE_PROJECTS_DIR', '')
SSH_CMD = os.environ.get(
    'KB_SSH_CMD', 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new')


def new_rows(existing_lines: list[str], incoming_lines: list[str]) -> list[str]:
    """Return the incoming rows not already present in ``existing`` (exact-line dedup).

    Pure and total. Blank lines are dropped; duplicates *within* ``incoming`` collapse
    to their first occurrence; order is preserved. Dedup is on the stripped line — JSONL
    rows carry a unique ts (+ session_id), so this is exact and idempotent without
    parsing JSON or assuming any field is present.
    """
    seen = {ln.strip() for ln in existing_lines if ln.strip()}
    out: list[str] = []
    for ln in incoming_lines:
        s = ln.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding='utf-8').splitlines()


def _sanitize(host: str) -> str:
    return host.replace(':', '_').replace('/', '_')


def pull_remote_logs(staging_dir: Path, host: str) -> tuple[bool, str]:
    """rsync the whitelisted logs from ``host`` into ``staging_dir``.

    One connection, filtered so absent remote files are silently skipped (not errors).
    Returns (ok, detail). ok=False on an SSH/rsync failure (e.g. host unreachable).
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    src = f'{REMOTE_USER}@{host}:{REMOTE_KB_DIR}/logs/'
    cmd = ['rsync', '-az', '-e', SSH_CMD]
    for name in MERGE_LOGS:
        cmd += ['--include', name]
    cmd += ['--exclude', '*', src, str(staging_dir) + '/']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f'rsync failed: {e}'
    if proc.returncode != 0:
        return False, f'rsync exit {proc.returncode}: {proc.stderr.strip().splitlines()[-1:] or ""}'
    return True, 'ok'


def outcome_sessions(lines: list[str]) -> set[str]:
    """Distinct session_ids referenced by staged outcome rows.

    Pure and total: blank lines, unparseable rows and rows without a session_id
    are ignored (the log is append-only from a live hook; a torn last line must
    not cost us the whole pull)."""
    out: set[str] = set()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get('session_id'):
            out.add(row['session_id'])
    return out


def sessions_missing_transcripts(sessions, resolve_fn) -> list[str]:
    """Sorted session_ids with no locally-resolvable transcript — the fetch list.

    ``resolve_fn`` is the consumers' own resolver, so "reachable" means exactly
    what the judge/gate mean by it."""
    return sorted(sid for sid in sessions if resolve_fn(sid) is None)


def transcript_fetch_list(listing: list[str], wanted) -> list[str]:
    """Map a remote ``find -printf '%P\\n'`` listing to the relative paths to pull.

    Listing entries look like ``<project-dir>/<session_id>.jsonl``. Entries outside
    the wanted set, malformed entries, and top-level files are dropped; a session
    appearing under two project dirs resolves to the first by sorted path so the
    file list is deterministic."""
    want = set(wanted)
    picked: dict[str, str] = {}
    for entry in sorted(ln.strip() for ln in listing):
        if not entry or '/' not in entry or not entry.endswith('.jsonl'):
            continue
        sid = entry.rsplit('/', 1)[1][:-len('.jsonl')]
        if sid in want and sid not in picked:
            picked[sid] = entry
    return sorted(picked.values())


def list_remote_transcripts(host: str) -> tuple[bool, list[str]]:
    """One SSH call listing the remote's transcripts, relative to the projects dir."""
    find = (f"find {shlex.quote(REMOTE_PROJECTS_DIR)} -maxdepth 2 -name '*.jsonl' "
            f"-printf '%P\\n'")
    cmd = shlex.split(SSH_CMD) + [f'{REMOTE_USER}@{host}', find]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, [f'ssh failed: {e}']
    if proc.returncode != 0:
        return False, [f'ssh exit {proc.returncode}']
    return True, proc.stdout.splitlines()


def pull_remote_transcripts(host: str, sessions, dry_run: bool) -> tuple[int, str]:
    """Fetch the transcripts for ``sessions`` into the archive. Returns (n, detail).

    Never raises: an unreachable host or a vanished transcript is reported and
    skipped, exactly like the log pull."""
    missing = sessions_missing_transcripts(sessions, resolve_transcript)
    if not missing:
        return 0, 'all resolvable'
    ok, listing = list_remote_transcripts(host)
    if not ok:
        return 0, listing[0]
    paths = transcript_fetch_list(listing, missing)
    if not paths:
        return 0, f'{len(missing)} unresolvable, none on remote'
    if dry_run:
        return len(paths), 'dry-run'
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-az', '-e', SSH_CMD, '--files-from=-',
           f'{REMOTE_USER}@{host}:{REMOTE_PROJECTS_DIR}/', str(ARCHIVE_DIR) + '/']
    try:
        proc = subprocess.run(cmd, input='\n'.join(paths) + '\n',
                              capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 0, f'rsync failed: {e}'
    if proc.returncode != 0:
        return 0, f'rsync exit {proc.returncode}'
    return len(paths), 'ok'


def merge_one(name: str, staging_dir: Path, dry_run: bool) -> int:
    """Fold staged remote ``name`` into ``logs/name``. Returns rows appended."""
    staged = staging_dir / name
    if not staged.exists():
        return 0
    dest = LOGS_DIR / name
    additions = new_rows(_read_lines(dest), _read_lines(staged))
    if additions and not dry_run:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with dest.open('a', encoding='utf-8') as fh:
            for row in additions:
                fh.write(row + '\n')
    return len(additions)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='pull + report would-append counts, write nothing')
    ap.add_argument('--host', default=REMOTE_HOST, help='override KB_REMOTE_HOST')
    ap.add_argument('--no-transcripts', action='store_true',
                    help='merge the logs only; skip the transcript fetch')
    args = ap.parse_args()
    host = args.host
    if not host or not REMOTE_KB_DIR:
        print('merge-remote-logs: SKIP — KB_REMOTE_HOST/KB_REMOTE_KB_DIR not configured')
        return 0

    staging_dir = STAGING_ROOT / _sanitize(host)
    ok, detail = pull_remote_logs(staging_dir, host)
    if not ok:
        print(f'merge-remote-logs: SKIP {host} — {detail}')
        return 0  # never abort the pipeline over a sync hiccup

    parts = []
    for name in MERGE_LOGS:
        n = merge_one(name, staging_dir, args.dry_run)
        parts.append(f'{name}+{n}')
    verb = 'would merge' if args.dry_run else 'merged'

    if not args.no_transcripts and not REMOTE_PROJECTS_DIR:
        print('merge-remote-logs: transcripts SKIP — KB_REMOTE_PROJECTS_DIR not configured')
    elif not args.no_transcripts:
        sessions = outcome_sessions(_read_lines(staging_dir / OUTCOME_LOG))
        n_tx, detail = pull_remote_transcripts(host, sessions, args.dry_run)
        parts.append(f'transcripts+{n_tx} ({detail})')

    print(f'merge-remote-logs: {verb} from {host} — ' + ', '.join(parts))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
