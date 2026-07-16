"""Tests for recall_miss detection (front #1b) — the under-injection signal.

A recall_miss = a harvested correction that, fed as a query to the LIVE retriever, would
inject a topic at HIGH — yet that topic was NOT injected HIGH in the session the correction
came from. The KB demonstrably could have surfaced the relevant topic (by its own scoring
rules, HIGH band) and didn't, and the user had to correct the assistant.

Reusing retrieve() (rather than hand-rolled token overlap) inherits all the precision
machinery — catch-all demotion, distinctiveness guards, thresholds — and makes the
definition self-consistent with what the system actually injects. Binary PER CORRECTION
(one missed info-need), not one-per-matching-entry. Precision-first → an honest lower bound.

retrieve_fn is injected so these tests need no index/tuning.
"""
import recall_miss as rm


def _fake_retrieve(result_map):
    """A stand-in for memory_index.retrieve: correction_text -> retrieve result dict."""
    def fn(text, project=None):
        return result_map.get(text, {'top_tier': 'none', 'matches': []})
    return fn


# ---- detect_session_miss: the precision-first core (reuses live retrieval) ----

def test_miss_when_correction_would_inject_high_but_session_did_not():
    retrieve = _fake_retrieve({
        'the widget cache must be cleared after regen':
            {'top_tier': 'high', 'matches': [{'slug': 'widget-cache-regen', 'score': 0.8}]},
    })
    m = rm.detect_session_miss('the widget cache must be cleared after regen',
                               'acme', set(), retrieve)
    assert m == {'slug': 'widget-cache-regen', 'score': 0.8}


def test_no_miss_when_retrieve_is_below_high():
    # If the live retriever wouldn't inject anything HIGH for this correction, not a miss.
    retrieve = _fake_retrieve({'x': {'top_tier': 'moderate', 'matches': [{'slug': 's', 'score': 0.5}]}})
    assert rm.detect_session_miss('x', 'p', set(), retrieve) is None


def test_no_miss_when_topic_was_surfaced_high():
    # We DID inject it HIGH that session -> not a miss.
    retrieve = _fake_retrieve({'x': {'top_tier': 'high', 'matches': [{'slug': 's', 'score': 0.8}]}})
    assert rm.detect_session_miss('x', 'p', {'s'}, retrieve) is None


# ---- high_injects_by_session: the topics we DID surface ----

def test_high_injects_by_session_groups_only_high_tier():
    rows = [
        {'session_id': 's1', 'tier': 'high', 'injected': 'a'},
        {'session_id': 's1', 'tier': 'moderate', 'advertised': ['b']},
        {'session_id': 's1', 'tier': 'high', 'injected': 'c'},
        {'session_id': 's2', 'tier': 'none'},
    ]
    assert rm.high_injects_by_session(rows) == {'s1': {'a', 'c'}}


# ---- recall_misses: binary per correction, orchestrated over sessions ----

def test_recall_misses_binary_per_correction_with_session_and_project():
    retrieve = _fake_retrieve({
        'corr-a': {'top_tier': 'high', 'matches': [{'slug': 'topic-a', 'score': 0.8}]},
        'corr-b': {'top_tier': 'none', 'matches': []},
    })
    sessions = [('s1', 'acme', 'corr-a'), ('s2', 'acme', 'corr-b')]
    out = rm.recall_misses(sessions, injection_rows=[], retrieve_fn=retrieve)
    assert out == [{'session_id': 's1', 'project': 'acme', 'slug': 'topic-a', 'score': 0.8}]


def test_recall_misses_excludes_surfaced_topic():
    retrieve = _fake_retrieve({'corr-a': {'top_tier': 'high', 'matches': [{'slug': 'topic-a', 'score': 0.8}]}})
    sessions = [('s1', 'acme', 'corr-a')]
    rows = [{'session_id': 's1', 'tier': 'high', 'injected': 'topic-a'}]
    assert rm.recall_misses(sessions, injection_rows=rows, retrieve_fn=retrieve) == []


# ---- score_correction / stats: symmetric correction-population recall (Task 6) ----

def test_score_correction_hit_when_top_slug_was_injected():
    fn = lambda text, project: {'top_tier': 'high',
                                'matches': [{'slug': 'tailscale-acl', 'score': 0.9}]}
    assert rm.score_correction('x', 'p', {'tailscale-acl'}, fn) == 'hit'


def test_score_correction_miss_when_not_injected():
    fn = lambda text, project: {'top_tier': 'high',
                                'matches': [{'slug': 'tailscale-acl', 'score': 0.9}]}
    assert rm.score_correction('x', 'p', set(), fn) == 'miss'


def test_score_correction_none_when_retriever_would_not_fire():
    fn = lambda text, project: {'top_tier': 'moderate', 'matches': []}
    assert rm.score_correction('x', 'p', set(), fn) is None


def test_stats_unattributable_for_unknown_session(monkeypatch):
    # session s-old predates session_id logging: NOT in the injection log at all.
    monkeypatch.setattr(rm, 'load_sessions_with_corrections',
                        lambda: [('s-old', 'p', 'correction text')])
    monkeypatch.setattr(rm, 'load_injection_rows',
                        lambda: [{'session_id': 's-new', 'tier': 'high', 'injected': 'a'}])
    fn = lambda text, project: {'top_tier': 'high',
                                'matches': [{'slug': 'a', 'score': 0.9}]}
    s = rm.stats(retrieve_fn=fn)
    assert s == {'hits': 0, 'misses': 0, 'unattributable': 1, 'not_fired': 0, 'details': []}


def test_stats_counts_not_fired_when_retriever_would_not_fire_high(monkeypatch):
    # F6: a correction the retriever wouldn't fire HIGH for is neither a hit nor a
    # miss (score_correction returns None) — it should be counted, not silently
    # dropped, so 'no scored corrections' is distinguishable from 'nothing to score'.
    monkeypatch.setattr(rm, 'load_sessions_with_corrections',
                        lambda: [('s1', 'p', 'unrelated text')])
    monkeypatch.setattr(rm, 'load_injection_rows',
                        lambda: [{'session_id': 's1', 'tier': 'high', 'injected': 'a'}])
    fn = lambda text, project: {'top_tier': 'moderate', 'matches': []}
    s = rm.stats(retrieve_fn=fn)
    assert s == {'hits': 0, 'misses': 0, 'unattributable': 0, 'not_fired': 1, 'details': []}
