# tests/test_rescore_outcomes.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import rescore_outcomes as ro


def test_group_sessions_filters_and_counts_sessionless():
    rows = [
        {'session_id': 's1', 'tier': 'high', 'injected': 'a'},
        {'session_id': 's1', 'tier': 'moderate', 'advertised': ['b']},
        {'session_id': None, 'tier': 'high', 'injected': 'c'},
        {'session_id': 's2', 'tier': 'none'},          # not an injection decision
    ]
    sessions, sessionless = ro.group_sessions(rows)
    assert set(sessions) == {'s1'}
    assert len(sessions['s1']) == 2
    assert sessionless == 1


def test_rescore_skips_missing_transcripts_and_scores_found_ones(tmp_path):
    t = tmp_path / 's1.jsonl'
    turns = [
        {'message': {'role': 'user', 'content': 'how do i poll the deploy?'}},
        {'message': {'role': 'assistant',
                     'content': 'poll the ecs rollout state until completed'}},
    ]
    t.write_text('\n'.join(json.dumps(x) for x in turns))
    sessions = {
        's1': [{'session_id': 's1', 'tier': 'high', 'injected': 'ecs-rollout-polling',
                'ts': '2026-07-01T10:00:00', 'injected_facts': ['poll rolloutState']}],
        's2': [{'session_id': 's2', 'tier': 'high', 'injected': 'x-y',
                'ts': '2026-07-01T11:00:00'}],
    }
    rows, stats = ro.rescore(
        sessions,
        transcript_for=lambda sid: t if sid == 's1' else None,
        correction_for=lambda sid: '')
    assert stats['sessions_scored'] == 1
    assert stats['sessions_missing_transcript'] == 1
    assert len(rows) == 1
    assert rows[0]['scorer_version'] == 2
    assert rows[0]['engaged_in_assistant'] is True
    assert rows[0]['n_user_turns'] == 1


def test_rescore_tracks_missing_session_ids():
    sessions = {'s1': [{'session_id': 's1', 'tier': 'high', 'injected': 'a', 'ts': 't1'}]}
    rows, stats = ro.rescore(
        sessions, transcript_for=lambda sid: None, correction_for=lambda sid: '')
    assert stats['sessions_missing_transcript'] == 1
    assert stats['missing_session_ids'] == {'s1'}


# ---- F3: union-merge retention (a previous rescored row survives an expired transcript) ----

def test_merge_previous_retains_transcriptless_session_row():
    previous_rows = [{'session_id': 's2', 'ts': 't0', 'injected': 'b', 'scorer_version': 2}]
    merged, carried = ro.merge_previous([], previous_rows, missing_sessions={'s2'})
    assert carried == 1
    assert merged == previous_rows


def test_merge_previous_new_row_overwrites_not_duplicates():
    old = {'session_id': 's1', 'ts': 't1', 'injected': 'a', 'engaged_in_assistant': False}
    new = {'session_id': 's1', 'ts': 't1', 'injected': 'a', 'engaged_in_assistant': True}
    # s1 also happens to be "missing" this run (irrelevant: new_rows always win for
    # keys present in both).
    merged, carried = ro.merge_previous([new], [old], missing_sessions={'s1'})
    assert carried == 0
    assert merged == [new]


def test_merge_previous_drops_row_whose_session_is_neither_new_nor_missing():
    # s3's transcript wasn't missing this run (e.g. its injection rows dropped out
    # of memory_injection.jsonl entirely) and it wasn't rescored -> not carried.
    previous_rows = [{'session_id': 's3', 'ts': 't2', 'injected': 'c'}]
    merged, carried = ro.merge_previous([], previous_rows, missing_sessions=set())
    assert carried == 0
    assert merged == []


def test_merge_previous_sorts_by_ts():
    new_rows = [{'session_id': 's1', 'ts': '2026-07-02T10:00:00', 'injected': 'a'}]
    previous_rows = [{'session_id': 's2', 'ts': '2026-07-01T09:00:00', 'injected': 'b'}]
    merged, carried = ro.merge_previous(new_rows, previous_rows, missing_sessions={'s2'})
    assert [r['session_id'] for r in merged] == ['s2', 's1']


def test_agreement_joins_on_session_ts_slug():
    v1 = [{'session_id': 's1', 'ts': 't1', 'injected': 'a',
           'engaged_in_assistant': True, 'topic_corrected': False}]
    v2 = [{'session_id': 's1', 'ts': 't1', 'injected': 'a',
           'engaged_in_assistant': False, 'topic_corrected': False},
          {'session_id': 's9', 'ts': 't9', 'injected': 'z',
           'engaged_in_assistant': True, 'topic_corrected': False}]
    rep = ro.agreement(v1, v2)
    assert rep['joined'] == 1
    assert rep['engaged_agree'] == 0
    assert rep['engaged_v1_only'] == 1      # v1 said engaged, v2 says not
    assert rep['corrected_agree'] == 1


# ---- judge pass (#1e): graft, queue, cap, never-fake-3 ----

def _v2row(sid, ts, slug, tier='high'):
    return {'session_id': sid, 'ts': ts, 'injected': slug, 'tier': tier,
            'scorer_version': 2, 'engaged_in_assistant': False, 'topic_corrected': False}


def test_graft_carries_current_version_verdicts_only():
    rows = [_v2row('s1', 't1', 'a'), _v2row('s2', 't2', 'b')]
    prev = [dict(_v2row('s1', 't1', 'a'), scorer_version=3, judge_version='ejX',
                 engaged_in_assistant=True, judge_rationale='r', judge_evidence={'n_snippets': 1}),
            dict(_v2row('s2', 't2', 'b'), scorer_version=3, judge_version='ej-OLD',
                 engaged_in_assistant=True)]
    n = ro.graft_previous_judgments(rows, prev, 'ejX')
    assert n == 1
    assert rows[0]['scorer_version'] == 3 and rows[0]['engaged_in_assistant'] is True
    assert rows[1]['scorer_version'] == 2          # old judge_version NOT grafted


def test_select_queue_skips_current_version_and_sorts_by_ts():
    rows = [dict(_v2row('s1', 't9', 'a'), scorer_version=3, judge_version='ejX'),
            _v2row('s2', 't2', 'b'), _v2row('s3', 't1', 'c')]
    q = ro.select_judge_queue(rows, 'ejX')
    assert [r['ts'] for r in q] == ['t1', 't2']


def test_run_judge_pass_caps_fails_open_and_counts():
    rows = [_v2row('s1', 't1', 'a'), _v2row('s2', 't2', 'b'), _v2row('s3', 't3', 'c')]
    verdicts = {'a': {'engaged': True, 'rationale': 'used it'}, 'b': None}
    def fake_judge(row):
        return verdicts.get(row['injected']), {'n_snippets': 2, 'nomination_empty': False}
    stats = ro.run_judge_pass(rows, [], 'ejX', max_calls=2, judge_row_fn=fake_judge)
    assert stats == {'grafted': 0, 'judged': 1, 'failed': 1, 'remaining': 1}
    assert rows[0]['scorer_version'] == 3 and rows[0]['judge_version'] == 'ejX'
    assert rows[0]['judge_evidence'] == {'n_snippets': 2, 'nomination_empty': False}
    assert rows[1]['scorer_version'] == 2          # None verdict: never faked to 3
    assert rows[2]['scorer_version'] == 2          # over the cap: untouched


def test_judge_rerun_is_zero_call_and_byte_identical():
    rows1 = [_v2row('s1', 't1', 'a')]
    def fake_judge(row):
        return {'engaged': True, 'rationale': 'r'}, {'n_snippets': 0, 'nomination_empty': True}
    ro.run_judge_pass(rows1, [], 'ejX', max_calls=10, judge_row_fn=fake_judge)
    frozen = [dict(r) for r in rows1]
    calls = []
    def counting_judge(row):
        calls.append(row)
        return {'engaged': False, 'rationale': 'flip!'}, {}
    rows2 = [_v2row('s1', 't1', 'a')]
    stats = ro.run_judge_pass(rows2, frozen, 'ejX', max_calls=10,
                              judge_row_fn=counting_judge)
    assert calls == [] and stats['grafted'] == 1
    assert rows2[0]['engaged_in_assistant'] is True    # cached verdict, never re-rolled


# ---- C2: disable knob (KB_JUDGE_MAX_CALLS=0) must not wipe the verdict cache ----

def test_run_judge_pass_grafts_even_when_max_calls_zero():
    # s1 has a cached v3 verdict at the current judge_version -> must graft.
    # s2 is fresh (no previous verdict) -> stays queued, but max_calls=0 means the
    # judge loop must break before calling judge_row_fn on it (zero judge calls).
    rows = [_v2row('s1', 't1', 'a'), _v2row('s2', 't2', 'b')]
    prev = [dict(_v2row('s1', 't1', 'a'), scorer_version=3, judge_version='ejX',
                 engaged_in_assistant=True, judge_rationale='r',
                 judge_evidence={'n_snippets': 1})]
    calls = []
    def counting_judge(row):
        calls.append(row)
        return {'engaged': False, 'rationale': 'flip!'}, {}
    stats = ro.run_judge_pass(rows, prev, 'ejX', max_calls=0, judge_row_fn=counting_judge)
    assert calls == []
    assert stats['grafted'] == 1 and stats['judged'] == 0 and stats['remaining'] == 1
    assert rows[0]['scorer_version'] == 3 and rows[0]['engaged_in_assistant'] is True
    assert rows[1]['scorer_version'] == 2          # fresh row, untouched (never faked)


# ---- max_calls_from_env: clamp negative values to 0 ----

def test_max_calls_from_env_defaults_to_0(monkeypatch):
    # pre-gate default: judge unvalidated -> writes disabled unless explicitly
    # opted in (operator decision 2026-07-02, final-review Important #3).
    monkeypatch.delenv('KB_JUDGE_MAX_CALLS', raising=False)
    assert ro._max_calls_from_env() == 0


def test_max_calls_from_env_parses_positive(monkeypatch):
    monkeypatch.setenv('KB_JUDGE_MAX_CALLS', '25')
    assert ro._max_calls_from_env() == 25


def test_max_calls_from_env_clamps_zero(monkeypatch):
    monkeypatch.setenv('KB_JUDGE_MAX_CALLS', '0')
    assert ro._max_calls_from_env() == 0


def test_max_calls_from_env_clamps_negative(monkeypatch):
    monkeypatch.setenv('KB_JUDGE_MAX_CALLS', '-1')
    assert ro._max_calls_from_env() == 0


def test_max_calls_from_env_raises_on_non_numeric(monkeypatch):
    monkeypatch.setenv('KB_JUDGE_MAX_CALLS', 'x')
    try:
        ro._max_calls_from_env()
        assert False, "should have raised ValueError"
    except ValueError:
        pass


# ---- C1: prod evidence must mirror --dev's inj_eff (moderate rows have no `injected`
#      key in the raw injection log; only `advertised`) ----

def test_production_judge_row_fn_builds_inj_eff_for_moderate_rows(tmp_path, monkeypatch):
    import engagement_judge as ej

    t = tmp_path / 's1.jsonl'
    turns = [
        {'message': {'role': 'user', 'content': 'tell me about some-slug please'}},
        {'message': {'role': 'assistant', 'content': 'discussing some-slug details now'}},
    ]
    t.write_text('\n'.join(json.dumps(x) for x in turns))

    monkeypatch.setattr(ej, 'resolve_transcript', lambda sid: t)
    monkeypatch.setattr(ej, 'load_topic_page', lambda slug: {'overview': '', 'points': []})

    captured = {}
    def capturing_judge(ev):
        captured['ev'] = ev
        return {'engaged': True, 'rationale': 'x'}
    monkeypatch.setattr(ej, 'judge_engagement', capturing_judge)

    # Raw injection-log row for a MODERATE tier: carries `advertised`, NOT `injected`
    # (that's the skew C1 fixes -- production must not pass this row as-is).
    injection_row = {'session_id': 's1', 'ts': 't1', 'tier': 'moderate',
                      'advertised': ['some-slug'], 'prompt_head': 'tell me about some-slug'}
    inj_by_key = {('s1', 't1'): injection_row}
    outcome_row = {'session_id': 's1', 'ts': 't1', 'tier': 'moderate', 'injected': 'some-slug'}

    judge_row = ro._production_judge_row_fn(inj_by_key, {})
    verdict, meta = judge_row(outcome_row)

    assert captured['ev']['topic_slug'] == 'some-slug'   # not None
    assert verdict == {'engaged': True, 'rationale': 'x'}
