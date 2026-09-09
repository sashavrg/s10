#!/usr/bin/env bash
# SessionStart warm-up for the rerank judge (Task 9 flip, 2026-07-29).
#
# The live UserPromptSubmit hook runs retrieve-then-rerank with a 6s internal
# deadline; the judge model is ~1-2s WARM but ~24s cold, so a cold first turn
# always burns one lexical fallback. This hook preloads the model the moment a
# session starts — while the user is still typing their first prompt — and the
# generous keep_alive then spans real inter-turn gaps. Semantics-preserving by
# construction: it only loads the SAME pinned model, it never changes what the
# scorer does.
#
# Registered as a SessionStart hook in ~/.claude/settings.json. Detaches and
# exits immediately (never adds startup latency, never fails a session).
#
# Keepalive default 30m — keep in sync with memory_inject_hook.py's pin. Manual
# VRAM reclaim at any moment: `ollama stop qwen2.5:7b-instruct-q4_K_M` (the next
# rerank falls back to lexical once, logged, and reloads the model).
KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$KB_DIR/.env" ] && { set -a; . "$KB_DIR/.env"; set +a; } 2>/dev/null

MODEL="${KB_RERANK_MODEL:-qwen2.5:7b-instruct-q4_K_M}"
URL="${KB_OLLAMA_URL:-http://127.0.0.1:11434}"
KEEP="${KB_RERANK_KEEPALIVE:-30m}"

# Empty prompt = Ollama's documented "just load the model" call.
nohup curl -s --max-time 120 -X POST "$URL/api/generate" \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"\",\"keep_alive\":\"$KEEP\"}" \
  >/dev/null 2>&1 &
exit 0
