"""Tests for the shared per-topic governance record (front #2).

The record is L1 revealed-preferences AND the elfmem Beta-confidence/decay substrate,
fed idempotently from logs/injection_outcomes.jsonl. v1 BUILDS the record; retrieval/
archival consumption is deferred (see docs/borrow-map.md entry 2, sequencing note).

The crown-jewel property is idempotence: re-running the nightly over the (append-only)
outcome log yields identical records — alpha/beta/decay are pure functions of cumulative
counts, never incremented in place.
"""
import datetime as dt
import json
import math

import pytest

import topic_records as tr


def _out(slug, *, engaged, corrected, tier='high', project='proj',
         ts='2026-06-20T10:00:00', score=0.8):
    """Build one injection_outcomes.jsonl row (mirrors injection_outcome.append_outcomes)."""
    return {
        'session_id': 'sess', 'project': project, 'ts': ts, 'tier': tier,
        'injected': slug, 'score': score, 'engaged_in_assistant': engaged,
        'topic_corrected': corrected, 'n_user_turns': 5, 'scored_at': ts,
    }


# ---- verdict (must stay in lockstep with audit_hooks.classify_outcomes) ----

def test_outcome_verdict_useful_is_engaged_not_corrected():
    assert tr.outcome_verdict(_out('s', engaged=True, corrected=False)) == 'useful'


def test_outcome_verdict_harmful_is_engaged_and_corrected():
    assert tr.outcome_verdict(_out('s', engaged=True, corrected=True)) == 'harmful'


def test_outcome_verdict_neutral_is_not_engaged():
    assert tr.outcome_verdict(_out('s', engaged=False, corrected=False)) == 'neutral'
    # corrected-without-engagement is also neutral, not harmful
    assert tr.outcome_verdict(_out('s', engaged=False, corrected=True)) == 'neutral'


# ---- aggregation over the full log (both tiers count as evidence) ----

def test_aggregate_counts_per_slug_over_both_tiers():
    outcomes = [
        _out('s1', engaged=True, corrected=False, tier='high', ts='2026-06-20T10:00:00', project='a'),
        _out('s1', engaged=True, corrected=False, tier='moderate', ts='2026-06-22T10:00:00', project='b'),
        _out('s1', engaged=True, corrected=True, ts='2026-06-21T10:00:00', project='a'),
        _out('s1', engaged=False, corrected=False, project='a'),
        _out('s2', engaged=True, corrected=False),
    ]
    agg = tr.aggregate_outcomes(outcomes)
    assert agg['s1']['useful'] == 2          # both high+moderate engagements count
    assert agg['s1']['harmful'] == 1
    assert agg['s1']['neutral'] == 1
    assert agg['s1']['last_useful_ts'] == '2026-06-22T10:00:00'  # max ts among useful
    assert agg['s1']['projects'] == {'a', 'b'}
    assert agg['s2']['useful'] == 1


def test_aggregate_skips_rows_without_a_slug():
    agg = tr.aggregate_outcomes([{'tier': 'high', 'engaged_in_assistant': True}])
    assert agg == {}


# ---- build_record: alpha/beta from Jeffreys prior + counts ----

def test_build_record_beta_from_jeffreys_and_counts():
    agg = {'useful': 3, 'harmful': 1, 'neutral': 0,
           'last_useful_ts': '2026-06-22T10:00:00', 'projects': {'b', 'a'}}
    rec = tr.build_record(agg, tier='standard')
    assert rec['alpha'] == 0.5 + 3
    assert rec['beta'] == 0.5 + 1
    assert rec['reinforcement_count'] == 3
    assert rec['evidence'] == 4
    assert rec['last_reinforced'] == '2026-06-22T10:00:00'
    assert rec['tier'] == 'standard'
    assert rec['source'] == ['a', 'b']       # sorted provenance


def test_build_record_seeds_last_reinforced_from_compiled_at_when_no_useful():
    agg = {'useful': 0, 'harmful': 2, 'neutral': 1, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='standard', compiled_at='2026-01-15')
    assert rec['last_reinforced'] == '2026-01-15'


def test_build_record_last_reinforced_none_when_no_useful_and_no_seed():
    agg = {'useful': 0, 'harmful': 0, 'neutral': 3, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='standard')
    assert rec['last_reinforced'] is None


# ---- accelerate-decay on harmful (idempotent: pure function of harmful_count) ----

def test_build_record_accelerates_decay_per_harmful_event():
    base = tr.TIER_LAMBDA['standard']
    agg = {'useful': 0, 'harmful': 1, 'neutral': 0, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='standard')
    assert rec['decay_lambda'] == pytest.approx(base * 2)


def test_build_record_decay_is_capped():
    agg = {'useful': 0, 'harmful': 8, 'neutral': 0, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='standard')
    assert rec['decay_lambda'] == tr.DECAY_CAP   # base * 2**8 far exceeds the cap


def test_build_record_identity_tiers_are_decay_exempt():
    agg = {'useful': 0, 'harmful': 5, 'neutral': 0, 'last_useful_ts': None, 'projects': set()}
    for tier in ('durable', 'permanent'):
        rec = tr.build_record(agg, tier=tier)
        assert rec['decay_lambda'] == tr.TIER_LAMBDA[tier]   # no acceleration


def test_ephemeral_base_decay_not_clamped_by_cap():
    # ephemeral base λ = ln2/7 ≈ 0.099 > DECAY_CAP 0.05: with harmful=0 the record
    # must keep the tier's own λ (cap limits acceleration, not the tier definition).
    agg = {'useful': 1, 'harmful': 0, 'neutral': 0, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='ephemeral')
    assert abs(rec['decay_lambda'] - math.log(2) / 7) < 1e-9


def test_ephemeral_harmful_acceleration_capped_at_base():
    agg = {'useful': 0, 'harmful': 3, 'neutral': 0, 'last_useful_ts': None, 'projects': set()}
    rec = tr.build_record(agg, tier='ephemeral')
    assert abs(rec['decay_lambda'] - math.log(2) / 7) < 1e-9   # max(cap, base) = base


# ---- derived read helpers ----

def test_confidence_is_alpha_over_alpha_plus_beta():
    assert tr.confidence({'alpha': 3.5, 'beta': 1.5}) == 0.7


def test_recency_decays_exponentially():
    rec = {'last_reinforced': '2026-06-01', 'decay_lambda': 0.1}
    today = dt.date(2026, 6, 11)   # 10 days later
    assert tr.recency(rec, today) == pytest.approx(math.exp(-0.1 * 10))


def test_recency_parses_datetime_stamps():
    rec = {'last_reinforced': '2026-06-01T10:00:00', 'decay_lambda': 0.1}
    assert tr.recency(rec, dt.date(2026, 6, 1)) == pytest.approx(1.0)


def test_recency_is_one_when_never_reinforced():
    rec = {'last_reinforced': None, 'decay_lambda': 0.1}
    assert tr.recency(rec, dt.date(2026, 6, 11)) == 1.0


# ---- rebuild_records: orchestration + the idempotence guarantee ----

def test_rebuild_builds_one_record_per_seen_slug():
    outcomes = [
        _out('s1', engaged=True, corrected=False),
        _out('s2', engaged=False, corrected=False),
    ]
    records = tr.rebuild_records(outcomes)
    assert set(records) == {'s1', 's2'}
    assert records['s1']['alpha'] == 1.5
    assert records['s2']['alpha'] == 0.5     # no useful evidence yet


def test_rebuild_is_idempotent_over_the_same_log():
    outcomes = [
        _out('s1', engaged=True, corrected=False, ts='2026-06-20T10:00:00'),
        _out('s1', engaged=True, corrected=True, ts='2026-06-21T10:00:00'),
        _out('s2', engaged=False, corrected=False),
    ]
    once = tr.rebuild_records(outcomes)
    twice = tr.rebuild_records(outcomes, prev=once)   # re-run with prior state
    assert once == twice                              # no double-counting


def test_rebuild_preserves_manual_tier_override():
    outcomes = [_out('s1', engaged=True, corrected=False)]
    seeded = tr.rebuild_records(outcomes)
    seeded['s1']['tier'] = 'durable'                  # operator marks it identity-tier (L2)
    rebuilt = tr.rebuild_records(outcomes, prev=seeded)
    assert rebuilt['s1']['tier'] == 'durable'
    assert rebuilt['s1']['decay_lambda'] == tr.TIER_LAMBDA['durable']  # exemption honored


# ---- I/O wrappers (explicit paths; fail-open; atomic write) ----

def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / 'state' / 'topic_records.json'
    records = {'s1': {'alpha': 1.5, 'beta': 0.5, 'tier': 'standard'}}
    tr.save_records(records, path=path)
    assert tr.load_records(path=path) == records


def test_load_records_fails_open_on_missing_file(tmp_path):
    assert tr.load_records(path=tmp_path / 'nope.json') == {}


def test_load_records_fails_open_on_corrupt_json(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{not json')
    assert tr.load_records(path=path) == {}


def test_read_outcomes_skips_blank_and_bad_lines(tmp_path):
    path = tmp_path / 'logs' / 'injection_outcomes.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join([
        json.dumps(_out('s1', engaged=True, corrected=False)),
        '',
        'not json',
        json.dumps(_out('s2', engaged=False, corrected=False)),
    ]))
    rows = tr.read_outcomes(path=path)
    assert [r['injected'] for r in rows] == ['s1', 's2']
