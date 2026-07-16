"""Tests for deterministic rerank-shadow sampling (suspension window, adjustment #1).

The rerank pass spikes the 7B judge per would-inject turn — a standing GPU cost over a
weeks-long collection window on a box that's also used for gaming. We sample it to ~20%,
but DETERMINISTICALLY by session_id (hash -> bottom quintile), NOT a per-turn random draw,
so the later counterfactual join (shadow verdict × session-depth) isn't biased by which
turns happened to be sampled: a session is entirely in or entirely out.
"""
import shadow_retrieval as sr


def test_rerank_sampled_is_deterministic():
    assert sr._rerank_sampled('sess-abc') == sr._rerank_sampled('sess-abc')


def test_rerank_sampled_rate_one_includes_everything():
    assert sr._rerank_sampled('any-session', rate=1.0) is True


def test_rerank_sampled_rate_zero_includes_nothing():
    assert sr._rerank_sampled('any-session', rate=0.0) is False


def test_rerank_sampled_excludes_missing_session():
    # No session_id -> can't join to depth later -> don't spend GPU on it.
    assert sr._rerank_sampled(None) is False
    assert sr._rerank_sampled('') is False


def test_rerank_sampled_is_a_session_level_quintile():
    # Uniform sha1 bucket -> ~20% of distinct sessions sampled (within tolerance).
    ids = [f'session-{i}' for i in range(2000)]
    frac = sum(1 for s in ids if sr._rerank_sampled(s)) / len(ids)
    assert 0.15 < frac < 0.25


def test_rerank_sampled_whole_session_in_or_out():
    # Determinism means every turn of a given session gets the same verdict.
    sid = 'session-42'
    decisions = {sr._rerank_sampled(sid) for _ in range(5)}
    assert len(decisions) == 1
