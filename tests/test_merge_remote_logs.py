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


# ------------------------------------------------- transcript sync (gate A2)
# Merged outcome rows are only *judgeable* if their transcript is reachable on
# this machine; without this the server's rows count toward accrual but can
# never be labeled or judged. Pure selection logic only — no SSH here.

def test_outcome_sessions_collects_distinct_ids_and_ignores_junk():
    lines = ['{"session_id":"s1","ts":"t1"}',
             '',
             '   ',
             'not json at all',
             '{"ts":"t2"}',                    # sessionless row
             '{"session_id":"","ts":"t3"}',    # empty id
             '{"session_id":"s1","ts":"t4"}',  # dupe
             '{"session_id":"s2","ts":"t5"}']
    assert m.outcome_sessions(lines) == {'s1', 's2'}


def test_sessions_missing_transcripts_skips_locally_resolvable():
    resolve = lambda sid: '/tx/found.jsonl' if sid == 's1' else None
    assert m.sessions_missing_transcripts({'s2', 's1', 's3'}, resolve) == ['s2', 's3']


def test_sessions_missing_transcripts_empty_when_all_resolve():
    assert m.sessions_missing_transcripts({'s1'}, lambda sid: '/tx') == []


def test_transcript_fetch_list_maps_listing_to_relative_paths():
    listing = ['-root/s2.jsonl', '-root/s9.jsonl', '-mnt-storage/s3.jsonl',
               'no-extension', '', 'top-level.jsonl']
    # only wanted sessions, sorted for a deterministic rsync file list
    assert m.transcript_fetch_list(listing, ['s3', 's2']) == \
        ['-mnt-storage/s3.jsonl', '-root/s2.jsonl']


def test_transcript_fetch_list_takes_first_of_duplicate_session_dirs():
    listing = ['-b-dir/s1.jsonl', '-a-dir/s1.jsonl']
    assert m.transcript_fetch_list(listing, ['s1']) == ['-a-dir/s1.jsonl']


def test_transcript_fetch_list_empty_when_nothing_wanted():
    assert m.transcript_fetch_list(['-root/s1.jsonl'], []) == []
