#!/usr/bin/env python3
"""LLM reranker — the precision stage of retrieve-then-rerank.

Embedding retrieval has high RECALL but low top-1 PRECISION: the right topic
lands in the top-K candidate pool, but sibling topics outrank it for the #1 slot
(measured: paraphrase recall@5 = 42% but hit@1 ~ 0). This stage hands the top-K
embedding candidates to a local LLM and asks which ONE actually answers the
prompt — or none — converting recall into an injectable single pick.

Stdlib-only (urllib) so it has no hard deps. A failed/ambiguous call returns None,
and the caller falls back to the embedding ordering, so retrieval never breaks.

NOTE: this calls a local generate (~1-2s warm) — it belongs offline / in the eval
and as an async or ambiguity-gated live stage, NOT inline in the eager hook.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

OLLAMA_URL = os.environ.get('KB_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
RERANK_MODEL = os.environ.get('KB_RERANK_MODEL', 'qwen2.5:7b-instruct-q4_K_M')
RERANK_TIMEOUT = float(os.environ.get('KB_RERANK_TIMEOUT', '60'))
RERANK_FACTS = int(os.environ.get('KB_RERANK_FACTS', '3'))

# Cloud judge pin (haiku rung, 2026-08-01). Alias 'haiku' resolved empirically via
# modelUsage BEFORE dev (A1 lesson applied at the start this time); assert-and-abort
# on mismatch in _generate_cloud — a verdict from any other model is never accepted,
# the call raises and the caller takes the bounded lexical fallback. Changing this
# pin is a NEW scorer: it changes retrieval_config and requires re-certification.
RERANK_CLOUD_MODEL_ID = 'claude-haiku-4-5-20251001'


def rerank_backend() -> str:
    """'ollama' (default) | 'claude'. Read at call time so the hook's env pins
    and per-process eval overrides both work without reloads."""
    return os.environ.get('KB_RERANK_BACKEND', 'ollama').strip().lower()


class RerankError(RuntimeError):
    pass


def _payload(prompt: str) -> dict:
    # keep_alive holds qwen2.5:7b resident between gated calls — a cold reload is
    # ~24s (the latency that kept rerank offline); warm it's ~1-2s. Read at call
    # time so it's tunable per-process without a reload.
    return {
        'model': RERANK_MODEL,
        'prompt': prompt,
        'stream': False,
        'keep_alive': os.environ.get('KB_RERANK_KEEPALIVE', '10m'),
        'options': {'temperature': 0.0, 'num_predict': 8, 'num_ctx': 4096},
    }


def _generate_ollama(prompt: str) -> str:
    data = json.dumps(_payload(prompt)).encode()
    req = urllib.request.Request(
        f'{OLLAMA_URL}/api/generate',
        data=data, headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as r:
            return json.loads(r.read()).get('response', '')
    except Exception as e:
        raise RerankError(f'rerank generate failed: {e}') from e


def _generate_cloud(prompt: str) -> str:
    """One pinned cloud judge call, bounded by the rerank deadline.

    `kb` is imported lazily (it pulls yaml/requests) so the default ollama path
    keeps this module dep-free. Any failure — transport, timeout, pin mismatch —
    raises RerankError; the caller's contract turns that into the bounded
    lexical fallback with provenance. A verdict from an unpinned model is never
    accepted (A1)."""
    import kb  # noqa: PLC0415
    try:
        payload = kb.claude_code_payload(RERANK_CLOUD_MODEL_ID, prompt,
                                         timeout=RERANK_TIMEOUT)
    except Exception as e:
        raise RerankError(f'cloud rerank failed: {e}') from e
    models = sorted((payload.get('modelUsage') or {}).keys())
    if models != [RERANK_CLOUD_MODEL_ID]:
        raise RerankError(f'cloud rerank model pin violated: payload reports '
                          f'{models}, expected [{RERANK_CLOUD_MODEL_ID!r}]')
    return (payload.get('result') or '').strip()


def _generate(prompt: str) -> str:
    if rerank_backend() == 'claude':
        return _generate_cloud(prompt)
    return _generate_ollama(prompt)


def _build_prompt(query: str, candidates: list[dict]) -> str:
    lines = [
        'You match a user question to the ONE knowledge-base topic that best answers it.',
        '',
        'User question:',
        query.strip(),
        '',
        'Candidate topics:',
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(f'{i}. {c.get("display") or c.get("slug")}')
        for kp in (c.get('key_points') or [])[:RERANK_FACTS]:
            lines.append(f'   - {kp}')
    lines += [
        '',
        'Most user messages are conversational or workflow commands (e.g. "merge to',
        'main", "let\'s sync", "commit it") with NO relevant topic — for those the',
        'answer is 0. Only choose a number if that topic DIRECTLY answers a genuine',
        'question. When in doubt, answer 0.',
        f'Which ONE candidate (1-{len(candidates)}) directly answers the question, or 0 if none?',
        'Reply with ONLY the number.',
        'Answer:',
    ]
    return '\n'.join(lines)


def rerank(query: str, candidates: list[dict]) -> int | None:
    """Return the 0-based index of the chosen candidate, or None (none relevant /
    unparseable / call failed)."""
    if not candidates:
        return None
    out = _generate(_build_prompt(query, candidates))
    m = re.search(r'-?\d+', out)
    if not m:
        return None
    n = int(m.group())
    if n <= 0 or n > len(candidates):
        return None          # 0 = "none relevant", or out of range
    return n - 1
