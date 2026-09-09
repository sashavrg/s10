#!/usr/bin/env bash
set -euo pipefail

KB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$KB_DIR"

# cron runs with a minimal PATH (typically /usr/bin:/bin) that omits
# ~/.local/bin, where the `claude` CLI is installed. Without this the
# cloud-backend guard's `command -v claude` fails under cron and the nightly
# silently degrades to local-only; it also lets kb.py's inherited `claude`
# subprocess resolve the binary. Harmless when these dirs are already present.
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

mkdir -p logs
exec > >(tee -a logs/pipeline.log) 2>&1

exec 9>/tmp/s10-pipeline.lock
if ! flock -n 9; then
  echo "[kb $(date -u +%Y-%m-%dT%H:%M:%SZ)] another pipeline run is in progress; exiting"
  exit 0
fi

source .venv/bin/activate

if [ -f "$KB_DIR/.env" ]; then
  set -a; source "$KB_DIR/.env"; set +a
fi

# Optional sync targets. All three sync points (inbox pull, processed-record
# push, cache publish) activate only when a target is configured:
#   KB_DASHBOARD_DIR    — local data dir (this machine hosts the dashboard/drop-zone)
#   KB_REMOTE_HOST      — SSH host to sync with instead (IP or resolvable name;
#                         a stable VPN address such as a Tailscale/WireGuard IP
#                         avoids LAN-DNS and host-key churn)
#   KB_REMOTE_DATA_DIR  — data dir on that host (required with KB_REMOTE_HOST)
#   KB_REMOTE_USER      — SSH user for the remote host (default: current user)
# With none of these set, the pipeline runs fully local and sync is skipped.
REMOTE_HOST="${KB_REMOTE_HOST:-}"
REMOTE_INBOX=""
REMOTE_CACHE=""
if [ -n "$REMOTE_HOST" ]; then
  if [ -n "${KB_REMOTE_DATA_DIR:-}" ]; then
    REMOTE_INBOX="${KB_REMOTE_USER:-$USER}@${REMOTE_HOST}:${KB_REMOTE_DATA_DIR%/}/inbox/"
    REMOTE_CACHE="${KB_REMOTE_USER:-$USER}@${REMOTE_HOST}:${KB_REMOTE_DATA_DIR%/}/cache/"
  else
    echo "WARN: KB_REMOTE_HOST is set but KB_REMOTE_DATA_DIR is not — remote sync disabled" >&2
    REMOTE_HOST=""
  fi
fi
SSH_CMD="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

log() { echo "[kb $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

tg_send() {
  local msg="$1"
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    return 0
  fi
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${msg}" \
    >/dev/null 2>&1 || true
}

log "Pipeline started"

# 1 — pull new inbox items (removes them from the source after transfer).
# KB_DASHBOARD_DIR set: local rsync from that data dir. KB_REMOTE_HOST set:
# remote rsync over SSH. Neither: skip — use raw/inbox/ as-is.
mkdir -p raw/inbox
INBOX_PULL=()
if [ -n "${KB_DASHBOARD_DIR:-}" ]; then
  mkdir -p "$KB_DASHBOARD_DIR/inbox"
  INBOX_PULL=(rsync -az --remove-source-files --exclude='processed/' "$KB_DASHBOARD_DIR/inbox/" raw/inbox/)
elif [ -n "$REMOTE_INBOX" ]; then
  INBOX_PULL=(rsync -az --remove-source-files --exclude='processed/' -e "$SSH_CMD" "$REMOTE_INBOX" raw/inbox/)
fi
if [ "${#INBOX_PULL[@]}" -eq 0 ]; then
  log "Inbox sync not configured — using local raw/inbox/ only"
elif "${INBOX_PULL[@]}"; then
  log "Inbox pulled OK"
else
  log "WARN: inbox pull failed — continuing with local inbox only"
fi

# 1b — merge the home server's interactive collection logs into ours.
# The memory-inject (UserPromptSubmit) and injection-outcome (SessionEnd) hooks
# fire on the server's interactive Claude sessions too, but the nightly consumers
# (audit_hooks, topic_records, scorecard) run only here and logs/ is excluded from
# the cache rsync — so without this the server's rows would be stranded and the loop
# would learn from PC traffic only. Idempotent, deduped, never fatal. Harvested
# corrections already ride the raw/inbox symlink, so this covers just the two JSONL
# logs. Primary deployment only: a KB_DASHBOARD_DIR host can't merge from itself,
# and with no remote host configured there is nothing to merge.
if [ -z "${KB_DASHBOARD_DIR:-}" ] && [ -n "$REMOTE_HOST" ]; then
  log "Merging remote collection logs..."
  MERGE_OUT=$(python "$KB_DIR/scripts/merge_remote_logs.py" 2>&1 || true)
  log "$MERGE_OUT"
fi

# 2 — run pipeline
# KB_MODEL / KB_TOPIC_MODEL override the defaults; each falls back to the first
# locally-available model from its candidate list.
SUMMARY_CANDIDATES=("${KB_MODEL:-qwen2.5-coder:7b}" "qwen2.5-coder:7b-instruct-q4_K_M" "llama3.2:3b")
TOPIC_CANDIDATES=("${KB_TOPIC_MODEL:-qwen2.5:7b-instruct-q4_K_M}" "qwen2.5-coder:7b-instruct-q4_K_M" "qwen2.5-coder:7b" "llama3.2:3b")

OLLAMA_TAGS_JSON=$(curl -sS --max-time 5 "${KB_OLLAMA_URL:-http://127.0.0.1:11434}/api/tags" 2>/dev/null || echo '{}')

pick_model() {
  local picked=""
  for candidate in "$@"; do
    if echo "$OLLAMA_TAGS_JSON" \
         | python3 -c "import json,sys; sys.exit(0 if '$candidate' in [m['name'] for m in json.load(sys.stdin).get('models',[])] else 1)"; then
      picked="$candidate"
      break
    fi
  done
  echo "$picked"
}

SUMMARY_MODEL=$(pick_model "${SUMMARY_CANDIDATES[@]}")
TOPIC_MODEL=$(pick_model "${TOPIC_CANDIDATES[@]}")

if [ -z "$SUMMARY_MODEL" ]; then
  # Refuse to run with a phantom tag: that path silently produces zero summaries
  # while still reporting success and pushing an empty result to the server.
  ERR="no ollama summary model available from candidates: ${SUMMARY_CANDIDATES[*]}"
  log "ERROR: $ERR — aborting (check 'ollama list' vs the candidate tags above)"
  python3 - "$ERR" <<'PY'
import json, datetime, sys
from pathlib import Path
Path('state').mkdir(exist_ok=True)
now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
meta = {'last_attempt': now, 'last_error': sys.argv[1]}
try:
    prev = json.loads(Path('state/sync_meta.json').read_text())
    if prev.get('synced_at'):
        meta['synced_at'] = prev['synced_at']  # preserve last successful sync
except Exception:
    pass
Path('state/sync_meta.json').write_text(json.dumps(meta, indent=2) + '\n')
PY
  tg_send "🧠 LLM KB | ABORTED — $ERR"
  exit 1
fi
if [ -z "$TOPIC_MODEL" ]; then
  log "WARN: no preferred topic model available; reusing summary model"
  TOPIC_MODEL="$SUMMARY_MODEL"
fi

# Cloud backend selection. Nightly default routes summaries to Sonnet and topic
# pages to Opus (separate quota pools on this plan), with the resolved local
# models as per-item fallback. Requires the `claude` binary and a long-lived
# CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`, stored in .env). If either
# is missing we degrade to local-only rather than firing a fallback per item.
# Override the specs with KB_SUMMARY_SPEC / KB_TOPIC_SPEC (e.g. to force local).
if command -v claude >/dev/null 2>&1 && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  SUMMARY_SPEC="${KB_SUMMARY_SPEC:-claude:sonnet}"
  TOPIC_SPEC="${KB_TOPIC_SPEC:-claude:opus}"
  log "Cloud backend enabled (summary: $SUMMARY_SPEC, topic: $TOPIC_SPEC; local fallback summary: $SUMMARY_MODEL, topic: $TOPIC_MODEL)"
else
  # Name exactly which precondition is missing so the failure is self-explaining
  # (the previous lumped message made this hard to diagnose from logs alone).
  MISSING=""
  command -v claude >/dev/null 2>&1 || MISSING="'claude' binary not on PATH"
  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    MISSING="${MISSING:+$MISSING; }CLAUDE_CODE_OAUTH_TOKEN unset (add it to .env)"
  fi
  SUMMARY_SPEC="${KB_SUMMARY_SPEC:-$SUMMARY_MODEL}"
  TOPIC_SPEC="${KB_TOPIC_SPEC:-$TOPIC_MODEL}"
  log "WARN: cloud backend unavailable ($MISSING) — running local-only"
  tg_send "🧠 LLM KB | ⚠️ cloud backend unavailable ($MISSING) — running nightly pipeline on local models only"
fi

log "Running pipeline (summary: $SUMMARY_SPEC, topic: $TOPIC_SPEC)..."

PIPELINE_START_EPOCH=$(date +%s)
set +e
OUTPUT=$(python scripts/kb.py run \
  --summary-model "$SUMMARY_SPEC" \
  --topic-model "$TOPIC_SPEC" \
  --summary-fallback "$SUMMARY_MODEL" \
  --topic-fallback "$TOPIC_MODEL" \
  --review-model "$SUMMARY_MODEL" 2>&1)
PIPELINE_RC=$?
set -e
echo "$OUTPUT"

# Detect qwen3 thinking-trace leakage in newly-written compiled files.
THINK_HITS=$(find compiled/sources compiled/topics -type f -name '*.md' \
  -newermt "@$PIPELINE_START_EPOCH" 2>/dev/null \
  -exec grep -l -E '<think>|</think>' {} + 2>/dev/null || true)
if [ -n "$THINK_HITS" ]; then
  log "WARN: thinking-trace leakage detected in compiled output:"
  echo "$THINK_HITS" | while read -r f; do log "  - $f"; done
fi

PIPELINE_ERROR=""
if [ "$PIPELINE_RC" -ne 0 ]; then
  PIPELINE_ERROR="kb.py run exited $PIPELINE_RC (summary: $SUMMARY_SPEC, topic: $TOPIC_SPEC)"
  log "ERROR: $PIPELINE_ERROR — continuing with bookkeeping/sync"
  MSG="KB pipeline FAILED ($PIPELINE_ERROR) — see logs/pipeline.log"
else
  INGESTED=$(echo "$OUTPUT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(len(d.get('ingested_from_inbox',[])))" 2>/dev/null || echo "?")
  SUMMARIZED=$(echo "$OUTPUT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(len(d.get('summarized',[])))" 2>/dev/null || echo "?")
  REBUILT=$(echo "$OUTPUT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(len(d.get('rebuilt_topics',[])))" 2>/dev/null || echo "?")

  if [ "$INGESTED" = "0" ] && [ "$SUMMARIZED" = "0" ] && [ "$REBUILT" = "0" ]; then
    MSG="KB pipeline ran — nothing changed."
  else
    MSG="KB pipeline done — inbox: $INGESTED new, summaries: $SUMMARIZED updated, topics rebuilt: $REBUILT."
  fi
fi
log "$MSG"

# Summarize any cloud->local fallbacks from this run into the notification.
if [ -f state/last_run_report.json ]; then
  FB_SUMMARY=$(python3 -c "
import json
from collections import Counter
try:
    d = json.load(open('state/last_run_report.json'))
except Exception:
    raise SystemExit
fb = d.get('fallbacks', [])
if fb:
    c = Counter(x['reason'] for x in fb)
    print(' | ⚠️ %d fell back to local (%s)' % (len(fb), ', '.join('%s×%d' % (k, v) for k, v in sorted(c.items()))))
" 2>/dev/null || true)
  if [ -n "$FB_SUMMARY" ]; then
    MSG="$MSG$FB_SUMMARY"
    log "Fallbacks this run:$FB_SUMMARY"
  fi
fi

# 3 — push processed inbox record back to the inbox drop-zone (bookkeeping, non-fatal)
if [ -d raw/inbox/processed ] && [ "$(ls -A raw/inbox/processed 2>/dev/null)" ]; then
  PROCESSED_PUSH=()
  if [ -n "${KB_DASHBOARD_DIR:-}" ]; then
    mkdir -p "$KB_DASHBOARD_DIR/inbox/processed"
    PROCESSED_PUSH=(rsync -az raw/inbox/processed/ "$KB_DASHBOARD_DIR/inbox/processed/")
  elif [ -n "$REMOTE_INBOX" ]; then
    PROCESSED_PUSH=(rsync -az -e "$SSH_CMD" raw/inbox/processed/ "${REMOTE_INBOX}processed/")
  fi
  if [ "${#PROCESSED_PUSH[@]}" -gt 0 ]; then
    log "Pushing processed inbox record..."
    "${PROCESSED_PUSH[@]}" \
      && log "Processed record synced OK" \
      || log "WARN: processed record push failed (non-fatal)"
  fi
fi

# 4 — write sync metadata (included in push)
LAST_ERROR_JSON="null"
if [ -n "$PIPELINE_ERROR" ]; then
  LAST_ERROR_JSON=$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$PIPELINE_ERROR")
fi
python3 - "$LAST_ERROR_JSON" <<'PY'
import json, datetime, sys
from pathlib import Path
Path('state').mkdir(exist_ok=True)
now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
last_error = json.loads(sys.argv[1])
meta = {
    'last_attempt': now,
    'last_error': last_error,
}
if last_error is None:
    meta['synced_at'] = now
else:
    # preserve the last successful sync timestamp on failure
    try:
        prev = json.loads(Path('state/sync_meta.json').read_text())
        if prev.get('synced_at'):
            meta['synced_at'] = prev['synced_at']
    except Exception:
        pass
Path('state/sync_meta.json').write_text(json.dumps(meta, indent=2) + '\n')
PY

# 5 — publish full KB to the dashboard cache.
# KB_DASHBOARD_DIR set: local rsync into that data dir. KB_REMOTE_HOST set:
# remote rsync over SSH. Neither: skip.
CACHE_RSYNC=(rsync -az --delete
  --exclude='.venv/'
  --exclude='.env'
  --exclude='.git/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='logs/'
  --exclude='raw/inbox/'
  --exclude='sample_*'
  --exclude='sample.pdf')
CACHE_TARGET=""
if [ -n "${KB_DASHBOARD_DIR:-}" ]; then
  mkdir -p "$KB_DASHBOARD_DIR/cache"
  CACHE_RSYNC+=("$KB_DIR/" "$KB_DASHBOARD_DIR/cache/")
  CACHE_TARGET="local"
elif [ -n "$REMOTE_CACHE" ]; then
  CACHE_RSYNC+=(-e "$SSH_CMD" "$KB_DIR/" "$REMOTE_CACHE")
  CACHE_TARGET="remote"
fi
if [ -z "$CACHE_TARGET" ]; then
  log "Cache publish not configured — skipping"
elif log "Publishing KB to dashboard cache..." && "${CACHE_RSYNC[@]}"; then
  log "KB publish OK"
else
  log "KB publish FAILED"
  MSG="$MSG (cache publish failed)"
fi

# 6 — nightly hook auto-tune (opt-in). Classifies memory-injection misfires from
# logs/memory_injection.jsonl and TIGHTENS config/memory_tuning.yaml on a dated
# branch (never main). Default OFF until a few --dry-runs have been reviewed.
if [ "${KB_HOOK_AUTOTUNE:-0}" = "1" ]; then
  log "Running hook auto-tune..."
  AUTOTUNE_OUT=$(python "$KB_DIR/scripts/audit_hooks.py" 2>&1 || true)
  log "$AUTOTUNE_OUT"
  case "$AUTOTUNE_OUT" in
    *"no action"*|*"disabled"*|"") : ;;
    *) tg_send "🛠️ KB hook auto-tune | $AUTOTUNE_OUT" ;;
  esac
fi

# 7 — per-topic record maintenance. Rebuilds state/topic_records.json idempotently from
# logs/injection_outcomes.jsonl: per-topic Beta confidence (alpha/beta) + decay substrate
# = L1 revealed-preferences ∪ elfmem governance. Pure data maintenance — no git, no config
# mutation, no human gate — so it runs every night unconditionally (unlike the auto-tune
# above) and only logs. Built but NOT yet consumed by retrieval (see docs/feedback-loop.md).
TOPIC_RECORDS_OUT=$(python "$KB_DIR/scripts/topic_records.py" 2>&1 || true)
log "$TOPIC_RECORDS_OUT"

# 7b — bounded correction-harvest backfill (2026-08-20; collection-compatible per
# the registration: "transcript backfill — grows Gate-1; cannot backfill the
# curve"). Works the ~3,200-transcript backlog newest-first at
# KB_HARVEST_BACKFILL_CAP (default 60) sonnet calls/night — the judge-cap
# pattern: resumable, state-on-success-only, stops early on a cloud outage.
# Harvested notes land in raw/inbox/ and ride tomorrow's ingestion normally.
log "step 7b: correction-harvest backfill"
BACKFILL_OUT=$(python "$KB_DIR/scripts/harvest_backfill.py" 2>&1 || true)
log "$BACKFILL_OUT"

# 8 — daily upkeep piggyback (2026-08-10). The original 09:00 heartbeat cron
# assumed a machine awake at 09:00; the journal showed this PC never is, so the
# upkeep block (transcript archive, rescore refresh, judge pass, compounding
# tripwire) silently never ran — last genuine run 07-20, judge 0/903 eleven
# nights after being wired ON. The 22:00 slot demonstrably fires, so upkeep
# rides it; the morning cron (moved to 10:30) is an
# idempotent bonus. The heartbeat's staleness check is quiet here by
# construction (sync_meta was written moments ago). Never fatal to the pipeline.
log "step 8: daily upkeep (heartbeat piggyback)"
bash "$KB_DIR/scripts/heartbeat.sh" >> "$KB_DIR/logs/pipeline.log" 2>&1 || true

# Append the current gate-accrual count (read-only; delegates to gate_dossier.py
# count — the JUDGEABLE set). Informational only — the tripwire itself still fires once.
GATE_ACCRUAL=$(bash "$KB_DIR/scripts/gate_accrual_check.sh" --count-only 2>/dev/null || echo '?')
MSG="$MSG | 🎯 gate: ${GATE_ACCRUAL} judgeable"

# Rerank fallback count (scorer health when a rerank config is live). timeouts/
# attempts (rate); ⚠Nerr means retrieve() crashed N times — zero by construction,
# so any count is a surfacing bug. Bump KB_FALLBACK_SINCE when a new retrieval
# config ships. With live retrieval LEXICAL (Task 9 ladder exhausted 2026-08-01;
# GPU upgrade ≥2027) the count is 'n/a' indefinitely — suppress the line then,
# rather than sending noise for months; it self-reactivates on any judge attempt.
# Floor = window-open date, PAST the terminated k5f2 deploy's rows (its 21
# attempts are a dead config's history, not current scorer health).
FALLBACK=$(python "$KB_DIR/scripts/fallback_rate.py" --brief \
  --since "${KB_FALLBACK_SINCE:-2026-08-05T00:00}" 2>/dev/null || echo '?')
[ "$FALLBACK" != "n/a" ] && MSG="$MSG | ⏱ rerank fb: ${FALLBACK}"

tg_send "🧠 LLM KB | $MSG"
log "Pipeline finished"
