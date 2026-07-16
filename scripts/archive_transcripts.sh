#!/usr/bin/env bash
# Archive Claude Code transcripts before the 30-day retention cleanup deletes them.
#
# Transcripts are the raw material for retroactive outcome re-scoring (the #1e
# engagement judge re-derives `engaged` from them) and for the KB/eval backfill;
# once retention expires them they are irreplaceable. Approved 2026-07-02
# (collection-compatible: read/copy only, touches no live loop).
#
# Destination is logs/ ON PURPOSE: gitignored AND excluded from the nightly
# server rsync (run_pipeline.sh) — transcripts contain client content and must
# never leave this machine.
#
# Idempotent (rsync -a): safe to re-run any time. Not wired to cron/nightly yet —
# re-run manually until the judge phase decides the recurring home.
set -euo pipefail
KB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-$HOME/.claude/projects/}"
DEST="$KB_DIR/logs/transcript_archive/"
mkdir -p "$DEST"
rsync -a --include='*/' --include='*.jsonl' --exclude='*' "$SRC" "$DEST"
echo "archived: $(find "$DEST" -name '*.jsonl' | wc -l) transcripts ($(du -sh "$DEST" | cut -f1)) -> $DEST"
