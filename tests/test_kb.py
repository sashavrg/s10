import threading
import time

import pytest


# ---------- pure-logic helpers (no kb_root needed) ----------

def test_normalize_bullets_strips_markers_and_drops_none():
    import kb
    out = kb.normalize_bullets(['- first', '* second', '- None', 'n/a', '- explicit thing.'])
    assert out == ['first', 'second', 'explicit thing.']


def test_extract_bullets_h2_h3_and_bold_headings():
    import kb
    md_h2 = "## Open questions\n- one\n- two\n## Other\n- skip\n"
    md_h3 = "### Open questions\n- one\n- two\n### Next\n- skip\n"
    md_bold = "**Open questions**\n- one\n- two\n\n**Next section**\n- skip\n"
    md_case = "## open QUESTIONS\n- one\n- two\n"
    for md in (md_h2, md_h3, md_bold, md_case):
        assert kb.extract_bullets('Open questions', md) == ['one', 'two'], md


def test_extract_bullets_returns_empty_when_section_absent():
    import kb
    assert kb.extract_bullets('Open questions', "## Summary\n- alpha\n") == []


def test_slugify_and_canonical_topic_slug_with_alias(kb_root):
    cfg = {'aliases': {'py': 'programming-python'}, 'ignored': [], 'manual_topics': {}, 'pinned': {}}
    assert kb_root.canonical_topic_slug('Py', cfg) == 'programming-python'
    assert kb_root.canonical_topic_slug('Programming Python', cfg) == 'programming-python'


def test_normalize_topics_honors_ignored_and_manual(kb_root):
    cfg = {
        'aliases': {'py': 'programming-python'},
        'ignored': ['noise'],
        'manual_topics': {'src-1': ['Forced Topic']},
        'pinned': {'programming-python': 'Programming: Python'},
    }
    out = kb_root.normalize_topics(['Py', 'noise', 'Other'], source_id='src-1', config=cfg)
    # 'noise' dropped, 'py' canonicalized + pinned display name, manual added, 'Other' kept
    assert 'Programming: Python' in out
    assert 'Forced Topic' in out
    assert 'Other' in out
    assert all('noise' not in t.lower() for t in out)


# ---------- frontmatter round-trip ----------

def test_frontmatter_round_trip(tmp_path):
    import kb
    meta = {'a': 1, 'b': 'hello', 'c': None}
    body = "# Title\n\nbody text"
    path = tmp_path / 'doc.md'
    path.write_text(kb.frontmatter(meta, body))
    out_meta, out_body = kb.read_frontmatter_doc(path)
    assert out_meta['a'] == 1
    assert out_meta['b'] == 'hello'
    assert out_body.strip() == body.strip()


# ---------- manifest + dedupe ----------

def test_add_file_dedupes_by_original_path(kb_root, tmp_path):
    src = tmp_path / 'note.md'
    src.write_text('initial body')
    rec1 = kb_root.add_file(str(src))
    sources = kb_root.load_manifest()['sources']
    assert len(sources) == 1
    initial_hash = rec1['content_hash']

    # Re-ingest same path with unchanged content: still 1 source, no flags flipped.
    kb_root.add_file(str(src))
    sources = kb_root.load_manifest()['sources']
    assert len(sources) == 1
    assert sources[0]['content_hash'] == initial_hash

    # Modify content and re-ingest: still 1 source, hash updated, needs_summary flipped.
    src.write_text('updated body')
    kb_root.add_file(str(src))
    sources = kb_root.load_manifest()['sources']
    assert len(sources) == 1
    assert sources[0]['content_hash'] != initial_hash
    assert sources[0]['needs_summary'] is True
    assert sources[0]['status'] == 'changed'


def test_detect_changed_sources_does_not_advance_last_seen_hash(kb_root, tmp_path):
    src = tmp_path / 'note.md'
    src.write_text('original')
    rec = kb_root.add_file(str(src))
    # Simulate a successful prior summary: needs_summary=False, last_seen_hash matches.
    rec['needs_summary'] = False
    rec['last_seen_hash'] = rec['content_hash']
    rec['status'] = 'compiled'
    kb_root.update_source_record(rec)

    # Mutate the raw file directly (bypassing add_file's dedupe path).
    raw_path = kb_root.BASE_DIR / rec['raw_path']
    meta, _ = kb_root.read_frontmatter_doc(raw_path)
    raw_path.write_text(kb_root.frontmatter(meta, 'mutated body'))

    changed = kb_root.detect_changed_sources()
    assert len(changed) == 1
    stored = kb_root.load_manifest()['sources'][0]
    # content_hash advances to new hash, but last_seen_hash stays at the old one
    # so a crash before summarize will be re-detected next run.
    assert stored['content_hash'] != stored['last_seen_hash']
    assert stored['needs_summary'] is True


# ---------- inbox: move-after-success ----------

def test_ingest_inbox_file_moves_after_record_written(kb_root):
    inbox = kb_root.RAW_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    item = inbox / 'idea.md'
    item.write_text('inbox body')

    results = kb_root.sync_inbox()
    assert len(results) == 1
    assert not item.exists()
    assert (inbox / 'processed' / 'idea.md').exists()
    assert len(kb_root.load_manifest()['sources']) == 1


def test_ingest_inbox_file_leaves_file_in_place_on_failure(kb_root, monkeypatch):
    inbox = kb_root.RAW_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    item = inbox / 'idea.md'
    item.write_text('inbox body')

    def boom(*a, **kw):
        raise RuntimeError('forced failure')

    monkeypatch.setattr(kb_root, 'ingest_text_body', boom)

    with pytest.raises(RuntimeError):
        kb_root.sync_inbox()

    # Still in the inbox, not in processed/.
    assert item.exists()
    assert not (inbox / 'processed' / 'idea.md').exists()


# ---------- manifest_lock cross-process semantics ----------

def test_manifest_lock_is_exclusive(kb_root):
    timeline = []

    def worker(label, hold_seconds):
        with kb_root.manifest_lock():
            timeline.append(('enter', label, time.monotonic()))
            time.sleep(hold_seconds)
            timeline.append(('exit', label, time.monotonic()))

    t1 = threading.Thread(target=worker, args=('A', 0.15))
    t2 = threading.Thread(target=worker, args=('B', 0.05))
    t1.start()
    time.sleep(0.02)  # ensure A grabs the lock first
    t2.start()
    t1.join()
    t2.join()

    # Verify the two critical sections did not interleave.
    events = [(kind, label) for kind, label, _ in timeline]
    assert events == [('enter', 'A'), ('exit', 'A'), ('enter', 'B'), ('exit', 'B')]


# ---------- cloud backend: spec parsing ----------

def test_parse_model_spec_recognizes_backends():
    import kb
    assert kb.parse_model_spec('claude:opus') == ('claude', 'opus')
    assert kb.parse_model_spec('claude:sonnet') == ('claude', 'sonnet')
    assert kb.parse_model_spec('ollama:qwen3:8b') == ('ollama', 'qwen3:8b')


def test_parse_model_spec_defaults_to_ollama_for_bare_and_unknown():
    import kb
    assert kb.parse_model_spec('qwen2.5-coder:7b') == ('ollama', 'qwen2.5-coder:7b')
    assert kb.parse_model_spec('llama3.2:3b') == ('ollama', 'llama3.2:3b')
    assert kb.parse_model_spec('claude:') == ('ollama', 'claude:')


# ---------- cloud backend: claude_code_generate ----------

def test_classify_cloud_error_buckets():
    import kb
    assert kb.classify_cloud_error("You've hit your weekly limit · resets Mon") == 'quota'
    assert kb.classify_cloud_error('Not logged in · Please run /login') == 'auth'
    assert kb.classify_cloud_error('API Error: 401 Unauthorized') == 'auth'
    assert kb.classify_cloud_error('getaddrinfo failed: connection refused') == 'network'
    assert kb.classify_cloud_error('some other failure') == 'other'


def test_claude_code_generate_returns_result_field(monkeypatch):
    import json as _json
    import types
    import kb

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ['claude', '-p']
        assert '--output-format' in cmd and 'json' in cmd
        assert kwargs.get('input') == 'PROMPT'
        return types.SimpleNamespace(returncode=0, stdout=_json.dumps({'result': '  hello  '}), stderr='')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    assert kb.claude_code_generate('sonnet', 'PROMPT') == 'hello'


def test_claude_code_generate_raises_classified_error_on_nonzero(monkeypatch):
    import types
    import kb

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=1, stdout='', stderr="You've hit your session limit · resets 3:45pm")

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'quota'


def test_claude_code_generate_missing_binary_is_cloud_error(monkeypatch):
    import kb

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError('claude')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'other'


def test_claude_code_generate_timeout_is_network_error(monkeypatch):
    import kb

    def fake_run(cmd, **kwargs):
        raise kb.subprocess.TimeoutExpired(cmd, kb.CLAUDE_CLI_TIMEOUT)

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'network'


def test_claude_code_generate_bad_json_is_other_error(monkeypatch):
    import types
    import kb

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout='not json at all', stderr='')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'other'


def test_claude_code_generate_is_error_payload_classifies_reason(monkeypatch):
    import json as _json
    import types
    import kb

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=_json.dumps({'is_error': True, 'result': "You've hit your weekly limit"}), stderr='')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'quota'


def test_claude_code_generate_empty_result_is_other_error(monkeypatch):
    import json as _json
    import types
    import kb

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=_json.dumps({'result': '   '}), stderr='')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    with pytest.raises(kb.CloudLLMError) as ei:
        kb.claude_code_generate('opus', 'p')
    assert ei.value.reason == 'other'


# ---------- cloud backend: run report ----------

def test_write_run_report_records_fallbacks(kb_root):
    import json as _json
    kb_root.RUN_FALLBACKS.clear()
    kb_root.record_fallback(stage='summary', item_id='src_a', primary='claude:sonnet', reason='quota')
    kb_root.record_fallback(stage='topic', item_id='topic-x', primary='claude:opus', reason='network')
    path = kb_root.write_run_report({'summarized': 5, 'rebuilt_topics': 2})
    data = _json.loads(path.read_text())
    assert data['counts'] == {'summarized': 5, 'rebuilt_topics': 2, 'fallbacks': 2}
    assert {f['reason'] for f in data['fallbacks']} == {'quota', 'network'}
    kb_root.RUN_FALLBACKS.clear()


# ---------- cloud backend: dispatcher ----------

def test_llm_generate_ollama_path_calls_ollama(monkeypatch):
    import kb
    calls = {}

    def fake_ollama(model, prompt, num_ctx=kb.DEFAULT_NUM_CTX):
        calls['model'] = model
        return 'OLLAMA'

    monkeypatch.setattr(kb, 'ollama_generate', fake_ollama)
    out = kb.llm_generate('qwen2.5-coder:7b', None, 'p', stage='summary', item_id='x')
    assert out == 'OLLAMA'
    assert calls['model'] == 'qwen2.5-coder:7b'


def test_llm_generate_cloud_success_records_no_fallback(monkeypatch):
    import kb
    kb.RUN_FALLBACKS.clear()
    monkeypatch.setattr(kb, 'claude_code_generate', lambda model, prompt: 'CLOUD')
    out = kb.llm_generate('claude:sonnet', 'qwen2.5-coder:7b', 'p', stage='summary', item_id='x')
    assert out == 'CLOUD'
    assert kb.RUN_FALLBACKS == []


def test_llm_generate_falls_back_and_records_on_cloud_error(monkeypatch):
    import kb
    kb.RUN_FALLBACKS.clear()

    def boom(model, prompt):
        raise kb.CloudLLMError('quota', 'session limit')

    monkeypatch.setattr(kb, 'claude_code_generate', boom)
    monkeypatch.setattr(kb, 'ollama_generate', lambda model, prompt, num_ctx=kb.DEFAULT_NUM_CTX: 'LOCAL')
    out = kb.llm_generate('claude:sonnet', 'qwen2.5-coder:7b', 'p', stage='summary', item_id='src_a')
    assert out == 'LOCAL'
    assert len(kb.RUN_FALLBACKS) == 1
    assert kb.RUN_FALLBACKS[0] == {'stage': 'summary', 'item_id': 'src_a', 'primary': 'claude:sonnet', 'reason': 'quota'}
    kb.RUN_FALLBACKS.clear()


def test_llm_generate_reraises_when_no_fallback(monkeypatch):
    import kb

    def boom(model, prompt):
        raise kb.CloudLLMError('auth', 'login')

    monkeypatch.setattr(kb, 'claude_code_generate', boom)
    with pytest.raises(kb.CloudLLMError):
        kb.llm_generate('claude:opus', None, 'p', stage='topic', item_id='t')


# ---------- cloud backend: call-site wiring ----------

def test_summarize_routes_through_llm_generate_with_fallback(kb_root, tmp_path, monkeypatch):
    import kb
    captured = {}

    def fake_llm_generate(primary, fallback, prompt, *, stage, item_id, num_ctx=kb_root.DEFAULT_NUM_CTX):
        captured['primary'] = primary
        captured['fallback'] = fallback
        captured['stage'] = stage
        captured['item_id'] = item_id
        return "## Suggested topics\n- Testing\n\n## Open questions\n- None\n"

    src = tmp_path / 'note.md'
    src.write_text('a body to summarize')
    rec = kb_root.add_file(str(src))

    monkeypatch.setattr(kb, 'llm_generate', fake_llm_generate)
    out = kb_root.summarize(rec['source_id'], model='claude:sonnet', fallback='qwen2.5-coder:7b')

    assert captured == {
        'primary': 'claude:sonnet',
        'fallback': 'qwen2.5-coder:7b',
        'stage': 'summary',
        'item_id': rec['source_id'],
    }
    assert out['status'] == 'compiled'
    assert (kb_root.COMPILED_SOURCES_DIR / f"{rec['source_id']}.md").exists()


# ---------- cloud backend: run_pipeline report ----------

def test_run_pipeline_clears_fallbacks_and_writes_report(kb_root):
    import json as _json
    kb_root.RUN_FALLBACKS.append({'stage': 'stale', 'item_id': 'x', 'primary': 'claude:opus', 'reason': 'quota'})
    result = kb_root.run_pipeline()
    assert result['fallbacks'] == 0
    report_path = kb_root.STATE_DIR / 'last_run_report.json'
    assert report_path.exists()
    data = _json.loads(report_path.read_text())
    assert data['counts']['fallbacks'] == 0


# ---------- cloud backend: configurable ollama endpoint ----------

def test_ollama_base_url_defaults_to_localhost(monkeypatch):
    import kb
    monkeypatch.delenv('KB_OLLAMA_URL', raising=False)
    assert kb.ollama_base_url() == 'http://127.0.0.1:11434'


def test_ollama_base_url_honors_env_and_strips_trailing_slash(monkeypatch):
    import kb
    monkeypatch.setenv('KB_OLLAMA_URL', 'http://100.64.0.2:11434/')
    assert kb.ollama_base_url() == 'http://100.64.0.2:11434'
