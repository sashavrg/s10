#!/usr/bin/env python3
"""Shared per-topic governance record (self-improvement front #2).

ONE per-topic record, fed idempotently from ``logs/injection_outcomes.jsonl`` by the
nightly job, that is simultaneously:

  * **L1 revealed preferences** — what gets used grows (reinforcement_count, last_reinforced);
  * **the elfmem governance substrate** — a per-topic Beta-Binomial confidence
    (``alpha``/``beta``) + per-tier exponential decay (``decay_lambda``) that will later
    subsume the binary ``outcome_demoted`` list with a principled continuous signal.

See ``docs/borrow-map.md`` entry 2 (elfmem) for the design and the sequencing note. This is
**v1: it BUILDS and maintains the record**. Consuming it — wiring ``recency``/``confidence``
into retrieval scoring, archival/supersession, and the ``outcome_demoted -> confidence-floor``
migration — is deliberately deferred ("the migration happens when confidence exists, not before").
C2/C3 binary demotion keeps running untouched; the record accrues alongside until it is trusted.

**Idempotence is the load-bearing property.** ``alpha``/``beta``/``decay_lambda`` are pure
functions of cumulative outcome counts (``alpha = 0.5 + w*useful``,
``decay_lambda = min(base*2**harmful, cap)``), never incremented in place. Re-running the
nightly over the append-only outcome log therefore yields *identical* records — no watermark,
no double-counting. This mirrors the codebase's existing ``replace_list_section`` reversibility
(rewrite-the-set-each-run) rather than fighting it.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RECORDS_PATH = BASE_DIR / 'state' / 'topic_records.json'
OUTCOME_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'

# Beta-Binomial prior: Jeffreys (0.5, 0.5). confidence = a/(a+b); evidence = a+b-1.
JEFFREYS = 0.5
# Evidence weight per outcome. v1 = 1.0 (each outcome is one unit of evidence). Weighting by
# session size (n_user_turns) or retrieval score is a documented later tunable.
EVIDENCE_WEIGHT = 1.0

# Per-tier base decay lambda, per WALL-CLOCK DAY. elfmem's hour-scaled "active-hours" lambdas are
# reframed to KB-appropriate wall-clock half-lives (the borrow-map flags active-hours as optional).
# half-life = ln(2)/lambda. These are STORED but NOT consumed by retrieval in v1; values will be
# tuned when decay is actually wired in.
TIER_LAMBDA = {
    'permanent': math.log(2) / 3650,   # ~10 yr
    'durable':   math.log(2) / 180,    # ~6 mo
    'standard':  math.log(2) / 30,     # ~1 mo  (default)
    'ephemeral': math.log(2) / 7,      # ~1 wk
}
DEFAULT_TIER = 'standard'
# Identity tiers (operator-intent Layer 2): a manual semantic mark, exempt from accelerate-decay.
DECAY_EXEMPT = frozenset({'permanent', 'durable'})
# Accelerate-decay ceiling (per day), from elfmem's `min(lambda*2, 0.05)`.
DECAY_CAP = 0.05


# --------------------------------------------------------------------------- verdict

def outcome_verdict(row: dict) -> str:
    """Classify one outcome row. Kept in lockstep with audit_hooks.classify_outcomes:
    useful = engaged AND NOT corrected; harmful = engaged AND corrected; else neutral."""
    engaged = bool(row.get('engaged_in_assistant'))
    corrected = bool(row.get('topic_corrected'))
    if engaged and not corrected:
        return 'useful'
    if engaged and corrected:
        return 'harmful'
    return 'neutral'


# --------------------------------------------------------------------------- aggregation

def aggregate_outcomes(outcomes: list[dict]) -> dict:
    """Aggregate the full outcome log per slug (both high+moderate tiers count as evidence).

    Returns {slug: {useful, harmful, neutral, last_useful_ts, projects}}. Pure; idempotent —
    it derives cumulative counts from the log rather than mutating prior state.
    """
    agg: dict[str, dict] = {}
    for row in outcomes:
        slug = row.get('injected')
        if not slug:
            continue
        a = agg.get(slug)
        if a is None:
            a = agg[slug] = {'useful': 0, 'harmful': 0, 'neutral': 0,
                             'last_useful_ts': None, 'projects': set()}
        verdict = outcome_verdict(row)
        a[verdict] += 1
        project = row.get('project')
        if project:
            a['projects'].add(project)
        if verdict == 'useful':
            ts = row.get('ts')
            if ts and (a['last_useful_ts'] is None or ts > a['last_useful_ts']):
                a['last_useful_ts'] = ts
    return agg


# --------------------------------------------------------------------------- record build

def build_record(agg: dict, *, tier: str = DEFAULT_TIER,
                 compiled_at: str | None = None) -> dict:
    """Derive a per-topic record from its aggregates. Pure & idempotent: every field is a
    function of (cumulative counts, tier, seed) — nothing is incremented in place."""
    useful = agg['useful']
    harmful = agg['harmful']
    base_lambda = TIER_LAMBDA.get(tier, TIER_LAMBDA[DEFAULT_TIER])
    if tier in DECAY_EXEMPT:
        decay_lambda = base_lambda
    else:
        # decay_lambda = min(base * 2**harmful, max(cap, base)): one doubling per
        # harmful event, but the cap only ceilings how far that ACCELERATION can
        # climb — max(cap, base) means the ceiling never pulls a tier's own base
        # lambda down below where it started (it only bites once cap > base).
        # Computed in closed form so re-runs are identical.
        decay_lambda = min(base_lambda * (2 ** harmful), max(DECAY_CAP, base_lambda))
    last_reinforced = agg['last_useful_ts'] or compiled_at
    return {
        'alpha': JEFFREYS + EVIDENCE_WEIGHT * useful,
        'beta': JEFFREYS + EVIDENCE_WEIGHT * harmful,
        'evidence': useful + harmful,
        'reinforcement_count': useful,
        'last_reinforced': last_reinforced,
        'decay_lambda': decay_lambda,
        'tier': tier,
        'source': sorted(agg['projects']),
    }


def rebuild_records(outcomes: list[dict], *, prev: dict | None = None,
                    index: dict | None = None) -> dict:
    """Rebuild every per-topic record from the full outcome log. The ONLY thing carried over
    from ``prev`` is a manual ``tier`` override (operator-intent L2 identity marks); all numeric
    fields are recomputed, so the result is idempotent under re-runs. ``index`` (optional, unused
    in v1) maps slug -> compiled_at to seed ``last_reinforced`` for topics with no useful events.
    """
    prev = prev or {}
    index = index or {}
    records: dict[str, dict] = {}
    for slug, agg in aggregate_outcomes(outcomes).items():
        tier = prev.get(slug, {}).get('tier', DEFAULT_TIER)
        records[slug] = build_record(agg, tier=tier, compiled_at=index.get(slug))
    return records


# --------------------------------------------------------------------------- derived reads

def confidence(record: dict) -> float:
    """Beta posterior mean: alpha / (alpha + beta)."""
    a, b = record['alpha'], record['beta']
    return a / (a + b)


def recency(record: dict, today: dt.date) -> float:
    """Exponential recency exp(-lambda * delta_days) since last_reinforced. Returns 1.0 when the
    topic has never been reinforced/seeded (unknown age -> don't penalize). NOT consumed by
    retrieval in v1 — present so the next layer (decay/archival) doesn't re-derive it."""
    stamp = record.get('last_reinforced')
    if not stamp:
        return 1.0
    last = dt.date.fromisoformat(str(stamp)[:10])
    delta_days = max((today - last).days, 0)
    return math.exp(-record['decay_lambda'] * delta_days)


# --------------------------------------------------------------------------- I/O (fail-open)

def read_outcomes(path: Path | str = OUTCOME_PATH) -> list[dict]:
    """Parse injection_outcomes.jsonl; skip blank/malformed lines (fail-open)."""
    rows: list[dict] = []
    try:
        text = Path(path).read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_records(path: Path | str = RECORDS_PATH) -> dict:
    """Load the per-topic records; fail-open to {} on missing/corrupt file."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_records(records: dict, path: Path | str = RECORDS_PATH) -> None:
    """Atomically write the records (tmp + os.replace) with stable key order for clean git diffs.
    Atomic write guards against truncation on disk-pressure — a real failure mode on this host."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- nightly entrypoint

def main() -> None:
    """Nightly: rebuild the per-topic records from the outcome log, idempotently. Invoked by
    run_pipeline.sh right after audit_hooks.py (same cadence, same outcome-log signal)."""
    outcomes = read_outcomes()
    prev = load_records()
    records = rebuild_records(outcomes, prev=prev)
    save_records(records)
    print(f"topic_records: {len(records)} topics from {len(outcomes)} outcomes")


if __name__ == '__main__':
    main()
