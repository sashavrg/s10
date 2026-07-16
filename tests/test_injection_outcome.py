"""Tests for the injection-outcome scorer (the utility/reward signal substrate).

Pure-logic only: no Ollama, no network, no real transcripts. Functions accept
path params so tmp files stand in for logs/state.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import injection_outcome as io  # noqa: E402


def test_tokenize_drops_short_and_stopwords():
    toks = io.tokenize('The OpenRGB shutdown fix needs sizes.ors')
    assert {'openrgb', 'shutdown', 'sizes'} <= toks
    assert 'the' not in toks   # stopword
    assert 'ors' in toks       # 3 chars, kept


def test_tokenize_and_slug_tokens_handle_none():
    assert io.tokenize(None) == set()
    assert io.slug_tokens(None) == set()


def test_slug_tokens_splits_on_hyphen():
    assert io.slug_tokens('openrgb-shutdown-fix') == {'openrgb', 'shutdown', 'fix'}
    # bare project-name catch-all slug yields a single weak token
    assert io.slug_tokens('acme') == {'acme'}


def test_read_session_injections_filters_by_session_and_tier(tmp_path):
    log = tmp_path / 'memory_injection.jsonl'
    rows = [
        {'tier': 'high', 'session_id': 'S1', 'injected': 'a-b'},
        {'tier': 'none', 'session_id': 'S1', 'best_score': 0.1},        # dropped (tier)
        {'tier': 'moderate', 'session_id': 'S1', 'advertised': ['c-d']},
        {'tier': 'high', 'session_id': 'S2', 'injected': 'e-f'},        # dropped (session)
        {'tier': 'skipped', 'session_id': 'S1', 'reason': 'synthetic'}, # dropped (tier)
    ]
    log.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    got = io.read_session_injections('S1', log_path=log)
    assert [r.get('injected') or r.get('advertised') for r in got] == ['a-b', ['c-d']]


def test_compute_outcomes_engagement_and_correction():
    rows = [
        {'session_id': 'S1', 'project': 'p', 'ts': 't', 'tier': 'high',
         'injected': 'openrgb-shutdown-fix', 'score': 0.9,
         'injected_facts': ['zero-size ARGB zones must be resized before blacking']},
        # off-topic injection: tokens never appear in the assistant text
        {'session_id': 'S1', 'project': 'p', 'ts': 't2', 'tier': 'high',
         'injected': 'monitoring-solutions', 'score': 0.95, 'injected_facts': []},
    ]
    turns = [
        {'role': 'user', 'text': 'why do my leds stay on'},
        {'role': 'assistant', 'text': 'the openrgb shutdown service resizes the zones'},
    ]
    correction_text = 'Corrected: the shutdown fix needs sizes.ors rewritten'
    out = io.compute_outcomes(rows, turns, correction_text)

    assert out[0]['engaged_in_assistant'] is True    # 'openrgb'/'shutdown' in assistant turn
    assert out[0]['topic_corrected'] is True          # 'shutdown' overlaps the correction
    assert out[1]['engaged_in_assistant'] is False    # off-topic inject, never engaged
    assert out[1]['topic_corrected'] is False
    assert all(o['n_user_turns'] == 1 for o in out)


def test_v2_engaged_is_word_boundary_not_substring():
    rows = [{'session_id': 's', 'tier': 'high', 'injected': 'api-gateway-config',
             'ts': 't', 'injected_facts': []}]
    turns = [{'role': 'assistant', 'text': 'the therapist reconfigured nothing'}]
    out = io.compute_outcomes(rows, turns, '')
    assert out[0]['engaged_in_assistant'] is False


def test_v2_engaged_requires_quorum():
    rows = [{'session_id': 's', 'tier': 'high', 'injected': 'ecs-rollout-polling',
             'ts': 't', 'injected_facts': []}]
    one_token = [{'role': 'assistant', 'text': 'we discussed polling briefly'}]
    out = io.compute_outcomes(rows, one_token, '')
    assert out[0]['engaged_in_assistant'] is False
    two_tokens = [{'role': 'assistant', 'text': 'polling the ecs service now'}]
    out = io.compute_outcomes(rows, two_tokens, '')
    assert out[0]['engaged_in_assistant'] is True


def test_v2_rows_carry_version_and_audit_fields():
    rows = [{'session_id': 's', 'tier': 'high', 'injected': 'a-b-c', 'ts': 't'}]
    out = io.compute_outcomes(rows, [], '')
    assert out[0]['scorer_version'] == 2
    assert 'engaged_matched' in out[0] and 'corrected_matched' in out[0]


def test_append_outcomes_writes_jsonl_with_stamp(tmp_path):
    path = tmp_path / 'injection_outcomes.jsonl'
    io.append_outcomes([{'session_id': 'S1', 'injected': 'a-b'}], path=path)
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0]['injected'] == 'a-b'
    assert 'scored_at' in lines[0]


def test_append_outcomes_noop_on_empty(tmp_path):
    path = tmp_path / 'injection_outcomes.jsonl'
    io.append_outcomes([], path=path)
    assert not path.exists()


def _write_state(tmp_path, note):
    state_path = tmp_path / 'harvested_sessions.json'
    state_path.write_text(json.dumps({'sessions': {'S1': {'note': note}}}))
    return state_path


def test_correction_text_for_session_reads_recorded_path(tmp_path):
    (tmp_path / 'raw' / 'inbox').mkdir(parents=True)
    note_rel = 'raw/inbox/correction-20260623-230647-acme-portal-api.md'
    (tmp_path / note_rel).write_text('be more careful about X')
    state_path = _write_state(tmp_path, note_rel)
    assert io.correction_text_for_session(
        'S1', state_path=state_path, base_dir=tmp_path) == 'be more careful about X'


def test_correction_text_for_session_falls_back_to_moved_note(tmp_path):
    # Recorded note path (raw/inbox/...) no longer exists: sync-inbox already
    # moved it into raw/web/ under kb.py's sync_inbox naming
    # (inbox-<sync-ts>-<slugify(stem, 36)>.md) — mirrors the real files seen
    # in raw/web/ for 5 of 6 currently-harvested sessions.
    (tmp_path / 'raw' / 'web').mkdir(parents=True)
    note_rel = 'raw/inbox/correction-20260623-230647-acme-portal-api.md'
    moved = tmp_path / 'raw' / 'web' / 'inbox-20260624-220001-correction-20260623-230647-acme-port.md'
    moved.write_text('be more careful about X')
    state_path = _write_state(tmp_path, note_rel)
    assert io.correction_text_for_session(
        'S1', state_path=state_path, base_dir=tmp_path) == 'be more careful about X'


def test_correction_text_for_session_missing_everywhere_returns_empty(tmp_path):
    note_rel = 'raw/inbox/correction-20260623-230647-acme-portal-api.md'
    state_path = _write_state(tmp_path, note_rel)
    # neither raw/inbox/<note> nor any raw/web/inbox-*-<slug>.md exists
    assert io.correction_text_for_session(
        'S1', state_path=state_path, base_dir=tmp_path) == ''
