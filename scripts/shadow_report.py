#!/usr/bin/env python3
"""Summarize logs/shadow_retrieval.jsonl — the embedding-vs-lexical real-traffic
A/B that shadow mode collects. Answers the questions that gate flipping the live
flag: how often would embedding inject a HIGH fact lexical missed (the upside)?
how often would switching DROP a fact lexical currently injects (the risk)? and
WHAT does embedding surface, so its picks can be eyeballed for false injects."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG = BASE_DIR / 'logs' / 'shadow_retrieval.jsonl'


def main() -> None:
    if not LOG.exists():
        print(f'no shadow log yet: {LOG}')
        return
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    n = len(rows)
    if not n:
        print('shadow log empty')
        return
    lex_high = sum(1 for r in rows if r.get('lexical_high'))
    emb_high = sum(1 for r in rows if r.get('embedding_high'))
    emb_only = [r for r in rows if r.get('embedding_only_high')]
    lex_only = [r for r in rows if r.get('lexical_only_high')]
    agree = sum(1 for r in rows if r.get('agree_injected'))

    def pct(x):
        return f'{100 * x // n}%'

    print(f'shadow turns logged: {n}')
    print(f'  lexical produced HIGH:   {lex_high} ({pct(lex_high)})')
    print(f'  embedding produced HIGH: {emb_high} ({pct(emb_high)})')
    print(f'  embedding HIGH-surfaced a fact lexical MISSED: {len(emb_only)} ({pct(len(emb_only))})  <- upside')
    print(f'  lexical HIGH-surfaced a fact embedding MISSED: {len(lex_only)} ({pct(len(lex_only))})  <- switch risk')
    print(f'  top-injection agreement: {agree}/{n}')

    # Rerank verdicts (only present once rerank shadow is enabled —
    # KB_SHADOW_RERANK=1 / touch state/shadow_rerank.enabled). This is the
    # precision signal the gated eval can't give: when embedding wants to inject,
    # does the judge CONFIRM / SWITCH / REJECT on real traffic?
    rr_rows = [r for r in rows if r.get('rerank_verdict')]
    if rr_rows:
        vc = Counter(r['rerank_verdict'] for r in rr_rows)
        m = len(rr_rows)
        considered = [r for r in rr_rows if r['rerank_verdict'] != 'no_embedding_high']
        rejects = vc.get('reject', 0)
        print(f'\nrerank judge verdicts ({m} turns with a rerank pass):')
        for v in ('confirm', 'switch', 'reject', 'no_embedding_high'):
            if vc.get(v):
                print(f'  {v:18} {vc[v]} ({100 * vc[v] // m}%)')
        if considered:
            print(f'  -> of {len(considered)} would-inject turns, judge REJECTED '
                  f'{rejects} ({100 * rejects // len(considered)}%) as noise')

    c = Counter(s for r in emb_only for s in r['embedding_only_high'])
    if c:
        print('\nmost frequent embedding-only HIGH topics (eyeball for relevance / false injects):')
        for slug, cnt in c.most_common(15):
            print(f'  {cnt:3}  {slug}')
    if emb_only:
        print('\nsample turns where embedding surfaced what lexical missed:')
        for r in emb_only[:8]:
            print(f"  [{r.get('project')}] {(r.get('prompt_head') or '')[:80]!r}")
            print(f"      lexical={r.get('lexical_injected')}  emb_only_high={r.get('embedding_only_high')}")


if __name__ == '__main__':
    main()
