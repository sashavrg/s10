#!/usr/bin/env bash
# KB pipeline heartbeat / missed-run detector.
#
# The nightly pipeline only ever ALERTS when it actually runs. If the PC is
# asleep/off at 22:00 (or cron is broken), the nightly silently doesn't run and
# the KB + injected memory drift stale with zero signal — this already happened
# for the 2026-06-27 run. Run this from cron a few times a day, INDEPENDENTLY of
# the pipeline, to catch a missed or failed nightly.
#
# Suggested crontab (checks at 10:30 — a slot the machine is usually awake for;
# 30h window flags a missed 22:00 run by then):
#   30 10 * * *  /path/to/s10/scripts/heartbeat.sh
#
# ALSO invoked from run_pipeline.sh step 8: the original 09:00 slot
# never fired (machine asleep every morning) and the upkeep block silently didn't
# run for three weeks. The 22:00 pipeline slot is the primary trigger; the
# morning cron (moved 09:00 -> 10:30) is an idempotent bonus.
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

# Gate-set upkeep (ej9 gate addendum A5): archive transcripts before retention
# deletes them, then refresh the rescored analysis file. Runs here so the 10:35
# gate-accrual check always counts fresh, retention-proofed data. Logged, never
# fatal to the heartbeat.
#
# JUDGE PASS: ON since 2026-07-29 — the stage-1 gate PASSED (n=35, P=0.947/R=0.783),
# which is the pre-committed condition for writing judge verdicts into the analysis
# dataset. The cap bounds cloud spend per run: each judged row is one claude-sonnet-5
# call (the gate-validated model — see engagement_judge.production_generate). Backlog
# at flip time was 903 unjudged v2 rows, so ~23 nights to drain at 40/run; rows beyond
# the cap stay v2 for the next night, and a cloud failure leaves a row at v2 rather
# than faking a 3. Override with KB_JUDGE_MAX_CALLS (0 disables the judging loop;
# grafting of previously-judged rows runs unconditionally either way).
JUDGE_MAX_CALLS="${KB_JUDGE_MAX_CALLS:-40}"
{
  echo "[$(date -Iseconds)] gate-set upkeep (judge cap ${JUDGE_MAX_CALLS})"
  "$KB_DIR/scripts/archive_transcripts.sh"
  KB_JUDGE_MAX_CALLS="$JUDGE_MAX_CALLS" "$PY" "$KB_DIR/scripts/rescore_outcomes.py"
  # One-shot notice when the reopened window hits its pre-registered read point.
  # Silent until state/window_open exists (Task 10 Step 4), and silent forever after
  # it fires once. Runs after the rescore so it counts the freshest rows.
  "$KB_DIR/scripts/compounding_read_check.sh"
} >> "$KB_DIR/logs/heartbeat.log" 2>&1 || true

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
