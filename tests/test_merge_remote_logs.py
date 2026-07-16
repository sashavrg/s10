"""Tests for merge_remote_logs — the remote collection-log folder.

Covers the pure dedup contract and the append/idempotence of merge_one. No SSH:
pull_remote_logs (rsync) is exercised operationally by the wrapper, not here.
"""
import merge_remote_logs as m


def test_new_rows_dedups_against_existing():
    existing = ['{"ts":"a"}', '{"ts":"b"}']
    incoming = ['{"ts":"b"}', '{"ts":"c"}']  # b already present
    assert m.new_rows(existing, incoming) == ['{"ts":"c"}']


def test_new_rows_dedups_within_incoming_and_drops_blanks():
    incoming = ['{"ts":"x"}', '', '  ', '{"ts":"x"}', '{"ts":"y"}']
    # first x kept, blanks dropped, second x collapsed, order preserved
    assert m.new_rows([], incoming) == ['{"ts":"x"}', '{"ts":"y"}']


def test_new_rows_strips_before_compare():
    # a trailing-newline/space variant of an existing row is NOT re-added
    assert m.new_rows(['{"ts":"a"}'], ['  {"ts":"a"}  ']) == []


def test_new_rows_idempotent():
    existing = ['{"ts":"a"}']
    incoming = ['{"ts":"a"}', '{"ts":"b"}']
    first = m.new_rows(existing, incoming)
    assert first == ['{"ts":"b"}']
    # feed the merged state back in with the same incoming → nothing new
    assert m.new_rows(existing + first, incoming) == []


def test_merge_one_appends_then_is_a_noop(tmp_path, monkeypatch):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'memory_injection.jsonl').write_text('{"ts":"pc1"}\n', encoding='utf-8')
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / 'memory_injection.jsonl').write_text(
        '{"ts":"pc1"}\n{"ts":"srv1"}\n{"ts":"srv2"}\n', encoding='utf-8')
    monkeypatch.setattr(m, 'LOGS_DIR', logs)

    # first merge: pc1 already present, srv1/srv2 are new
    assert m.merge_one('memory_injection.jsonl', staging, dry_run=False) == 2
    lines = (logs / 'memory_injection.jsonl').read_text(encoding='utf-8').splitlines()
    assert lines == ['{"ts":"pc1"}', '{"ts":"srv1"}', '{"ts":"srv2"}']

    # second merge over the same staging: idempotent no-op
    assert m.merge_one('memory_injection.jsonl', staging, dry_run=False) == 0
    assert (logs / 'memory_injection.jsonl').read_text(encoding='utf-8').splitlines() == lines


def test_merge_one_dry_run_writes_nothing(tmp_path, monkeypatch):
    logs = tmp_path / 'logs'
    logs.mkdir()
    dest = logs / 'injection_outcomes.jsonl'
    dest.write_text('{"ts":"pc1"}\n', encoding='utf-8')
    staging = tmp_path / 'staging'
    staging.mkdir()
    (staging / 'injection_outcomes.jsonl').write_text('{"ts":"srv1"}\n', encoding='utf-8')
    monkeypatch.setattr(m, 'LOGS_DIR', logs)

    assert m.merge_one('injection_outcomes.jsonl', staging, dry_run=True) == 1
    assert dest.read_text(encoding='utf-8') == '{"ts":"pc1"}\n'  # unchanged


def test_merge_one_missing_staged_file_is_zero(tmp_path, monkeypatch):
    logs = tmp_path / 'logs'
    logs.mkdir()
    staging = tmp_path / 'staging'
    staging.mkdir()
    monkeypatch.setattr(m, 'LOGS_DIR', logs)
    # remote never produced this log yet (e.g. injection_outcomes pre-first-SessionEnd)
    assert m.merge_one('injection_outcomes.jsonl', staging, dry_run=False) == 0
