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


# --- cloud backend (haiku rung, 2026-08-01) ---------------------------------------
# KB_RERANK_BACKEND=claude routes the judge call to the pinned cloud model via
# kb.claude_code_payload. The pin is assert-and-abort (A1, applied at the START
# this time): a payload reporting any other model raises — upstream that is
# ok=False -> bounded lexical fallback -> no verdict from an unpinned model is
# ever accepted. The call carries the rerank deadline, not kb's pipeline-scale
# CLAUDE_CLI_TIMEOUT, or a hung API call would blow the hook's 10s kill and
# re-create the dropped-row bug.

def _cloud(monkeypatch, payload_or_exc):
    import kb
    monkeypatch.setenv('KB_RERANK_BACKEND', 'claude')
    calls = {}

    def fake_payload(model, prompt, timeout=None):
        calls.update(model=model, timeout=timeout)
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        return payload_or_exc
    monkeypatch.setattr(kb, 'claude_code_payload', fake_payload)
    return calls


def test_cloud_backend_returns_result_on_correct_pin(monkeypatch):
    calls = _cloud(monkeypatch, {'modelUsage': {rr.RERANK_CLOUD_MODEL_ID: {}},
                                 'result': ' 3 '})
    assert rr._generate('prompt') == '3'
    assert calls['model'] == rr.RERANK_CLOUD_MODEL_ID
    assert calls['timeout'] == rr.RERANK_TIMEOUT


def test_cloud_backend_raises_on_pin_violation(monkeypatch):
    _cloud(monkeypatch, {'modelUsage': {'claude-sonnet-5': {}}, 'result': '3'})
    import pytest
    with pytest.raises(rr.RerankError, match='pin'):
        rr._generate('prompt')


def test_cloud_backend_wraps_transport_failure(monkeypatch):
    import kb
    _cloud(monkeypatch, kb.CloudLLMError('network', 'timed out'))
    import pytest
    with pytest.raises(rr.RerankError):
        rr._generate('prompt')


def test_default_backend_is_still_ollama(monkeypatch):
    monkeypatch.delenv('KB_RERANK_BACKEND', raising=False)
    seen = {}
    monkeypatch.setattr(rr, '_generate_ollama', lambda p: (seen.update(p=p), 'x')[1])
    assert rr._generate('prompt') == 'x'
    assert seen['p'] == 'prompt'
