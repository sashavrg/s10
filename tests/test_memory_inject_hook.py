import json
import subprocess
import sys
from pathlib import Path

import memory_inject_hook as h

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_is_synthetic_true_for_known_prefixes():
    assert h.is_synthetic('<task-notification>\n<task-id>abc</task-id>')
    assert h.is_synthetic('<system-reminder> stuff')
    assert h.is_synthetic('[Request interrupted by user]')
    assert h.is_synthetic('<command-name>/compact</command-name>')
    assert h.is_synthetic('<local-command-stdout>done</local-command-stdout>')
    assert h.is_synthetic('   <task-notification> leading ws')   # lstrip applied


def test_is_synthetic_false_for_real_prompt():
    assert not h.is_synthetic('how do I wire up the nimbus auth token refresh')
    assert not h.is_synthetic('fix the <div> rendering bug')   # angle bracket mid-text


def test_synthetic_prompt_injects_nothing_end_to_end(tmp_path):
    # A synthetic prompt must produce NO stdout (nothing injected), regardless of
    # what is in the index — the guard returns before touching memory_index.
    payload = json.dumps({'prompt': '<task-notification>\n<task-id>x</task-id>',
                          'cwd': '/home/u/acme-portal-api'})
    proc = subprocess.run([sys.executable, str(REPO_ROOT / 'scripts' / 'memory_inject_hook.py')],
                          input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ''


# --- Task 9 flip: per-row scorer provenance + the no-dropped-row invariant --------
# Every would-inject turn must produce a log row naming the scorer that ACTUALLY
# ran (retrieval_mode, + rerank_timeout when the judge call failed and lexical was
# injected instead). And NO code path may end in a dropped row — a retrieve()
# crash logs tier='error' instead of vanishing (fail-open still: nothing injected).

import io


def _drive(monkeypatch, tmp_path, res_or_exc, prompt='real user question about widgets'):
    """Run h.main() with a stubbed retrieve; return the parsed log rows + stdout."""
    import memory_retrieval
    monkeypatch.delenv('KB_HEADLESS', raising=False)
    log = tmp_path / 'memory_injection.jsonl'
    monkeypatch.setattr(h, 'LOG_PATH', log)
    if isinstance(res_or_exc, Exception):
        def stub(q, project=None):
            raise res_or_exc
    else:
        def stub(q, project=None):
            return res_or_exc
    monkeypatch.setattr(memory_retrieval, 'retrieve', stub)
    payload = {'prompt': prompt, 'cwd': '/home/u/proj', 'session_id': 'sess-1'}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    h.main()
    rows = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return rows, out.getvalue()


def _res(tier, mode='rerank', timeout=False):
    m = {'slug': 's1', 'display': 'S1', 'score': 0.9, 'tier': tier,
         'projects': [], 'key_points': ['fact'], 'path': '/tmp/s1.md'}
    return {'mode': mode, 'rerank_timeout': timeout, 'top_tier': tier, 'matches': [m]}


def test_high_row_carries_scorer_provenance(monkeypatch, tmp_path):
    rows, out = _drive(monkeypatch, tmp_path, _res('high', mode='rerank'))
    assert out.strip()                                   # injected
    assert rows[0]['tier'] == 'high'
    assert rows[0]['retrieval_mode'] == 'rerank'
    assert 'rerank_timeout' not in rows[0]               # only present when true


def test_timeout_fallback_row_names_lexical_and_flags_timeout(monkeypatch, tmp_path):
    rows, out = _drive(monkeypatch, tmp_path, _res('high', mode='lexical', timeout=True))
    assert out.strip()                                   # the LEXICAL result injected
    assert rows[0]['retrieval_mode'] == 'lexical'        # the scorer that ACTUALLY ran
    assert rows[0]['rerank_timeout'] is True


def test_none_tier_row_still_carries_provenance(monkeypatch, tmp_path):
    rows, out = _drive(monkeypatch, tmp_path, _res('none', mode='lexical', timeout=True))
    assert out.strip() == ''                             # silent turn
    assert rows[0]['tier'] == 'none'
    assert rows[0]['rerank_timeout'] is True             # ...but the row still lands


def test_retrieve_crash_logs_an_error_row_not_a_drop(monkeypatch, tmp_path):
    rows, out = _drive(monkeypatch, tmp_path, RuntimeError('index exploded'))
    assert out.strip() == ''                             # fail-open: nothing injected
    assert rows and rows[0]['tier'] == 'error'           # ...but the row LANDS
    assert 'RuntimeError' in rows[0]['reason']
    assert rows[0]['session_id'] == 'sess-1'
