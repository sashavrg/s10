#!/usr/bin/env python3
"""Eval-set train/val/held-out split (self-improvement front #1a).

Gate-1 requires a never-inspected held-out slice. This assigns each eval case a `split`,
persisted into `evals/cases.jsonl`, with four properties:

  * **stratified by `kind`** — each split gets representative paraphrase/direct/negative,
    so metrics on train/val/held-out are comparable (project×kind strata are too small to
    split, so stratify the SPLIT coarsely by kind; stratify the REPORTING finely — that's
    run_eval's job);
  * **deterministic** — ordered by sha1 of the durable case id (NOT Python's per-process
    salted hash()), so the seeding is reproducible;
  * **idempotent + preserves existing** — a case that already has a `split` keeps it, so
    re-running never reshuffles and the split is stable as the set grows;
  * **durable** — keyed on the stable case id, so a future `component` (component-under-test)
    tag annotates existing cases in place (a join on id), never a re-derivation.

Held-out is RESERVED: run_eval reports it separately and the tuning loop must not optimize
against it. The split is safe to add now, blind to the eventual component tag.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CASES_PATH = BASE_DIR / 'evals' / 'cases.jsonl'

SPLITS = ('train', 'val', 'held_out')          # order also breaks assignment ties (train first)
FRACTIONS = {'val': 0.2, 'held_out': 0.2}      # train = the remainder
STRATIFY_KEY = 'kind'


def stable_bucket(case_id: str) -> float:
    """A deterministic value in [0, 1) from the durable case id — reproducible across
    processes (sha1, not the salted built-in hash())."""
    h = hashlib.sha1(str(case_id).encode()).hexdigest()
    return int(h[:8], 16) / 0x100000000


def _targets(n: int) -> dict:
    """Target count per split for a stratum of n cases; train absorbs rounding."""
    held = round(FRACTIONS['held_out'] * n)
    val = round(FRACTIONS['val'] * n)
    return {'held_out': held, 'val': val, 'train': n - held - val}


def assign_splits(cases: list[dict], stratify_key: str = STRATIFY_KEY) -> list[dict]:
    """Return cases with `split` filled where missing. Existing splits preserved; counts
    per stratum hit the 60/20/20 targets; deterministic by stable_bucket(id)."""
    from collections import defaultdict
    strata = defaultdict(list)
    for c in cases:
        strata[c.get(stratify_key)].append(c)

    assigned: dict = {}
    for _, items in strata.items():
        targets = _targets(len(items))
        have = {s: 0 for s in SPLITS}
        unassigned = []
        for c in items:
            if c.get('split') in SPLITS:
                assigned[id(c)] = c['split']
                have[c['split']] += 1
            else:
                unassigned.append(c)
        # deterministic order, then greedily fill whichever split is most under target
        for c in sorted(unassigned, key=lambda c: stable_bucket(c.get('id'))):
            # max deficit; ties -> earlier split in SPLITS (train, then val, then held_out)
            pick = max(SPLITS, key=lambda s: (targets[s] - have[s], -SPLITS.index(s)))
            assigned[id(c)] = pick
            have[pick] += 1

    return [{**c, 'split': assigned[id(c)]} for c in cases]


# --------------------------------------------------------------------------- file I/O

def seed_file(path: Path | str = CASES_PATH) -> dict:
    """Assign splits to every case in cases.jsonl and rewrite it, preserving comment/blank
    lines verbatim. Returns the resulting split counts. Idempotent."""
    path = Path(path)
    raw = path.read_text().splitlines()
    parsed = []            # (line_index, case_dict) for JSON lines
    cases = []
    for i, line in enumerate(raw):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        c = json.loads(s)
        parsed.append(i)
        cases.append(c)

    filled = assign_splits(cases)
    by_line = dict(zip(parsed, filled))

    out_lines = []
    counts: dict = {}
    for i, line in enumerate(raw):
        if i in by_line:
            c = by_line[i]
            counts[c['split']] = counts.get(c['split'], 0) + 1
            out_lines.append(json.dumps(c))
        else:
            out_lines.append(line)
    path.write_text('\n'.join(out_lines) + '\n')
    return counts


def main() -> None:
    counts = seed_file()
    total = sum(counts.values())
    print(f"eval_split: {total} cases -> " +
          ", ".join(f"{s}={counts.get(s, 0)}" for s in SPLITS))


if __name__ == '__main__':
    main()
