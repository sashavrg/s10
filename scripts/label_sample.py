#!/usr/bin/env python3
"""Hand-label sample generator + scorer: grounds the v2 `engaged` proxy in
operator judgment (the seed of the #1e operator-grounded judge).

`make`  — sample ~40 rows stratified by v1/v2 agreement (disagreements first:
          that's where the information is), render a checklist markdown to
          evals/candidates/engagement-labels.md. Deterministic via
          eval_split.stable_bucket — NO randomness, reruns are stable.
`score` — parse the operator's yes/no labels back and print precision/recall
          of the v2 proxy against them.

The output file is DATA (evals/candidates/ is gitignored) — never commit it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_split import stable_bucket
from scorecard import read_outcomes

BASE_DIR = Path(__file__).resolve().parent.parent
V1_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
V2_PATH = BASE_DIR / 'logs' / 'injection_outcomes_rescored.jsonl'
OUT_PATH = BASE_DIR / 'evals' / 'candidates' / 'engagement-labels.md'


def _key(r: dict) -> tuple:
    return (r.get('session_id'), r.get('ts'), r.get('injected'))


def select_sample(v1_rows: list[dict], v2_rows: list[dict],
                  per_stratum: tuple = (20, 10, 10)) -> list[dict]:
    """Stratified sample for hand-labeling. F1: the HIGH arm gets its own uncapped
    'high' stratum — EVERY joined tier=='high' row is force-included regardless of
    v1/v2 agreement, so the arm the whole thesis is about is never left out of a
    capped sample. Those rows are excluded from the other three strata (no dupes).
    F9: V1_PATH may by now contain post-flip LIVE v2 rows (tagged scorer_version);
    those are not true v1 rows and must not dilute the disagree/agree strata."""
    v1 = {_key(r): r for r in v1_rows if 'scorer_version' not in r}
    strata: dict[str, list[dict]] = {
        'high': [], 'disagree': [], 'agree_engaged': [], 'agree_not': []}
    for r2 in v2_rows:
        r1 = v1.get(_key(r2))
        if r1 is None:
            continue
        if r2.get('tier') == 'high':
            strata['high'].append(r2)
            continue
        e1, e2 = bool(r1.get('engaged_in_assistant')), bool(r2.get('engaged_in_assistant'))
        if e1 != e2:
            strata['disagree'].append(r2)
        elif e2:
            strata['agree_engaged'].append(r2)
        else:
            strata['agree_not'].append(r2)

    def _sorted(rows):
        return sorted(rows, key=lambda r: stable_bucket('|'.join(map(str, _key(r)))))

    out = [dict(r, stratum='high') for r in _sorted(strata['high'])]
    for name, cap in zip(('disagree', 'agree_engaged', 'agree_not'), per_stratum):
        rows = _sorted(strata[name])
        out += [dict(r, stratum=name) for r in rows[:cap]]
    return out


def render_md(sample: list[dict]) -> str:
    lines = [
        '# Engagement-proxy hand labels', '',
        'For each row: did the assistant ACTUALLY engage the injected/advertised topic',
        'in that session (label_engaged), and was the topic corrected (label_corrected)?',
        'Replace each `?` with `yes` or `no`. Then run:',
        '    python scripts/label_sample.py score', '',
    ]
    for i, r in enumerate(sample, 1):
        lines += [
            f'## {i}. `{r.get("injected")}`  [{r.get("tier")}] '
            f'(stratum: {r["stratum"]})',
            f'- session: `{r.get("session_id")}`  ts: {r.get("ts")}',
            f'- v2 proxy said engaged={r.get("engaged_in_assistant")} '
            f'(matched: {", ".join(r.get("engaged_matched") or []) or "—"})',
            '- label_engaged: ?',
            '- label_corrected: ?', '',
        ]
    return '\n'.join(lines)


def parse_labels(md_text: str) -> list[dict]:
    rows, cur = [], None
    for line in md_text.splitlines():
        line = line.strip()
        if line.startswith('## '):
            if cur:
                rows.append(cur)
            slug = line.split('`')[1] if '`' in line else ''
            m = re.search(r'\(stratum: (\w+)\)', line)
            cur = {'injected': slug, 'stratum': m.group(1) if m else None}
        elif cur is not None and line.startswith('- v2 proxy said'):
            cur['engaged_in_assistant'] = 'engaged=True' in line
        elif cur is not None and line.startswith('- session:'):
            cur['session_id'] = line.split('`')[1] if '`' in line else ''
        elif cur is not None and line.startswith('- label_engaged:'):
            v = line.split(':', 1)[1].strip().lower()
            if v in ('yes', 'no'):
                cur['label_engaged'] = (v == 'yes')
        elif cur is not None and line.startswith('- label_corrected:'):
            v = line.split(':', 1)[1].strip().lower()
            if v in ('yes', 'no'):
                cur['label_corrected'] = (v == 'yes')
    if cur:
        rows.append(cur)
    return [r for r in rows if 'label_engaged' in r]


def _score(labeled: list[dict]) -> dict:
    tp = sum(1 for r in labeled if r['engaged_in_assistant'] and r['label_engaged'])
    fp = sum(1 for r in labeled if r['engaged_in_assistant'] and not r['label_engaged'])
    fn = sum(1 for r in labeled if not r['engaged_in_assistant'] and r['label_engaged'])
    return {
        'n': len(labeled),
        'engaged_precision': round(tp / (tp + fp), 4) if tp + fp else None,
        'engaged_recall': round(tp / (tp + fn), 4) if tp + fn else None,
    }


def score_labels(labeled: list[dict]) -> dict:
    """F5: overall precision/recall PLUS a per-stratum breakdown (rows without a
    parsed stratum — e.g. hand-edited labels — bucket under 'unknown')."""
    by_stratum: dict[str, list[dict]] = {}
    for r in labeled:
        by_stratum.setdefault(r.get('stratum') or 'unknown', []).append(r)
    result = _score(labeled)
    result['by_stratum'] = {k: _score(v) for k, v in sorted(by_stratum.items())}
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('make', 'score'))
    args = ap.parse_args()
    if args.cmd == 'make':
        sample = select_sample(read_outcomes(V1_PATH), read_outcomes(V2_PATH))
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(render_md(sample))
        print(f'wrote {len(sample)} rows -> {OUT_PATH} — label them, then `score`')
    else:
        labeled = parse_labels(OUT_PATH.read_text())
        print(score_labels(labeled))


if __name__ == '__main__':
    main()
