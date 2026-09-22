#!/usr/bin/env bash
# Codex SessionEnd hook. Start the durable KB worker and return within Codex's
# three-second SessionEnd ceiling; the worker itself remains fail-open.
set -uo pipefail

KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# A future Codex-backed batch backend may invoke Codex non-interactively. Those
# sessions are machinery, not human feedback, and must not spawn harvest jobs.
[ -n "${KB_HEADLESS:-}" ] && exit 0
PAYLOAD="$(cat)"
PY="$KB_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

read_field() {
  printf '%s' "$PAYLOAD" | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('$1', ''))" 2>/dev/null
}

TRANSCRIPT="$(read_field transcript_path)"
CWD="$(read_field cwd)"
[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || exit 0

if [ -n "$CWD" ]; then
  PROJECT="$(basename "$CWD")"
else
  PROJECT="unknown"
fi

nohup "$KB_DIR/scripts/session_end.sh" "$TRANSCRIPT" "$PROJECT" \
  >> "$KB_DIR/logs/codex_session_end.log" 2>&1 &
