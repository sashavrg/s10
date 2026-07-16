#!/usr/bin/env bash
# KB pipeline heartbeat / missed-run detector.
#
# The nightly pipeline only ever ALERTS when it actually runs. If the PC is
# asleep/off at 22:00 (or cron is broken), the nightly silently doesn't run and
# the KB + injected memory drift stale with zero signal — this already happened
# for the 2026-06-27 run. Run this from cron a few times a day, INDEPENDENTLY of
# the pipeline, to catch a missed or failed nightly.
#
# Suggested crontab (checks at 09:00; 30h window flags a missed 22:00 run by then):
#   0 9 * * *  /path/to/s10/scripts/heartbeat.sh
#
# Exit 0 always (cron-friendly). Quiet when healthy; alerts via Telegram + stderr
# when the last run is stale or errored. Tune the window with KB_HEARTBEAT_MAX_AGE_HOURS.

set -uo pipefail

KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$KB_DIR/.env" ] && { set -a; . "$KB_DIR/.env"; set +a; }

MAX_AGE_HOURS="${KB_HEARTBEAT_MAX_AGE_HOURS:-30}"
VENV_PY="$KB_DIR/.venv/bin/python"
PY="$VENV_PY"
[ -x "$VENV_PY" ] || PY="python3"

STATUS="$("$PY" - "$KB_DIR/state/sync_meta.json" "$MAX_AGE_HOURS" <<'PY'
import json, sys, datetime as dt
meta_path, max_age = sys.argv[1], float(sys.argv[2])
try:
    meta = json.loads(open(meta_path).read())
except FileNotFoundError:
    print("MISSING|no state/sync_meta.json — the pipeline may have never run here"); sys.exit(0)
except Exception as e:
    print(f"BADSTATE|could not read sync_meta.json: {e}"); sys.exit(0)
last = meta.get("last_attempt")
if not last:
    print("MISSING|sync_meta.json has no last_attempt"); sys.exit(0)
try:
    t = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
except Exception:
    print(f"BADTS|unparseable last_attempt: {last!r}"); sys.exit(0)
now = dt.datetime.now(t.tzinfo) if t.tzinfo else dt.datetime.now()
age_h = (now - t).total_seconds() / 3600
err = meta.get("last_error")
if age_h > max_age:
    print(f"STALE|last pipeline run was {age_h:.0f}h ago (> {max_age:.0f}h): {last}")
elif err:
    print(f"ERROR|last pipeline run reported an error: {err}")
else:
    print(f"OK|last pipeline run {age_h:.0f}h ago")
PY
)"

CODE="${STATUS%%|*}"
MSG="${STATUS#*|}"

notify() {
  { [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; } && return 0
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null || true
}

if [ "$CODE" != "OK" ]; then
  echo "[heartbeat] $CODE: $MSG" >&2
  notify "⚠️ KB heartbeat — $CODE: $MSG"
fi
exit 0
