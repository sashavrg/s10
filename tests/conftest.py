import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'scripts'))


@pytest.fixture
def kb_root(tmp_path, monkeypatch):
    """Redirect kb.py's module-level paths to tmp_path so tests are isolated."""
    import kb

    monkeypatch.setattr(kb, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(kb, 'RAW_WEB_DIR', tmp_path / 'raw' / 'web')
    monkeypatch.setattr(kb, 'RAW_INBOX_DIR', tmp_path / 'raw' / 'inbox')
    monkeypatch.setattr(kb, 'COMPILED_SOURCES_DIR', tmp_path / 'compiled' / 'sources')
    monkeypatch.setattr(kb, 'COMPILED_TOPICS_DIR', tmp_path / 'compiled' / 'topics')
    monkeypatch.setattr(kb, 'REVIEW_DIR', tmp_path / 'review')
    monkeypatch.setattr(kb, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(kb, 'LOGS_DIR', tmp_path / 'logs')
    monkeypatch.setattr(kb, 'CONFIG_DIR', tmp_path / 'config')
    monkeypatch.setattr(kb, 'TOPIC_CONFIG_PATH', tmp_path / 'config' / 'topics.yaml')
    monkeypatch.setattr(kb, 'MANIFEST_PATH', tmp_path / 'state' / 'manifest.json')
    kb.ensure_dirs()
    return kb
