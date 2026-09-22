"""Tests for the production scorecard (front #1c) — the Perplexity-derived signals,
computed from logs/injection_outcomes.jsonl.

v1 computes what the EXISTING logs support:
  * A — correctness-on-seen: engaged-without-correction rate on repeat-topic injects;
  * D — the compounding curve: engagement/useful rate bucketed by prior-session-depth
        (= distinct prior sessions that touched the same topic) — the operational
        signature of open-endedness.
B (recall) awaits the recall_miss signal (front #1b); C (tokens/task) awaits token
logging we don't have yet — both are reported as unavailable, never faked.

prior_session_depth is derived (no new logging): walk outcomes in ts order, count the
distinct prior sessions a slug appeared in. Verdict reuses topic_records.outcome_verdict
so useful/harmful stays canonical across topic_records, audit_hooks, and the scorecard.
"""
import scorecard as sc


def _out(slug, *, engaged, corrected, session, ts, project='proj', tier='high'):
    return {
        'session_id': session, 'project': project, 'ts': ts, 'tier': tier,
        'injected': slug, 'score': 0.8, 'engaged_in_assistant': engaged,
        'topic_corrected': corrected, 'n_user_turns': 5, 'scored_at': ts,
    }


# ---- depth enrichment (the compounding-curve raw signal) ----

def test_enrich_counts_distinct_prior_sessions_per_slug():
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='c', ts='2026-06-03T10:00:00'),
    ]
    enriched = sc.enrich_depth(outcomes)
    assert [r['prior_session_depth'] for r in enriched] == [0, 1, 2]
    assert [r['repeat_topic'] for r in enriched] == [False, True, True]


def test_enrich_depth_dedupes_within_a_session():
    # Two injects of the same slug in ONE session must not inflate depth.
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:05:00'),
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
    ]
    enriched = sc.enrich_depth(outcomes)
    assert [r['prior_session_depth'] for r in enriched] == [0, 0, 1]


def test_enrich_depth_is_per_slug_independent():
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s2', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
    ]
    enriched = sc.enrich_depth(outcomes)
    assert [r['prior_session_depth'] for r in enriched] == [0, 0]


def test_enrich_depth_orders_by_ts_regardless_of_input_order():
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='c', ts='2026-06-03T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
    ]
    enriched = sc.enrich_depth(outcomes)
    by_session = {r['session_id']: r['prior_session_depth'] for r in enriched}
    assert by_session == {'a': 0, 'b': 1, 'c': 2}


# ---- A: correctness-on-seen (rate over repeat-topic injects) ----

def test_correctness_on_seen_rate_over_repeat_injects():
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),  # depth 0, not counted
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),  # depth 1, useful
        _out('s1', engaged=True, corrected=True, session='c', ts='2026-06-03T10:00:00'),   # depth 2, harmful
    ]
    a = sc.correctness_on_seen(outcomes)
    assert a['repeat_injects'] == 2
    assert a['useful'] == 1
    assert a['rate'] == 0.5


def test_correctness_on_seen_rate_is_none_without_repeats():
    outcomes = [_out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00')]
    a = sc.correctness_on_seen(outcomes)
    assert a['repeat_injects'] == 0
    assert a['rate'] is None      # honest: no signal, not 0.0


# ---- D: the compounding curve (bucketed by prior-session-depth) ----

def test_compounding_curve_buckets_and_rates():
    outcomes = []
    # depth 0: one useful  -> bucket "0"
    outcomes.append(_out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'))
    # depth 1,2,3: all on s1 -> bucket "1-3"; make 2 useful / 1 harmful among them
    outcomes.append(_out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'))
    outcomes.append(_out('s1', engaged=True, corrected=False, session='c', ts='2026-06-03T10:00:00'))
    outcomes.append(_out('s1', engaged=False, corrected=False, session='d', ts='2026-06-04T10:00:00'))
    curve = {b['bucket']: b for b in sc.compounding_curve(outcomes)}
    assert curve['0']['n'] == 1
    assert curve['0']['useful_rate'] == 1.0
    assert curve['1-3']['n'] == 3
    assert curve['1-3']['useful_rate'] == round(2 / 3, 4)
    assert curve['1-3']['engaged_rate'] == round(2 / 3, 4)


def test_compounding_curve_omits_empty_buckets():
    outcomes = [_out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00')]
    buckets = {b['bucket'] for b in sc.compounding_curve(outcomes)}
    assert buckets == {'0'}        # only the populated bucket is reported


# ---- the assembled scorecard ----

def test_scorecard_reports_A_and_D_and_marks_B_C_unavailable():
    # _out() defaults tier='high', so both rows land in the HIGH tier population.
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
    ]
    card = sc.scorecard(outcomes)
    assert card['n_outcomes'] == 2
    high = card['by_tier']['high']
    assert high['correctness_on_seen']['rate'] == 1.0     # the one repeat inject was useful
    assert any(b['bucket'] == '0' for b in high['compounding_curve'])
    assert card['recall']['available'] is False           # awaits recall_miss.stats() (Task 6)
    assert card['cost']['available'] is False             # awaits token logging


def test_scorecard_computes_recall_when_stats_provided():
    # recall is now symmetric/correction-population-derived (Task 6), passed in whole
    # rather than reconstructed from outcomes + a miss count.
    outcomes = [
        _out('s1', engaged=True, corrected=False, session='a', ts='2026-06-01T10:00:00'),
        _out('s1', engaged=True, corrected=False, session='b', ts='2026-06-02T10:00:00'),
    ]
    card = sc.scorecard(outcomes, recall_stats={'hits': 2, 'misses': 1})
    assert card['recall']['available'] is True
    assert card['recall']['hits'] == 2
    assert card['recall']['misses'] == 1
    assert card['recall']['recall'] == round(2 / 3, 4)    # hits / (hits + misses)
    assert card['recall']['unattributable'] == 0          # defaults when not supplied
    assert card['recall']['ci'] is not None


# ---- v2: Wilson CIs, (slug, project) depth keying, tier stratification ----

def test_wilson_known_value():
    lo, hi = sc.wilson(8, 10)
    assert 0.49 < lo < 0.51
    assert 0.94 < hi < 0.95
    assert sc.wilson(0, 0) is None


def test_enrich_depth_keys_on_slug_and_project():
    rows = [
        {'injected': 's', 'project': 'p1', 'session_id': 'a', 'ts': '1'},
        {'injected': 's', 'project': 'p2', 'session_id': 'b', 'ts': '2'},
        {'injected': 's', 'project': 'p1', 'session_id': 'c', 'ts': '3'},
    ]
    out = {(r['project'], r['session_id']): r for r in sc.enrich_depth(rows)}
    assert out[('p2', 'b')]['prior_session_depth'] == 0   # p2 has no prior, despite p1's
    assert out[('p1', 'c')]['prior_session_depth'] == 1


def test_scorecard_stratifies_by_tier_and_reports_lift():
    high = [{'injected': 'a', 'project': 'p', 'session_id': f'h{i}', 'ts': str(i),
             'tier': 'high', 'engaged_in_assistant': True, 'topic_corrected': False}
            for i in range(4)]
    mod = [{'injected': 'b', 'project': 'p', 'session_id': f'm{i}', 'ts': str(i),
            'tier': 'moderate', 'engaged_in_assistant': i < 1, 'topic_corrected': False}
           for i in range(4)]
    card = sc.scorecard(high + mod)
    assert card['by_tier']['high']['n'] == 4
    assert card['by_tier']['moderate']['n'] == 4
    lift = card['injection_lift']
    assert abs(lift['lift'] - 0.75) < 1e-6          # 100% useful vs 25%
    assert lift['high_ci'] is not None and lift['moderate_ci'] is not None


def test_compounding_buckets_carry_ci():
    rows = [{'injected': 'a', 'project': 'p', 'session_id': f's{i}', 'ts': str(i),
             'tier': 'high', 'engaged_in_assistant': True, 'topic_corrected': False}
            for i in range(3)]
    card = sc.scorecard(rows)
    b0 = card['by_tier']['high']['compounding_curve'][0]
    assert b0['useful_ci'] is not None and len(b0['useful_ci']) == 2


# ---- version-pair keying (judge-aware) ----

def test_current_rows_filters_on_version_pair():
    rows = [
        {'scorer_version': 3, 'judge_version': 'ejX', 'injected': 'a'},
        {'scorer_version': 3, 'judge_version': 'ej-OLD', 'injected': 'b'},
        {'scorer_version': 2, 'injected': 'c'},
        {'injected': 'd'},
    ]
    out = sc.current_rows(rows, 'ejX')
    assert [r['injected'] for r in out] == ['a']


# --------------------------------------------- reopened-window read point (Task 10)
# The pre-registered exit is "depth>=1 HIGH n >= 40". The tripwire must count that,
# not something adjacent — so the counter lives here, next to the curve it gates.

def _o(sid, slug, ts, tier='high', project='p'):
    return {'session_id': sid, 'injected': slug, 'ts': ts, 'tier': tier,
            'project': project}


def test_window_progress_counts_only_in_window_high_repeats():
    rows = [
        _o('s1', 'a', '2026-08-01T10:00:00'),                    # depth 0 in-window
        _o('s2', 'a', '2026-08-02T10:00:00'),                    # depth 1 in-window ✓
        _o('s3', 'a', '2026-08-03T10:00:00', tier='moderate'),   # repeat but MODERATE
        _o('s4', 'b', '2026-07-01T10:00:00'),                    # pre-window
        _o('s5', 'b', '2026-07-02T10:00:00'),                    # pre-window repeat
    ]
    p = sc.window_progress(rows, '2026-08-01')
    assert p['n'] == 1
    assert p['target'] == sc.WINDOW_TARGET
    assert p['ready'] is False


def test_window_progress_counts_depth_from_full_history_not_just_the_window():
    """A topic first seen before the window is still a REPEAT when it recurs inside
    it — depth is a property of the topic's history, not of the window."""
    rows = [
        _o('s1', 'a', '2026-07-01T10:00:00'),   # pre-window first touch
        _o('s2', 'a', '2026-08-05T10:00:00'),   # in-window recurrence -> depth 1 ✓
    ]
    assert sc.window_progress(rows, '2026-08-01')['n'] == 1


def test_window_progress_ready_at_the_target():
    rows = []
    for i in range(sc.WINDOW_TARGET + 1):          # +1 first-touch row per slug
        rows.append(_o(f'first{i}', f'slug{i}', '2026-08-01T09:00:00'))
        rows.append(_o(f'again{i}', f'slug{i}', '2026-08-02T09:00:00'))
    p = sc.window_progress(rows, '2026-08-01')
    assert p['n'] == sc.WINDOW_TARGET + 1
    assert p['ready'] is True


def test_window_progress_without_a_window_start_is_not_ready():
    rows = [_o('s1', 'a', '2026-08-01T10:00:00'), _o('s2', 'a', '2026-08-02T10:00:00')]
    p = sc.window_progress(rows, None)
    assert p['n'] == 0 and p['ready'] is False


def test_window_open_date_prefers_env_then_marker_then_none(tmp_path, monkeypatch):
    marker = tmp_path / 'window_open'
    monkeypatch.setattr(sc, 'WINDOW_MARKER', marker)
    monkeypatch.delenv('KB_WINDOW_OPEN', raising=False)
    assert sc.window_open_date() is None          # no window declared yet
    marker.write_text('2026-08-01\n')
    assert sc.window_open_date() == '2026-08-01'  # marker (Task 10 Step 4)
    monkeypatch.setenv('KB_WINDOW_OPEN', '2026-09-09')
    assert sc.window_open_date() == '2026-09-09'  # env overrides for dry runs


import json
import sys

import pytest


def _judged(row, version='ej-test'):
    return {**row, 'scorer_version': 3, 'judge_version': version,
            'engaged_in_assistant': True, 'topic_corrected': False}


def test_depth_survives_window_tier_and_judge_selection():
    history = [_o('old', 'a', '2026-07-01', tier='moderate'),
               _o('new', 'a', '2026-08-05'),
               _o('other', 'a', '2026-08-06', project='different'),
               _o('later', 'a', '2026-09-01')]
    judged = [_judged(r) for r in history]
    judged[0]['judge_version'] = 'old-judge'
    card = sc.scorecard(judged, history=history, window_start='2026-08-01',
                        window_end='2026-09-01', judge_version='ej-test')
    high = card['by_tier']['high']
    assert high['n'] == 2
    assert high['correctness_on_seen']['repeat_injects'] == 1
    assert [(b['bucket'], b['n']) for b in high['compounding_curve']] == [('0', 1), ('1-3', 1)]
    assert card['coverage']['high_repeat_eligible'] == sc.window_progress(
        history, '2026-08-01', '2026-09-01')['n'] == 1


def test_enriched_helper_input_does_not_lose_pre_window_history():
    rows = sc.enrich_depth([_judged(_o('a', 'x', '2026-07-01')),
                            _judged(_o('b', 'x', '2026-08-01'))])
    selected = rows[1:]
    assert sc.compounding_curve(selected)[0]['bucket'] == '1-3'
    assert sc.correctness_on_seen(selected)['repeat_injects'] == 1
    assert sc.scorecard(selected)['by_tier']['high']['compounding_curve'][0]['bucket'] == '1-3'
    with pytest.raises(ValueError, match='Mixed raw'):
        sc.compounding_curve([rows[0], _o('c', 'x', '2026-09-01')])


def test_duplicate_log_writes_do_not_inflate_accrual_or_useful_counts():
    first = _judged(_o('a', 'x', '2026-07-01'))
    second = _judged(_o('b', 'x', '2026-08-01'))
    rows = [first, second, first, second]
    assert sc.window_progress(rows, '2026-08-01')['n'] == 1
    card = sc.scorecard(rows, window_start='2026-08-01')
    assert card['n_outcomes'] == card['by_tier']['high']['useful'] == 1
    assert card['coverage']['duplicate_analysis_rows'] == 2
    assert card['coverage']['duplicate_history_rows'] == 2


def test_reconstructed_analysis_event_cannot_change_tripwire_depth():
    # The real 37/38 discrepancy class: analysis has a prior exposure absent
    # from the live log. Re-judging cannot retroactively move the exit counter.
    live = [_o('later', 'hermes', '2026-08-26')]
    analysis = [_judged(_o('earlier', 'hermes', '2026-08-24')), _judged(live[0])]
    card = sc.scorecard(analysis, history=live, window_start='2026-08-05')
    assert card['coverage']['analysis_only'] == 1
    assert card['coverage']['high_repeat_eligible'] == 0
    assert card['by_tier']['high']['correctness_on_seen']['repeat_injects'] == 0
    assert card['by_tier']['high']['compounding_curve'][0]['bucket'] == '0'


def test_missing_or_mismatched_judgments_are_visible_coverage_gaps():
    live = [_o('first', 'x', '2026-07-01'), _o('again', 'x', '2026-08-01'),
            _o('third', 'x', '2026-08-02')]
    analysis = [_judged(live[1], 'old-judge'), _judged({**live[2], 'project': 'wrong'})]
    card = sc.scorecard(analysis, history=live, window_start='2026-08-01', judge_version='ej-test')
    assert card['n_outcomes'] == 0
    assert card['coverage']['high_repeat_eligible'] == 2
    assert card['coverage']['high_repeat_unjudged'] == 2
    assert card['coverage']['metadata_mismatch'] == 1
    assert card['repeat_injection_lift']['lift'] is None


def test_repeat_lift_is_distinct_from_all_depth_lift():
    rows = [_judged(_o('h0', 'h', '2026-07-01')),
            {**_judged(_o('h1', 'h', '2026-08-01')), 'engaged_in_assistant': False},
            {**_judged(_o('m0', 'm', '2026-07-01', tier='moderate')), 'engaged_in_assistant': False},
            _judged(_o('m1', 'm', '2026-08-01', tier='moderate'))]
    card = sc.scorecard(rows)
    assert card['injection_lift']['lift'] == 0
    assert card['repeat_injection_lift']['lift'] == -1
    assert card['repeat_injection_lift']['high_ci'] is not None


@pytest.fixture
def cli_logs(tmp_path, monkeypatch):
    live = tmp_path / 'live.jsonl'
    rescored = tmp_path / 'rescored.jsonl'
    rows = [_o('first', 'a', '2026-07-01T12:00:00'),
            _o('again', 'a', '2026-08-05T12:00:00')]
    live.write_text('\n'.join(json.dumps(r) for r in rows))
    # Stale analysis log, only pre-window data.
    rescored.write_text(json.dumps(_judged(rows[0])))
    monkeypatch.setattr(sc, 'OUTCOME_PATH', live)
    monkeypatch.setattr(sc, 'RESCORED_PATH', rescored)
    monkeypatch.setattr(sc, 'WINDOW_MARKER', tmp_path / 'absent-marker')
    monkeypatch.setenv('KB_WINDOW_OPEN', '2026-08-01')
    return live, rescored, rows


def test_window_count_uses_live_but_honors_explicit_default_log(cli_logs, monkeypatch, capsys):
    live, rescored, _ = cli_logs
    monkeypatch.setattr(sys, 'argv', ['scorecard', '--window-count'])
    sc.main()
    assert capsys.readouterr().out.strip() == '1'
    # Previously this explicit path was mistaken for an omitted --log.
    monkeypatch.setattr(sys, 'argv', ['scorecard', '--window-count', '--log', str(rescored)])
    sc.main()
    assert capsys.readouterr().out.strip() == '0'
    monkeypatch.setattr(sys, 'argv', ['scorecard', '--window-count', '--log', str(rescored),
                                    '--history-log', str(live)])
    sc.main()
    assert capsys.readouterr().out.strip() == '1'


def test_cli_window_pins_judge_and_reports_pending_coverage(cli_logs, monkeypatch, capsys):
    live, rescored, rows = cli_logs
    rescored.write_text('\n'.join(json.dumps(_judged(r, 'old-judge' if i else 'ej-test'))
                                  for i, r in enumerate(rows)))
    monkeypatch.setattr(sys, 'argv', ['scorecard', '--window', '2026-08-01', '--until', '2026-09-01',
                                    '--judge-version', 'ej-test', '--json'])
    sc.main()
    card = json.loads(capsys.readouterr().out)
    assert card['n_outcomes'] == 0
    assert card['coverage']['high_repeat_eligible'] == 1
    assert card['coverage']['high_repeat_unjudged'] == 1
    assert card['recall']['available'] is False
    assert 'window-scoped' in card['recall']['needs']


@pytest.mark.parametrize('bounds', [
    ['--window', 'garbage'], ['--window', '2026-02-30'],
    ['--window', '2026-08-01', '--until', '2026-07-01'],
    ['--window', '2026-08-01T00:00:00+02:00'],
])
def test_cli_rejects_invalid_window_bounds(cli_logs, monkeypatch, bounds):
    monkeypatch.setattr(sys, 'argv', ['scorecard', *bounds])
    with pytest.raises(SystemExit) as exc:
        sc.main()
    assert exc.value.code == 2
