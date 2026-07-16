#!/usr/bin/env python3
"""Cloud dev driver (#1e round 2): the ej6 rubric judge (claude:sonnet) vs the 83-row
CALIBRATION set (evals/fixtures/engagement_calibration_2026-07-03.jsonl — the two burned
label sets; gold for dev, never for gating).

Reuses engagement_judge's evidence builder, prompt (rubric-embedded, test-pinned), and
parser verbatim — only generate_fn differs (kb.claude_code_generate: neutral cwd,
KB_HEADLESS guard, ANTHROPIC_API_KEY scrubbed). Read-only; writes no dataset.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KB / 'scripts'))

import engagement_judge as ej                      # noqa: E402
import kb                                          # noqa: E402
from injection_outcome import parse_turns          # noqa: E402
from scorecard import read_outcomes, wilson        # noqa: E402

CAL_PATH = KB / 'evals' / 'fixtures' / 'engagement_calibration_2026-07-03.jsonl'


def sonnet_generate(prompt: str) -> str:
    return kb.claude_code_generate('sonnet', prompt)


def load_calibration(path: Path = CAL_PATH) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()
            if l.strip() and not l.startswith('#')]


def score(subset) -> dict:
    tp = sum(1 for x, v in subset if v and v['engaged'] and x['label_engaged'])
    fp = sum(1 for x, v in subset if v and v['engaged'] and not x['label_engaged'])
    fn = sum(1 for x, v in subset if v and not v['engaged'] and x['label_engaged'])
    unj = sum(1 for x, v in subset if v is None)
    return {'n': len(subset), 'tp': tp, 'fp': fp, 'fn': fn, 'unjudged': unj,
            'P': round(tp / (tp + fp), 4) if tp + fp else None,
            'R': round(tp / (tp + fn), 4) if tp + fn else None,
            'P_ci': wilson(tp, tp + fp) if tp + fp else None,
            'R_ci': wilson(tp, tp + fn) if tp + fn else None}


def main() -> None:
    rows = load_calibration()
    inj = read_outcomes(KB / 'logs' / 'memory_injection.jsonl')
    inj_by_key = {(r.get('session_id'), r.get('ts')): r for r in inj}
    turns_cache = {}
    jobs = []
    for x in rows:
        sid = x['session_id']
        if sid not in turns_cache:
            tp_ = ej.resolve_transcript(sid)
            turns_cache[sid] = parse_turns(tp_) if tp_ else []
        r = inj_by_key.get((sid, x['ts']))
        ev = None
        if r and turns_cache[sid]:
            inj_eff = {**r, 'injected': x['injected'], 'tier': x['tier']}
            page = None if x['tier'] == 'high' else ej.load_topic_page(x['injected'])
            ev = ej.build_evidence(inj_eff, turns_cache[sid], page)
        jobs.append((x, ev))

    smoke_ev = next(ev for _, ev in jobs if ev)
    if ej.judge_engagement(smoke_ev, generate_fn=sonnet_generate) is None:
        print('SMOKE FAILED — aborting before the batch.')
        sys.exit(1)

    def judge_one(job):
        x, ev = job
        return x, (ej.judge_engagement(ev, generate_fn=sonnet_generate) if ev else None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        judged = list(pool.map(judge_one, jobs))

    high = [(x, v) for x, v in judged if x['tier'] == 'high']
    mod = [(x, v) for x, v in judged if x['tier'] == 'moderate']
    print(f'dev [{ej.JUDGE_VERSION}] vs {CAL_PATH.name} ({len(rows)} rows)')
    print(f"  HIGH:     {score(high)}")
    print(f"  MODERATE: {score(mod)}")
    print(f"  pooled:   {score(judged)}")
    for src in ('dev48', 'gate35'):
        sub = [(x, v) for x, v in judged if x['source'] == src]
        print(f"  [{src}]:  {score(sub)}")
    print('--- misses ---')
    for x, v in judged:
        if v is not None and v['engaged'] != x['label_engaged']:
            print(f"  {x['source']} r{x['idx']:>2} [{x['tier'][:3]}] "
                  f"{x['injected'][:40]:<40} label={'E' if x['label_engaged'] else 'N'} "
                  f"judge={'E' if v['engaged'] else 'N'} — {v['rationale'][:160]}")


if __name__ == '__main__':
    main()
