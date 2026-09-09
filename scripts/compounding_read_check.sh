#!/usr/bin/env bash
# Compounding-read tripwire: Telegram-notify ONCE when the reopened collection
# window reaches its pre-registered read point — the FIRST compounding read of the
# window is then due, and reading it late is how a window silently over-runs.
#
# Read point (measurement-v2 Task 10): depth>=1 HIGH n >= 40, i.e. enough repeat-topic
# HIGH rows for a ±15pp Wilson CI on the bucket the compounding thesis turns on. The
# count is delegated to `scorecard.py --window-count` — the same enrich_depth() the
# curve itself uses — so the tripwire and the read can never diverge (the gate-accrual
# tripwire's lesson: a reimplemented count trips on the wrong number).
#
# Quiet until the window exists: state/window_open is written by Task 10 Step 4 and
# holds one ISO date; with no marker the count is 0 and this never fires.
#
# Invoked from heartbeat.sh (itself run by the 22:00 pipeline + the 10:30 cron),
# so no cron entry of its own is required.
# Test the Telegram wiring:  ./scripts/compounding_read_check.sh --test
# Print just COUNT/TARGET:   ./scripts/compounding_read_check.sh --count-only
set -uo pipefail
KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$KB_DIR/.env" ] && { set -a; . "$KB_DIR/.env"; set +a; }
TARGET="${KB_WINDOW_READ_TARGET:-40}"
MARKER="$KB_DIR/state/compounding_read_notified"
VENV_PY="$KB_DIR/.venv/bin/python"; PY="$VENV_PY"; [ -x "$VENV_PY" ] || PY="python3"

notify() {
  { [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; } && return 0
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null || true
}

if [ "${1:-}" = "--test" ]; then
  notify "🧪 KB compounding-read tripwire — wiring test OK (target: depth≥1 HIGH >= ${TARGET})"
  echo "[compounding-read] test notification sent"
fi

COUNT="$("$PY" "$KB_DIR/scripts/scorecard.py" --window-count 2>/dev/null)"
case "$COUNT" in
  ''|*[!0-9]*)
    # Never notify off a count we could not compute.
    echo "[compounding-read] SKIP — could not compute the window count"
    [ "${1:-}" = "--count-only" ] && echo "?/$TARGET"
    exit 0 ;;
esac

if [ "${1:-}" = "--count-only" ]; then
  echo "$COUNT/$TARGET"
  exit 0
fi

echo "[compounding-read] window depth>=1 HIGH rows: $COUNT / $TARGET"
if [ "$COUNT" -ge "$TARGET" ] && [ ! -f "$MARKER" ]; then
  notify "📈 KB compounding read DUE — the reopened window has ${COUNT} >= ${TARGET} depth≥1 HIGH rows. Run 'python scripts/scorecard.py' and read signal D (compounding curve) against the pre-registered branches (SUPPORTED / NOT SUPPORTED / REVERSED / INCONCLUSIVE)."
  date -Iseconds > "$MARKER"
  echo "[compounding-read] notified + marker written"
fi
exit 0
