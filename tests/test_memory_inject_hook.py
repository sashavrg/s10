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
