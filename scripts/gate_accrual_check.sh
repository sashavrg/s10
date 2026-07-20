#!/usr/bin/env bash
# Gate-accrual tripwire (#1e round 2): Telegram-notify ONCE when fresh HIGH rows
# reach the full-power gate threshold (default 35; KB_GATE_HIGH_TARGET overrides).
#
# "Fresh HIGH" = tier=high rows with a session_id, union of the LIVE outcome log and
# the rescored file, whose (session_id, ts, injected) key is NOT in the calibration
# set — the same definition the gate dossier will use. Fires once (marker:
# state/gate_accrual_notified), then stays quiet. Read-only on all measurement data.
#
# Crontab (after heartbeat):  5 9 * * *  /path/to/s10/scripts/gate_accrual_check.sh
# Test the Telegram wiring:   ./scripts/gate_accrual_check.sh --test
# Print just COUNT/TARGET (no notify, no marker; for the nightly post-run notice):
#                             ./scripts/gate_accrual_check.sh --count-only
set -uo pipefail
KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$KB_DIR/.env" ] && { set -a; . "$KB_DIR/.env"; set +a; }
TARGET="${KB_GATE_HIGH_TARGET:-35}"
MARKER="$KB_DIR/state/gate_accrual_notified"
VENV_PY="$KB_DIR/.venv/bin/python"; PY="$VENV_PY"; [ -x "$VENV_PY" ] || PY="python3"

notify() {
  { [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; } && return 0
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null || true
}

if [ "${1:-}" = "--test" ]; then
  notify "🧪 KB gate tripwire — wiring test OK (target: fresh HIGH >= ${TARGET})"
  echo "[gate-accrual] test notification sent"
fi

COUNT="$("$PY" - "$KB_DIR" <<'PY'
import json, sys
from pathlib import Path
kb = Path(sys.argv[1])
def rows(p):
    out = []
    try:
        for l in p.read_text(errors='replace').splitlines():
            l = l.strip()
            if not l or l.startswith('#'): continue
            try: out.append(json.loads(l))
            except json.JSONDecodeError: continue
    except OSError: pass
    return out
cal = {(r.get('session_id'), r.get('ts'), r.get('injected'))
       for r in rows(kb / 'evals/fixtures/engagement_calibration_2026-07-03.jsonl')}
seen = set()
for p in (kb / 'logs/injection_outcomes.jsonl', kb / 'logs/injection_outcomes_rescored.jsonl'):
    for r in rows(p):
        if r.get('tier') != 'high' or not r.get('session_id'): continue
        k = (r['session_id'], r.get('ts'), r.get('injected'))
        if k not in cal: seen.add(k)
print(len(seen))
PY
)"

if [ "${1:-}" = "--count-only" ]; then
  echo "$COUNT/$TARGET"
  exit 0
fi

echo "[gate-accrual] fresh HIGH rows: $COUNT / $TARGET"
if [ "$COUNT" -ge "$TARGET" ] && [ ! -f "$MARKER" ]; then
  notify "🎯 KB gate accrual TRIPPED — fresh HIGH rows: ${COUNT} >= ${TARGET}. The ej9 gate set is fully powered: run 'python scripts/gate_dossier.py make' (addendum A4) and start the blind labeling session."
  date -Iseconds > "$MARKER"
  echo "[gate-accrual] notified + marker written"
fi
exit 0
