"""reranker is the LLM precision judge. For it to be viable as a live, ambiguity-
gated stage it must stay WARM between calls (keep_alive) — a cold 7B reload is the
~24s that made it too slow inline. And its parsing contract (winner index / None)
must be exact, since None == 'inject nothing'."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import reranker as rr  # noqa: E402


def test_payload_sets_keep_alive_and_deterministic_options():
    p = rr._payload("hello")
    assert p['keep_alive']                       # judge kept warm between gated calls
    assert p['options']['temperature'] == 0.0    # deterministic verdict
    assert p['options']['num_ctx'] == 4096
    assert p['model'] == rr.RERANK_MODEL


def test_keep_alive_is_env_overridable(monkeypatch):
    monkeypatch.setenv('KB_RERANK_KEEPALIVE', '30m')
    assert rr._payload('x')['keep_alive'] == '30m'


def test_rerank_parses_winner_index(monkeypatch):
    monkeypatch.setattr(rr, '_generate', lambda prompt: 'Answer: 2')
    assert rr.rerank('q', [{'slug': 'a'}, {'slug': 'b'}, {'slug': 'c'}]) == 1


def test_rerank_zero_means_none(monkeypatch):
    monkeypatch.setattr(rr, '_generate', lambda prompt: '0')
    assert rr.rerank('q', [{'slug': 'a'}]) is None


def test_rerank_out_of_range_is_none(monkeypatch):
    monkeypatch.setattr(rr, '_generate', lambda prompt: '9')
    assert rr.rerank('q', [{'slug': 'a'}]) is None


def test_rerank_empty_candidates_is_none():
    assert rr.rerank('q', []) is None


def test_prompt_biases_toward_none():
    # On real traffic most turns are conversational/workflow with no relevant topic;
    # the judge must be pushed to answer 0 rather than grab the nearest candidate.
    p = rr._build_prompt('merge the branch to main', [{'slug': 'a', 'display': 'Topic A'}])
    low = p.lower()
    assert '0' in p                                       # the 'none' option exists
    assert 'directly' in low                              # require DIRECT relevance
    assert ('when in doubt' in low) or ('no relevant' in low)  # explicit none-bias
