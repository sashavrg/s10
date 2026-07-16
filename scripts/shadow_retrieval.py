#!/usr/bin/env python3
"""Shadow-mode embedding retrieval comparator.

Spawned DETACHED by the memory-injection hook on each real user turn when shadow
mode is on (state/shadow_retrieval.enabled exists, or KB_SHADOW_RETRIEVAL set in
the environment). It re-scores the same prompt with the embedding scorer and logs
what embedding WOULD have injected next to what lexical actually did — a zero-risk
A/B on real traffic. It injects nothing and runs out-of-band, so the live turn is
never blocked or altered.

Input: argv[1] = path to a JSON job file written by the hook:
  {prompt, project, session_id, lexical_top_tier, lexical_high:[slug...],
   lexical_injected: slug|null}
The job file is deleted after reading. Output is appended to
logs/shadow_retrieval.jsonl; analyze with: scripts/shadow_report.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))
from eval_split import stable_bucket  # noqa: E402  (needs the sys.path insert above)

SHADOW_LOG = BASE_DIR / 'logs' / 'shadow_retrieval.jsonl'

# Deterministic sample rate for the (expensive 7B) rerank pass over a weeks-long
# collection window. Override with KB_SHADOW_RERANK_SAMPLE (1.0 = full rate).
RERANK_SAMPLE_RATE = float(os.environ.get('KB_SHADOW_RERANK_SAMPLE', '0.2'))


def _rerank_shadow_enabled() -> bool:
    """Opt-in SEPARATELY from embedding shadow: the rerank pass runs the 7B judge
    (a GPU spike per would-inject turn), so it's gated behind its own flag to keep
    it off the box during gaming / live tuning. Enable with KB_SHADOW_RERANK=1 or
    `touch state/shadow_rerank.enabled`. Even when enabled it is SAMPLED — see
    _rerank_sampled."""
    if os.environ.get('KB_SHADOW_RERANK', '0') != '0':
        return True
    return (BASE_DIR / 'state' / 'shadow_rerank.enabled').exists()


def _rerank_sampled(session_id, rate: float = RERANK_SAMPLE_RATE) -> bool:
    """Deterministic per-SESSION sampling for the rerank pass: hash the session_id and
    keep the bottom `rate` quintile. Deterministic — NOT a per-turn random draw — so the
    later counterfactual (shadow verdict × session-depth) join isn't biased by which turns
    got sampled: a session is entirely in or entirely out. Missing session_id -> excluded
    (it can't be joined to depth anyway, so don't spend GPU on it)."""
    if not session_id:
        return False
    return stable_bucket(str(session_id)) < rate


def build_record(job: dict, emb_res: dict, rerank_res: dict | None, ts: str) -> dict:
    """Pure: assemble the shadow log row from the embedding pass and (optionally)
    the rerank pass. rerank_res is None when the rerank pass is disabled; when it
    ran but fell back to lexical (ollama/cache down) its mode != 'rerank' and the
    rerank fields are omitted so the verdict is never fabricated."""
    matches = emb_res.get('matches', [])
    top = matches[0] if matches else None
    emb_top = top['slug'] if top else None
    emb_high = [m['slug'] for m in matches if m['tier'] == 'high']
    lex_high = job.get('lexical_high') or []
    rec = {
        'ts': ts,
        'session_id': job.get('session_id'),
        'project': job.get('project'),
        'prompt_head': (job.get('prompt') or '')[:160],
        'lexical_top_tier': job.get('lexical_top_tier'),
        'lexical_injected': job.get('lexical_injected'),
        'lexical_high': lex_high,
        'embedding_top_tier': emb_res.get('top_tier'),
        'embedding_top': emb_top,
        'embedding_top_score': round(top['score'], 3) if top else 0.0,
        'embedding_high': emb_high,
        # the actionable signal: HIGH facts embedding surfaces that lexical missed
        'embedding_only_high': [s for s in emb_high if s not in lex_high],
        'lexical_only_high': [s for s in lex_high if s not in emb_high],
        'agree_injected': bool(top and job.get('lexical_injected') == emb_top),
    }
    if rerank_res is not None and rerank_res.get('mode') == 'rerank':
        rr_matches = rerank_res.get('matches', [])
        rr_high = [m['slug'] for m in rr_matches if m['tier'] == 'high']
        rr_top = rr_high[0] if rr_high else None
        if not emb_high:
            verdict = 'no_embedding_high'      # nothing for the judge to validate
        elif not rr_high:
            verdict = 'reject'                 # embedding wanted to inject; judge said none
        elif rr_top == emb_top:
            verdict = 'confirm'                # judge kept embedding's pick
        else:
            verdict = 'switch'                 # judge promoted a sibling
        rec['rerank_top'] = rr_top
        rec['rerank_high'] = rr_high
        rec['rerank_verdict'] = verdict
        rec['embedding_gap'] = (round(matches[0]['score'] - matches[1]['score'], 3)
                                if len(matches) > 1 else None)
    return rec


def _diag(msg: str) -> None:
    """Surface WHY a shadow turn produced no row. The hook redirects THIS process's
    stderr to logs/shadow.err (stdout stays DEVNULL), so these one-liners turn the
    formerly-silent failures (missing nomic-embed-text, Ollama down) into a visible
    trail. Fail-open — diagnostics must never break shadow. Empty file = healthy."""
    try:
        print(f'[shadow {dt.datetime.now().isoformat(timespec="seconds")}] {msg}',
              file=sys.stderr, flush=True)
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        return
    jobfile = Path(sys.argv[1])
    try:
        job = json.loads(jobfile.read_text())
    except Exception as e:
        _diag(f'bad job file {jobfile}: {e}')
        return
    finally:
        try:
            jobfile.unlink()
        except Exception:
            pass

    try:
        import memory_index as mi
    except Exception as e:
        _diag(f'memory_index import failed: {e}')
        return

    os.environ['KB_RETRIEVAL'] = 'embedding'   # force embedding for THIS process only
    try:
        emb_res = mi.retrieve(job['prompt'], project=job.get('project'))
    except Exception as e:
        _diag(f'embedding retrieve raised: {e}')
        return
    if emb_res.get('mode') != 'embedding':
        # Fell back to lexical -> the embed step failed. THE silent-shadow failure
        # mode (it cost a day on 2026-06-30: nomic-embed-text had vanished). Most
        # common cause: Ollama down or the embed model missing.
        _diag('embedding unavailable (retrieve fell back to lexical) — Ollama down '
              'or nomic-embed-text missing? check: ollama list')
        return

    # Optional second pass: ask the rerank judge what it WOULD inject. Forced on
    # every would-inject turn (KB_RERANK_ALWAYS) so we measure rejection rate, not
    # just tie-breaks. Heavier (7B), so gated behind its own flag and fail-open.
    rerank_res = None
    if _rerank_shadow_enabled() and _rerank_sampled(job.get('session_id')):
        os.environ['KB_RETRIEVAL'] = 'rerank'
        os.environ['KB_RERANK_ALWAYS'] = '1'
        try:
            rerank_res = mi.retrieve(job['prompt'], project=job.get('project'))
        except Exception as e:
            _diag(f'rerank pass raised (non-fatal): {e}')
            rerank_res = None

    rec = build_record(job, emb_res, rerank_res,
                       dt.datetime.now().isoformat(timespec='seconds'))
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_LOG.open('a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


if __name__ == '__main__':
    main()
