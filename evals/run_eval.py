#!/usr/bin/env python3
"""Retrieval / injection eval harness for the memory system.

Runs a golden set of (prompt -> expected topic slug(s)) cases through
memory_retrieval.retrieve() and reports retrieval quality. Scorer-agnostic: it calls
the real retrieve(), so whichever scorer that path uses (lexical today, the
embedding rescorer behind KB_RETRIEVAL=embedding later) is what gets measured.
A/B = run under each scorer, then `compare` the two saved result files.

Metrics (positives = cases with an expected slug; negatives = expect empty):
  hit@1     positive's TOP match is HIGH-tier AND an expected slug (would inject correctly)
  recall@k  an expected slug appears in the top-k matches, ANY tier
            -> the discriminating metric. Lexical scores 0 with no token overlap,
               so paraphrases miss; embeddings should still rank them.
  retrieved an expected slug appears ANYWHERE in the ranked matches (recall ceiling)
  FP        negatives that surfaced a HIGH match (should have stayed silent)

Usage:
  python evals/run_eval.py run [--cases evals/cases.jsonl] [--label lexical] [--k 5]
  python evals/run_eval.py compare evals/results/lexical.json evals/results/embedding.json
"""
import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'scripts'))
import memory_index as mi  # noqa: E402
import memory_retrieval  # noqa: E402

CASES_DEFAULT = BASE / 'evals' / 'cases.jsonl'
if not CASES_DEFAULT.exists():  # fresh clone: fall back to the synthetic example set
    CASES_DEFAULT = BASE / 'evals' / 'cases.example.jsonl'
RESULTS_DIR = BASE / 'evals' / 'results'


def load_cases(path):
    cases = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        cases.append(json.loads(line))
    return cases


def first_rank(matches, expect):
    for i, m in enumerate(matches):
        if m['slug'] in expect:
            return i
    return None


def run_cases(cases, k):
    index_slugs = {e['slug'] for e in mi.load_index()['entries']}
    rows, warnings = [], []
    for c in cases:
        expect = c.get('expect') or []
        for s in expect:
            if s not in index_slugs:
                warnings.append(f"{c.get('id', '?')}: expect slug not in index -> {s}")
        res = memory_retrieval.retrieve(c['prompt'], project=c.get('project'))
        matches = res['matches']
        top = matches[0] if matches else None
        rank = first_rank(matches, expect) if expect else None
        is_neg = not expect
        # tier of the (first) expected match -> would this fact actually be injected?
        expect_tier = next((m['tier'] for m in matches if m['slug'] in expect), None)
        rows.append({
            'id': c.get('id'),
            'kind': c.get('kind'),
            'project': c.get('project'),
            'split': c.get('split'),
            'expect': expect,
            'top_slug': top['slug'] if top else None,
            'top_tier': res['top_tier'],
            'top_score': round(top['score'], 3) if top else 0.0,
            'rank_of_expect': rank,                         # None = never retrieved
            'expect_tier': expect_tier,
            'in_topk': rank is not None and rank < k,
            'recall_at_high': expect_tier == 'high',        # reaches the injectable band
            'hit_at_1': bool(top and top['tier'] == 'high' and top['slug'] in expect),
            'retrieved': rank is not None,
            'false_high': bool(is_neg and res['top_tier'] == 'high'),
        })
    return rows, warnings


def _frac(num, den):
    return f'{num}/{den} ({round(100 * num / den)}%)' if den else 'n/a'


def _metrics(rows):
    """Standard metric block for any subset of rows (positives drive retrieval metrics,
    negatives drive the false-HIGH rate). Reused for overall / by_kind / by_split /
    by_project_kind so every slice is measured identically."""
    pos = [r for r in rows if r['expect']]
    neg = [r for r in rows if not r['expect']]
    m: dict = {'n': len(rows), 'n_pos': len(pos), 'n_neg': len(neg)}
    if pos:
        m['recall_at_high'] = _frac(sum(r['recall_at_high'] for r in pos), len(pos))
        m['recall_at_k'] = _frac(sum(r['in_topk'] for r in pos), len(pos))
        m['hit_at_1'] = _frac(sum(r['hit_at_1'] for r in pos), len(pos))
        m['retrieved'] = _frac(sum(r['retrieved'] for r in pos), len(pos))
    if neg:
        m['false_high_on_negatives'] = _frac(sum(r['false_high'] for r in neg), len(neg))
    return m


def summarize(rows, k):
    pos = [r for r in rows if r['expect']]
    neg = [r for r in rows if not r['expect']]
    by_kind = {kind: _metrics([r for r in rows if r['kind'] == kind])
               for kind in ('paraphrase', 'direct', 'negative')
               if any(r['kind'] == kind for r in rows)}
    # gate-1: held_out reported separately (and never tuned against)
    by_split = {sp: _metrics([r for r in rows if r.get('split') == sp])
                for sp in ('train', 'val', 'held_out')
                if any(r.get('split') == sp for r in rows)}
    # operator ask: does a config generalize across clients (project) × query-shapes (kind)?
    cells = sorted({(r.get('project'), r['kind']) for r in rows})
    by_project_kind = {f"{proj}·{kind}": _metrics(
        [r for r in rows if r.get('project') == proj and r['kind'] == kind])
        for proj, kind in cells}
    return {
        'k': k,
        'n_positive': len(pos),
        'n_negative': len(neg),
        'overall': _metrics(rows),
        'by_kind': by_kind,
        'by_split': by_split,
        'by_project_kind': by_project_kind,
    }


def cmd_run(args):
    cases = load_cases(args.cases)
    rows, warnings = run_cases(cases, args.k)
    summary = summarize(rows, args.k)

    for w in warnings:
        print(f'  ! WARN {w}')
    print(f'\n=== per-case (label={args.label}, k={args.k}) ===')
    for r in rows:
        mark = '·'
        if r['expect']:
            mark = 'HIT' if r['hit_at_1'] else ('top%d' % (r['rank_of_expect'] + 1) if r['retrieved'] else 'MISS')
        else:
            mark = 'FP!' if r['false_high'] else 'ok'
        print(f"  {r['id']:24} {r['kind']:11} {mark:6} tier={r['top_tier']:8} score={r['top_score']:.3f}  top={r['top_slug']}")

    print(f'\n=== summary (label={args.label}) ===')
    print(json.dumps(summary, indent=2))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f'{args.label}.json'
    out.write_text(json.dumps({'label': args.label, 'k': args.k, 'summary': summary, 'rows': rows}, indent=2))
    print(f'\nsaved -> {out.relative_to(BASE)}')


def cmd_compare(args):
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    print(f"=== {a['label']}  vs  {b['label']} ===")
    print(f"{'metric':28} {a['label']:>18} {b['label']:>18}")
    for key in ('recall_at_high', 'recall_at_k', 'hit_at_1', 'retrieved', 'false_high_on_negatives'):
        print(f"  {key:26} {a['summary']['overall'].get(key, 'n/a'):>18} {b['summary']['overall'].get(key, 'n/a'):>18}")
    for kind in ('paraphrase', 'direct'):
        ak = a['summary']['by_kind'].get(kind, {})
        bk = b['summary']['by_kind'].get(kind, {})
        if ak or bk:
            print(f"  [{kind}] recall_at_k {ak.get('recall_at_k', 'n/a'):>18} {bk.get('recall_at_k', 'n/a'):>18}")

    # per-case flips
    arows = {r['id']: r for r in a['rows']}
    print('\n--- per-case changes (rank of expected; lower is better) ---')
    for rb in b['rows']:
        ra = arows.get(rb['id'])
        if not ra or not rb['expect']:
            continue
        ranka = (ra['rank_of_expect'] + 1) if ra['retrieved'] else None
        rankb = (rb['rank_of_expect'] + 1) if rb['retrieved'] else None
        if ranka != rankb:
            print(f"  {rb['id']:24} {str(ranka):>5} -> {str(rankb):>5}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--cases', default=str(CASES_DEFAULT))
    r.add_argument('--label', default='lexical')
    r.add_argument('--k', type=int, default=5)
    r.set_defaults(func=cmd_run)
    c = sub.add_parser('compare')
    c.add_argument('a')
    c.add_argument('b')
    c.set_defaults(func=cmd_compare)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
