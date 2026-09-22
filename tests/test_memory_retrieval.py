"""The R seam must isolate stores/configs and preserve the retrieval contract."""
import json
import os

import pytest

import embedding_rescorer as er
import memory_index as mi
import memory_retrieval as mr
import reranker


def entry(slug='widget-cache', projects=None):
    tokens = sorted(mi.tokenize(slug))
    return {'slug': slug, 'display': slug, 'projects': projects or [],
            'key_points': ['Settled fact', 'Another fact'],
            'match_tokens': tokens, 'slug_tokens': tokens,
            'path': f'compiled/topics/{slug}.md'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Poison ambient paths: explicitly configured instances must never read them.
    live = tmp_path / 'live-index.json'
    live.write_text(json.dumps({'entries': [entry('private-live')]}))
    monkeypatch.setattr(mi, 'INDEX_PATH', live)
    live_tuning = tmp_path / 'live-tuning.yaml'
    live_tuning.write_text('thresholds:\n  high: 0.99\n')
    monkeypatch.setattr(mi, 'TUNING_PATH', live_tuning)
    monkeypatch.setattr(er, 'CACHE_PATH', tmp_path / 'live-embeddings.json')
    for key in list(os.environ):
        if key.startswith(('KB_RETRIEVAL', 'KB_EMBED_', 'KB_HYBRID_', 'KB_RERANK_')):
            monkeypatch.delenv(key)
    index = tmp_path / 'index.json'
    index.write_text(json.dumps({'entries': [entry(), entry(projects=['other'])]}))
    tuning = tmp_path / 'tuning.yaml'
    tuning.write_text('thresholds:\n  high: 0.73\n  moderate: 0.40\n')
    embeddings = tmp_path / 'embeddings.json'
    embeddings.write_text(json.dumps({'model': er.EMBED_MODEL,
                                      'topics': {'widget-cache': {'vec': [1.0, 0.0]}}}))
    monkeypatch.setattr(er, 'embed', lambda query: [1.0, 0.0])
    return dict(index_path=index, tuning_path=tuning, embedding_path=embeddings)


def test_instances_isolate_paths_tuning_and_mode(store, tmp_path, monkeypatch):
    monkeypatch.setenv('KB_RETRIEVAL', 'embedding')
    first = mr.MemoryRetriever(**store, mode='lexical')
    second_index = tmp_path / 'second.json'
    second_index.write_text(json.dumps({'entries': [entry('dns-blocking')]}))
    second = mr.MemoryRetriever(**{**store, 'index_path': second_index}, mode='lexical',
                                tuning=mi._deep_merge(mi.DEFAULT_TUNING, {
                                    'outcome_demoted': ['dns-blocking']}))
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    for _ in range(2):
        a = first.retrieve('widget cache', project='mine', max_facts=1, limit=1)
        b = second.retrieve('dns blocking')
        assert a['mode'] == b['mode'] == 'lexical'
        assert a['top_tier'] == 'high' and b['top_tier'] == 'moderate'
        assert [m['slug'] for m in a['matches']] == ['widget-cache']
        assert a['matches'][0]['key_points'] == ['Settled fact']
        assert [m['slug'] for m in b['matches']] == ['dns-blocking']
    assert os.environ['KB_RETRIEVAL'] == 'embedding'
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_long_lived_instance_reloads_files_and_per_call_tuning_wins(store):
    retriever = mr.MemoryRetriever(**store, mode='lexical')
    assert retriever.retrieve('widget cache')['top_tier'] == 'high'
    store['tuning_path'].write_text('thresholds:\n  high: 0.99\n')
    assert retriever.retrieve('widget cache')['top_tier'] == 'moderate'
    configured = mr.MemoryRetriever(**store, mode='lexical', tuning=mi.DEFAULT_TUNING)
    assert configured.retrieve('widget cache')['top_tier'] == 'high'
    demoted = mi._deep_merge(mi.DEFAULT_TUNING, {'outcome_demoted': ['widget-cache']})
    assert configured.retrieve('widget cache', tuning=demoted)['top_tier'] == 'moderate'
    store['index_path'].write_text('{"entries": []}')
    assert retriever.retrieve('widget cache')['matches'] == []


@pytest.mark.parametrize('mode', ['lexical', 'embedding', 'hybrid', 'rerank'])
def test_facade_matches_legacy_result_in_every_mode(store, monkeypatch, mode):
    monkeypatch.setattr(mi, 'INDEX_PATH', store['index_path'])
    monkeypatch.setattr(mi, 'TUNING_PATH', store['tuning_path'])
    monkeypatch.setattr(er, 'CACHE_PATH', store['embedding_path'])
    monkeypatch.setenv('KB_RETRIEVAL', mode)
    monkeypatch.setattr(reranker, 'rerank', lambda query, candidates: 0)
    legacy = mi.retrieve('widget cache', project='mine', max_facts=1, limit=1)
    assert legacy['mode'] == mode
    assert mr.MemoryRetriever(**store, mode=mode).retrieve(
        'widget cache', project='mine', max_facts=1, limit=1) == legacy
    assert mr.retrieve('widget cache', project='mine', max_facts=1, limit=1) == legacy


def test_explicit_missing_paths_never_read_live_data(store, tmp_path, monkeypatch):
    missing = tmp_path / 'missing'
    assert mr.MemoryRetriever(index_path=missing, mode='lexical').retrieve(
        'private live')['matches'] == []
    # Missing explicit tuning uses defaults, not the ambient high=0.99 file.
    configured = {**store, 'tuning_path': missing, 'embedding_path': missing}
    monkeypatch.setattr(er, 'CACHE_PATH', store['embedding_path'])
    monkeypatch.setattr(er, 'embed', lambda query: pytest.fail('missing cache must skip embed'))
    res = mr.MemoryRetriever(**configured, mode='embedding').retrieve('widget cache')
    assert res['mode'] == 'lexical' and res['top_tier'] == 'high'


def test_malformed_index_propagates_for_hook_error_logging(store):
    store['index_path'].write_text('not json')
    with pytest.raises(json.JSONDecodeError):
        mr.MemoryRetriever(**store).retrieve('widget cache')


@pytest.mark.parametrize('failure', ['embed', 'rerank'])
def test_backend_failures_keep_lexical_fallback_provenance(store, monkeypatch, failure):
    def fail(*args):
        raise TimeoutError('backend unavailable')
    monkeypatch.setattr(er if failure == 'embed' else reranker, failure, fail)
    res = mr.MemoryRetriever(**store, mode='rerank').retrieve('widget cache', project='mine')
    assert res['mode'] == 'lexical' and res['top_tier'] == 'high'
    assert res['rerank_timeout'] == (failure == 'rerank')
    if failure == 'rerank':
        assert res['retrieval_config'].startswith('rerank-')
    else:
        assert res['retrieval_config'] == 'lexical'


def test_default_mode_is_read_at_call_time(store, monkeypatch):
    retriever = mr.MemoryRetriever(**store)
    for mode in ('lexical', 'embedding', 'lexical'):
        monkeypatch.setenv('KB_RETRIEVAL', mode)
        assert retriever.retrieve('widget cache')['mode'] == mode


def test_explicit_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match='Unknown retrieval mode'):
        mr.MemoryRetriever(mode='typo')
