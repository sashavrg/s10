#!/usr/bin/env bash
# Claude Code SessionEnd hook -> harvest explicit corrections into the KB inbox.
#
# Install: reference this from ~/.claude/settings.json under hooks.SessionEnd,
# e.g.
#   {
#     "hooks": {
#       "SessionEnd": [
#         { "hooks": [ { "type": "command",
#                        "command": "/ABS/PATH/s10/scripts/claude_session_hook.sh" } ] }
#       ]
#     }
#   }
#
# Claude Code passes a JSON payload on stdin. We read transcript_path and cwd
# from it, derive a project tag from the project directory name, and run the
# harvester non-blocking. Failures must never disrupt the session, so we always
# exit 0.

set -uo pipefail

KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# RECURSION GUARD. The harvester extracts via a headless `claude -p` call, which
# is itself a Claude Code session that fires this very SessionEnd hook on exit —
# an infinite loop that re-spawns the harvester forever. kb.claude_code_generate
# marks those KB-internal calls with KB_HEADLESS=1 (it propagates to the spawned
# process and the hooks it fires); skip immediately when we see it.
[ -n "${KB_HEADLESS:-}" ] && exit 0

# Make cloud auth (CLAUDE_CODE_OAUTH_TOKEN) and any KB_* overrides available to
# the detached harvester, mirroring run_pipeline.sh. The default extraction model
# is a cloud model (claude:sonnet); this supplies its credential when the machine
# is not already interactively logged into the `claude` CLI.
if [ -f "$KB_DIR/.env" ]; then
  set -a; . "$KB_DIR/.env"; set +a
fi

VENV_PY="$KB_DIR/.venv/bin/python"
PY="${VENV_PY:-python3}"
[ -x "$VENV_PY" ] || PY="python3"

PAYLOAD="$(cat)"

# Extract fields without requiring jq (fall back to jq if present).
read_field() {
  local key="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$PAYLOAD" | jq -r ".${key} // empty" 2>/dev/null
  else
    printf '%s' "$PAYLOAD" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d.get('$key',''))" 2>/dev/null
  fi
}

TRANSCRIPT="$(read_field transcript_path)"
CWD="$(read_field cwd)"

[ -z "$TRANSCRIPT" ] && exit 0
[ -f "$TRANSCRIPT" ] || exit 0

# Project tag: prefer the working-dir basename; else the transcript's parent dir
# (Claude Code stores transcripts per-project).
if [ -n "$CWD" ]; then
  PROJECT="$(basename "$CWD")"
else
  PROJECT="$(basename "$(dirname "$TRANSCRIPT")")"
fi

# Run detached so session teardown is never blocked. session_end.sh runs the
# harvester (time-boxed) and then the injection-outcome scorer in order, so the
# scorer can see a correction harvested this same session.
nohup "$KB_DIR/scripts/session_end.sh" "$TRANSCRIPT" "$PROJECT" \
  >> "$KB_DIR/logs/session_end.log" 2>&1 &

exit 0
