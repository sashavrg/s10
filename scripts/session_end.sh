#!/usr/bin/env bash
# SessionEnd worker: run the two per-session jobs in order, detached by the
# caller (claude_session_hook.sh). Sequential — not parallel — so the outcome
# scorer can see a correction harvested THIS session.
#
#   1) harvest_session.py   — extract explicit user corrections (cloud LLM).
#      TIME-BOXED: a cloud hang was observed at 900s; bound it so it can never
#      starve step 2 (or pile up forever). A timed-out harvest just means this
#      session's corrections wait for a later --force run; a missed correction
#      costs nothing.
#   2) injection_outcome.py — score this session's memory injections into
#      logs/injection_outcomes.jsonl (the utility/reward signal). Local + fast.
#
# Both fail open; this wrapper never blocks session teardown.

set -uo pipefail

KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TRANSCRIPT="${1:?usage: session_end.sh <transcript_path> [project]}"
PROJECT="${2:-none}"

SESSION_ID="$(basename "$TRANSCRIPT")"
SESSION_ID="${SESSION_ID%.jsonl}"

# Cloud auth + KB_* overrides for the harvester (mirrors claude_session_hook.sh
# so this is correct when run standalone too).
if [ -f "$KB_DIR/.env" ]; then
  set -a; . "$KB_DIR/.env"; set +a
fi

VENV_PY="$KB_DIR/.venv/bin/python"
PY="$VENV_PY"
[ -x "$VENV_PY" ] || PY="python3"

timeout "${KB_HARVEST_TIMEOUT:-600}" "$PY" "$KB_DIR/scripts/harvest_session.py" \
  "$TRANSCRIPT" --project "$PROJECT" >> "$KB_DIR/logs/harvest.log" 2>&1 || true

"$PY" "$KB_DIR/scripts/injection_outcome.py" \
  --transcript "$TRANSCRIPT" --session "$SESSION_ID" --project "$PROJECT" \
  >> "$KB_DIR/logs/injection_outcome.log" 2>&1 || true
