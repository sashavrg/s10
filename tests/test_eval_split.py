"""Tests for eval-set split assignment (front #1a) — the gate-1 train/val/held-out split.

The split is stratified by `kind` (so each split has representative paraphrase/direct/
negative), deterministic (sha1 of the durable case id, not salted hash()), idempotent
(existing splits preserved), and hits target counts per stratum. Persisted into
cases.jsonl so a future `component` tag annotates existing cases in place (a join on the
durable id), never a re-derivation. Held-out is reserved: never tuned against.
"""
import eval_split as es


def _cases(kind, n, prefix='c'):
    return [{'id': f'{prefix}-{kind}-{i}', 'kind': kind, 'expect': ['x']} for i in range(n)]


# ---- stable_bucket: deterministic, salted-hash-free ----

def test_stable_bucket_is_deterministic_and_in_unit_range():
    a = es.stable_bucket('para-stale-widget')
    b = es.stable_bucket('para-stale-widget')
    assert a == b
    assert 0.0 <= a < 1.0
    assert es.stable_bucket('other-id') != a   # different id -> different bucket (overwhelmingly)


# ---- assign_splits: target counts per stratum ----

def test_assign_splits_hits_60_20_20_counts_within_a_kind():
    out = es.assign_splits(_cases('paraphrase', 10))
    counts = {}
    for c in out:
        counts[c['split']] = counts.get(c['split'], 0) + 1
    assert counts == {'train': 6, 'val': 2, 'held_out': 2}


def test_assign_splits_gives_small_strata_representation():
    # 'direct' has only 4 cases — each split must still be represented (1/1/2-ish), not all-train
    out = es.assign_splits(_cases('direct', 4))
    counts = {}
    for c in out:
        counts[c['split']] = counts.get(c['split'], 0) + 1
    assert counts.get('held_out', 0) >= 1
    assert counts.get('val', 0) >= 1
    assert sum(counts.values()) == 4


# ---- stratified: each kind split independently ----

def test_assign_splits_stratifies_by_kind():
    cases = _cases('paraphrase', 5, 'p') + _cases('negative', 5, 'n')
    out = es.assign_splits(cases)
    held = [c for c in out if c['split'] == 'held_out']
    kinds = {c['kind'] for c in held}
    assert kinds == {'paraphrase', 'negative'}   # both kinds represented in held_out


# ---- idempotent + preserves existing (the durability guarantee) ----

def test_assign_splits_preserves_existing_split():
    cases = _cases('paraphrase', 10)
    cases[0]['split'] = 'held_out'               # a hand-pinned case
    out = es.assign_splits(cases)
    assert next(c for c in out if c['id'] == cases[0]['id'])['split'] == 'held_out'


def test_assign_splits_is_idempotent():
    once = es.assign_splits(_cases('paraphrase', 10))
    twice = es.assign_splits(once)
    assert [c['split'] for c in once] == [c['split'] for c in twice]


def test_assign_splits_is_deterministic_across_fresh_calls():
    a = es.assign_splits(_cases('paraphrase', 10))
    b = es.assign_splits(_cases('paraphrase', 10))
    assert {c['id']: c['split'] for c in a} == {c['id']: c['split'] for c in b}


def test_real_cases_have_unique_ids_and_every_case_placed():
    """Guard the golden set: the durable id is the future component-tag join key (must be
    unique), and every case must carry a valid split (re-run eval_split after adding cases).
    On a fresh clone with no local cases.jsonl, the shipped example set is checked instead."""
    import json
    path = es.CASES_PATH if es.CASES_PATH.exists() \
        else es.CASES_PATH.with_name('cases.example.jsonl')
    cases = [json.loads(s) for s in
             (ln.strip() for ln in path.read_text().splitlines())
             if s and not s.startswith('#')]
    ids = [c.get('id') for c in cases]
    assert all(ids), 'every case needs a durable id'
    assert len(set(ids)) == len(ids), 'case ids must be unique (the component-tag join key)'
    assert all(c.get('split') in es.SPLITS for c in cases), 'every case must be placed in a split'
