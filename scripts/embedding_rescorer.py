#!/usr/bin/env python3
"""Embedding rescorer for memory retrieval (behind KB_RETRIEVAL=embedding|hybrid).

Fixes lexical retrieval's paraphrase-blindness: the lexical scorer returns 0 when
a query and a topic share no surface tokens, so a rephrased question never finds
the right topic (measured: paraphrase recall@5 = 8%). This embeds the query and
each topic with a local Ollama embedding model and ranks by cosine similarity.

Topic vectors are cached incrementally (keyed on a content hash) in
state/topic_embeddings.json, so live retrieval costs one query-embed call plus an
in-memory cosine sweep — the 895-topic build happens offline (CLI / nightly),
never in the hook. Stdlib-only (urllib + math) so the system-python3 hook can
import it; any failure is caught by the caller, which falls back to lexical.

CLI:
  python scripts/embedding_rescorer.py build [--force]   # (re)build the cache
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / 'state'
CACHE_PATH = STATE_DIR / 'topic_embeddings.json'

EMBED_MODEL = os.environ.get('KB_EMBED_MODEL', 'nomic-embed-text')
# Device: nomic-embed-text is tiny (137M) and embeds a query in tens of ms on CPU, so
# default it to CPU (num_gpu=0) — this frees ~595MB VRAM during interactive shadow use
# AND during the nightly (giving the 7B summary/topic models more headroom on the 6GB
# card, so they spill fewer layers). Code default (not .env) so it reaches the hook-
# spawned shadow, which doesn't source .env. Set KB_EMBED_NUM_GPU=99 to restore GPU.
EMBED_NUM_GPU = int(os.environ.get('KB_EMBED_NUM_GPU', '0'))
OLLAMA_URL = os.environ.get('KB_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
EMBED_TIMEOUT = float(os.environ.get('KB_EMBED_TIMEOUT', '30'))


class EmbedError(RuntimeError):
    pass


def embed(text: str) -> list[float]:
    payload = json.dumps({'model': EMBED_MODEL, 'prompt': text,
                          'options': {'num_gpu': EMBED_NUM_GPU}}).encode()
    req = urllib.request.Request(
        f'{OLLAMA_URL}/api/embeddings',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:  # network / model-missing / timeout
        raise EmbedError(f'embed call failed: {e}') from e
    vec = data.get('embedding')
    if not vec:
        raise EmbedError('no embedding in response')
    return vec


def entry_text(entry: dict) -> str:
    """The semantic surface of a topic: its display name + key points. This is
    what a relevant query is semantically near, even with zero token overlap."""
    parts = [entry.get('display') or entry.get('slug', '')]
    parts += entry.get('key_points', [])[:10]
    return '\n'.join(p for p in parts if p)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {'model': EMBED_MODEL, 'topics': {}}


def load_topic_vectors() -> dict:
    """slug -> vec, from cache ONLY (never builds). Empty dict if the cache is
    missing or was built with a different model — the caller treats empty as
    'embeddings unavailable' and falls back to lexical."""
    cache = load_cache()
    if cache.get('model') != EMBED_MODEL:
        return {}
    return {slug: rec['vec'] for slug, rec in cache.get('topics', {}).items() if rec.get('vec')}


def build_topic_embeddings(entries, force=False, log=print) -> dict:
    cache = load_cache()
    if cache.get('model') != EMBED_MODEL:
        cache = {'model': EMBED_MODEL, 'topics': {}}
    topics = cache['topics']
    built = reused = 0
    valid = set()
    for e in entries:
        slug = e['slug']
        valid.add(slug)
        text = entry_text(e)
        h = _hash(text)
        rec = topics.get(slug)
        if rec and rec.get('hash') == h and not force:
            reused += 1
            continue
        topics[slug] = {'hash': h, 'vec': embed(text)}
        built += 1
        if built % 100 == 0:
            log(f'  embedded {built} topics...')
    for slug in [s for s in topics if s not in valid]:   # prune dropped topics
        del topics[slug]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))
    log(f'topic embeddings: {built} built, {reused} reused, {len(topics)} total '
        f'({EMBED_MODEL}) -> {CACHE_PATH.relative_to(BASE_DIR)}')
    return {slug: rec['vec'] for slug, rec in topics.items()}


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(BASE_DIR / 'scripts'))
    import memory_index as mi

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'build'
    if cmd == 'build':
        build_topic_embeddings(mi.load_index()['entries'], force='--force' in sys.argv)
    else:
        print('usage: embedding_rescorer.py build [--force]')
