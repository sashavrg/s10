import tune_gate as tg


CUR = {'thresholds': {'high': 0.73, 'moderate': 0.40},
       'high_ineligible': ['acme'], 'outcome_demoted': [],
       'project_catch_all_tokens': [], 'auto_tune': {}}


def _prop(**over):
    p = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
         for k, v in CUR.items()}
    for k, v in over.items():
        p[k] = v
    return p


def test_diff_tuning_reports_a_demotion_add_as_tightening():
    changes = tg.diff_tuning(CUR, _prop(outcome_demoted=['dns-blocking']))
    assert changes == [{'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True}]


def test_diff_tuning_reports_blocklist_add_and_threshold_raise():
    prop = _prop(high_ineligible=['acme', 'monitoring-setup'],
                 thresholds={'high': 0.74, 'moderate': 0.40})
    changes = tg.diff_tuning(CUR, prop)
    assert {'kind': 'block', 'slug': 'monitoring-setup', 'tighten': True} in changes
    assert {'kind': 'threshold', 'from': 0.73, 'to': 0.74, 'tighten': True} in changes
    assert len(changes) == 2


def test_diff_tuning_flags_loosening():
    prop = _prop(high_ineligible=[], thresholds={'high': 0.70, 'moderate': 0.40})
    changes = tg.diff_tuning(CUR, prop)
    assert {'kind': 'unblock', 'slug': 'acme', 'tighten': False} in changes
    assert {'kind': 'threshold', 'from': 0.73, 'to': 0.70, 'tighten': False} in changes


def test_diff_tuning_identical_is_empty():
    assert tg.diff_tuning(CUR, _prop()) == []


# --- evaluate_cases -----------------------------------------------------------

CASES = [
    {'id': 'pos-a', 'kind': 'paraphrase', 'project': 'p', 'prompt': 'stale widget', 'expect': ['widget-cache']},
    {'id': 'pos-b', 'kind': 'direct', 'project': 'p', 'prompt': 'dns again', 'expect': ['dns-blocking']},
    {'id': 'neg-real-commit', 'kind': 'negative', 'project': 'p', 'prompt': 'commit it', 'expect': []},
    {'id': 'neg-pasta', 'kind': 'negative', 'project': 'p', 'prompt': 'pasta recipe', 'expect': []},
]


def _retrieve_factory(table):
    """table: {(prompt, demoted-tuple): (top_slug, top_tier)} → fake mi.retrieve."""
    def retrieve(query, project=None, tuning=None, **_):
        key = (query, tuple(sorted(tuning.get('outcome_demoted') or [])))
        slug, tier = table.get(key, (None, 'none'))
        matches = [{'slug': slug, 'tier': tier, 'score': 1.0}] if slug else []
        return {'top_tier': tier, 'matches': matches}
    return retrieve


def test_evaluate_cases_counts_false_high_and_high_hits_per_arm():
    cur, prop = CUR, _prop(outcome_demoted=['dns-blocking'])
    table = {
        ('stale widget', ()): ('widget-cache', 'high'),
        ('stale widget', ('dns-blocking',)): ('widget-cache', 'high'),
        ('dns again', ()): ('dns-blocking', 'high'),
        ('dns again', ('dns-blocking',)): ('dns-blocking', 'moderate'),   # demotion silences it
        ('commit it', ()): ('dns-blocking', 'high'),                       # a false HIGH today
        ('commit it', ('dns-blocking',)): ('dns-blocking', 'moderate'),    # fixed by the demotion
    }
    res = tg.evaluate_cases(CASES, cur, prop, retrieve=_retrieve_factory(table))
    assert res['n_negatives'] == 2 and res['n_positives'] == 2
    assert res['false_high'] == {'current': 1, 'proposed': 0}
    assert res['false_high_ids'] == {'current': ['neg-real-commit'], 'proposed': []}
    assert res['high_hits'] == {'current': ['pos-a', 'pos-b'], 'proposed': ['pos-a']}
    assert res['lost'] == ['pos-b'] and res['gained'] == []


def test_evaluate_cases_high_hit_requires_expected_slug_at_top():
    # HIGH on the wrong slug is not a hit; the expected slug must be matches[0] at HIGH.
    cases = [CASES[0]]
    table = {('stale widget', ()): ('other-topic', 'high'),
             ('stale widget', ('x',)): ('widget-cache', 'high')}
    res = tg.evaluate_cases(cases, CUR, _prop(outcome_demoted=['x']), retrieve=_retrieve_factory(table))
    assert res['high_hits'] == {'current': [], 'proposed': ['pos-a']}
    assert res['gained'] == ['pos-a'] and res['lost'] == []


# --- evidence floor -------------------------------------------------------------

AUTO = {'enabled': True, 'window_days': 14, 'min_samples': 3, 'cross_project_min': 3,
        'threshold_step': 0.01, 'threshold_ceiling': 0.85, 'false_high_rate_trigger': 0.30}
TUN = {**CUR, 'auto_tune': AUTO}


def _rec(tier, slug, project, head):
    return {'tier': tier, 'injected': slug, 'project': project, 'prompt_head': head,
            'ts': '2026-08-30T10:00:00'}


def _out(slug, engaged, corrected=False):
    return {'tier': 'high', 'injected': slug, 'engaged_in_assistant': engaged,
            'topic_corrected': corrected, 'ts': '2026-08-30T10:00:00'}


def test_evidence_floor_met_for_demotion_backed_by_real_outcomes():
    outcomes = [_out('dns-blocking', engaged=False)] * 3
    ev = tg.evidence_floor([{'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True}],
                           TUN, records=[], outcomes=outcomes)
    assert ev['met'] is True
    assert ev['per_change'][0]['met'] is True
    assert 'dns-blocking' in ev['per_change'][0]['detail']


def test_evidence_floor_unmet_for_demotion_without_real_evidence():
    ev = tg.evidence_floor([{'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True}],
                           TUN, records=[], outcomes=[_out('dns-blocking', engaged=False)] * 2)
    assert ev['met'] is False and ev['per_change'][0]['met'] is False


def test_evidence_floor_ignores_synthetic_only_signal_for_threshold_raise():
    # 10 synthetic HIGH fires → the auditor alone would raise the threshold; the
    # gate feeds decide() real traffic only, so the raise is unsupported.
    records = [_rec('high', 'some-topic', 'p', '<task-notification>...')] * 10
    ev = tg.evidence_floor([{'kind': 'threshold', 'from': 0.73, 'to': 0.74, 'tighten': True}],
                           TUN, records=records, outcomes=[])
    assert ev['met'] is False


def test_evidence_floor_met_for_threshold_raise_on_real_outcome_rate():
    outcomes = [_out('t1', engaged=False), _out('t2', engaged=False), _out('t3', engaged=True)]
    ev = tg.evidence_floor([{'kind': 'threshold', 'from': 0.73, 'to': 0.74, 'tighten': True}],
                           TUN, records=[], outcomes=outcomes)
    assert ev['met'] is True


def test_evidence_floor_block_needs_real_cross_project_promiscuity():
    real = [_rec('high', 'foo', p, 'please fix foo') for p in ('a', 'b', 'c')]
    synth = [_rec('high', 'bar', p, '<system-reminder>') for p in ('a', 'b', 'c')]
    ev = tg.evidence_floor([{'kind': 'block', 'slug': 'foo', 'tighten': True},
                            {'kind': 'block', 'slug': 'bar', 'tighten': True}],
                           TUN, records=real + synth, outcomes=[])
    assert [c['met'] for c in ev['per_change']] == [True, False]
    assert ev['met'] is False


# --- verdict + brief ------------------------------------------------------------

def _eval(fh_cur=0, fh_prop=0, lost=(), gained=()):
    return {'n_positives': 16, 'n_negatives': 25,
            'false_high': {'current': fh_cur, 'proposed': fh_prop},
            'false_high_ids': {'current': [], 'proposed': []},
            'high_hits': {'current': [], 'proposed': []},
            'lost': list(lost), 'gained': list(gained)}


DEMOTE = [{'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True}]
EV_OK = {'met': True, 'per_change': [{**DEMOTE[0], 'met': True, 'detail': 'ok'}]}
EV_NO = {'met': False, 'per_change': [{**DEMOTE[0], 'met': False, 'detail': 'thin'}]}


def test_verdict_merge_when_every_bar_passes():
    v = tg.verdict(DEMOTE, _eval(), EV_OK)
    assert v == {'verdict': 'merge', 'reasons': []}


def test_verdict_hold_when_false_high_increases():
    v = tg.verdict(DEMOTE, _eval(fh_cur=0, fh_prop=1), EV_OK)
    assert v['verdict'] == 'hold' and any('false_high' in r for r in v['reasons'])


def test_verdict_hold_on_any_lost_high_hit():
    v = tg.verdict(DEMOTE, _eval(lost=['pos-b']), EV_OK)
    assert v['verdict'] == 'hold' and any('pos-b' in r for r in v['reasons'])


def test_verdict_hold_when_evidence_floor_unmet():
    v = tg.verdict(DEMOTE, _eval(), EV_NO)
    assert v['verdict'] == 'hold' and any('evidence' in r for r in v['reasons'])


def test_verdict_never_merges_a_loosening_even_if_bars_pass():
    loosen = [{'kind': 'unblock', 'slug': 'acme', 'tighten': False}]
    ev = {'met': True, 'per_change': [{**loosen[0], 'met': True, 'detail': ''}]}
    v = tg.verdict(loosen, _eval(), ev)
    assert v['verdict'] == 'hold' and any('loosening' in r for r in v['reasons'])


def test_verdict_hold_on_empty_proposal():
    v = tg.verdict([], _eval(), {'met': True, 'per_change': []})
    assert v['verdict'] == 'hold'


def test_brief_is_one_line_with_the_numbers():
    result = {'verdict': 'merge', 'reasons': [], 'branch': 'auto-tune/2026-08-31',
              'changes': DEMOTE, 'evaluation': _eval(fh_cur=1, fh_prop=0), 'evidence': EV_OK}
    line = tg.brief(result)
    assert '\n' not in line
    assert line.startswith('merge')
    for frag in ('fh 1→0/25', 'lost 0/16', 'evidence ok', 'demote dns-blocking', 'auto-tune/2026-08-31'):
        assert frag in line, frag


def test_brief_names_hold_reasons():
    result = {'verdict': 'hold', 'reasons': ['lost HIGH hit(s): pos-b'], 'branch': None,
              'changes': DEMOTE, 'evaluation': _eval(lost=['pos-b']), 'evidence': EV_OK}
    line = tg.brief(result)
    assert line.startswith('hold') and 'pos-b' in line and 'lost 1/16' in line


# --- run_gate (orchestration) ---------------------------------------------------

import datetime as dt
import json
import os

SEED = ("thresholds:\n  high: 0.73\n  moderate: 0.4\nhigh_ineligible:\n  - acme\n"
        "outcome_demoted:\nproject_catch_all_tokens:\n  - acme\n"
        "auto_tune:\n  enabled: true\n  window_days: 14\n  min_samples: 3\n  cross_project_min: 3\n"
        "  threshold_step: 0.01\n  threshold_ceiling: 0.85\n  false_high_rate_trigger: 0.3\n")
PROPOSED = SEED.replace("outcome_demoted:\n", "outcome_demoted:\n  - dns-blocking\n")


def _setup(tmp_path, monkeypatch, table, outcomes):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text("# header comment\n" + '\n'.join(json.dumps(c) for c in CASES) + '\n')
    mem = tmp_path / 'mem.jsonl'; mem.write_text('')
    out = tmp_path / 'out.jsonl'; out.write_text('\n'.join(json.dumps(o) for o in outcomes) + '\n')
    verdicts = tmp_path / 'tune_verdicts.jsonl'
    monkeypatch.setattr(tg, 'CASES_PATH', cases)
    monkeypatch.setattr(tg, 'LOG_PATH', mem)
    monkeypatch.setattr(tg, 'OUTCOME_PATH', out)
    monkeypatch.setattr(tg, 'VERDICTS_PATH', verdicts)
    seen_modes = []
    fake = _retrieve_factory(table)
    def retrieve(query, project=None, tuning=None, **kw):
        seen_modes.append((kw['mode'], os.environ.get('KB_RETRIEVAL')))
        return fake(query, project=project, tuning=tuning)
    monkeypatch.setattr(tg.mi, 'retrieve', retrieve)
    return verdicts, seen_modes


CLEAN_TABLE = {
    ('stale widget', ()): ('widget-cache', 'high'),
    ('stale widget', ('dns-blocking',)): ('widget-cache', 'high'),
    ('commit it', ()): ('dns-blocking', 'high'),
    ('commit it', ('dns-blocking',)): ('dns-blocking', 'moderate'),
}


def test_run_gate_merge_verdict_is_recorded_with_branch_and_numbers(tmp_path, monkeypatch):
    verdicts, modes = _setup(tmp_path, monkeypatch, CLEAN_TABLE,
                             outcomes=[_out('dns-blocking', engaged=False)] * 3)
    monkeypatch.setenv('KB_RETRIEVAL', 'rerank')
    res = tg.run_gate(SEED, PROPOSED, branch='auto-tune/2026-08-31',
                      now=dt.datetime(2026, 8, 31, 22))
    assert res['verdict'] == 'merge', res['reasons']
    assert res['changes'] == [{'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True}]
    assert res['evaluation']['false_high'] == {'current': 1, 'proposed': 0}
    assert set(modes) == {('lexical', 'rerank')} and os.environ['KB_RETRIEVAL'] == 'rerank'
    rows = [json.loads(l) for l in verdicts.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['branch'] == 'auto-tune/2026-08-31' and rows[0]['verdict'] == 'merge'
    assert rows[0]['as_of'] == '2026-08-31' and rows[0]['ts']
    assert rows[0]['evidence']['met'] is True


def test_run_gate_record_false_writes_nothing(tmp_path, monkeypatch):
    verdicts, _ = _setup(tmp_path, monkeypatch, CLEAN_TABLE, outcomes=[])
    res = tg.run_gate(SEED, PROPOSED, branch=None, now=dt.datetime(2026, 8, 31), record=False)
    assert res['verdict'] == 'hold'            # no real evidence in the window
    assert not verdicts.exists()


def test_run_gate_windows_evidence_by_as_of(tmp_path, monkeypatch):
    # Evidence dated 08-30 is inside a 14-day window ending 08-31, outside one ending 09-30.
    verdicts, _ = _setup(tmp_path, monkeypatch, CLEAN_TABLE,
                         outcomes=[_out('dns-blocking', engaged=False)] * 3)
    fresh = tg.run_gate(SEED, PROPOSED, branch=None, now=dt.datetime(2026, 8, 31), record=False)
    stale = tg.run_gate(SEED, PROPOSED, branch=None, now=dt.datetime(2026, 9, 30), record=False)
    assert fresh['verdict'] == 'merge' and stale['verdict'] == 'hold'


# --- nightly wiring: audit_hooks.run() carries the shadow verdict -----------------

import audit_hooks as a


def _auditor_setup(tmp_path, monkeypatch):
    # Real-traffic evidence for exactly the live proposal: dns-blocking HIGH 3x, 0 useful.
    log = tmp_path / 'mem.jsonl'; log.write_text('')
    out = tmp_path / 'out.jsonl'
    out.write_text('\n'.join(json.dumps(_out('dns-blocking', engaged=False)) for _ in range(3)) + '\n')
    tun = tmp_path / 'memory_tuning.yaml'; tun.write_text(SEED)
    monkeypatch.setattr(a, 'LOG_PATH', log)
    monkeypatch.setattr(a, 'OUTCOME_PATH', out)
    monkeypatch.setattr(a, 'TUNING_PATH', tun)
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tun)
    return _setup(tmp_path, monkeypatch, CLEAN_TABLE, outcomes=[_out('dns-blocking', engaged=False)] * 3)


def test_auditor_dry_run_attaches_shadow_gate_verdict(tmp_path, monkeypatch):
    verdicts, _ = _auditor_setup(tmp_path, monkeypatch)
    res = a.run(now=dt.datetime(2026, 8, 31, 22), dry_run=True)
    # 3/3 real non-useful outcomes trip BOTH auditor mechanisms: the per-slug
    # demotion and the aggregate-rate threshold raise. Both ride real evidence.
    assert res['changes'] == ['outcome_demoted += dns-blocking', 'thresholds.high -> 0.74']
    assert res['gate']['verdict'] == 'merge', res['gate']['reasons']
    assert res['gate']['changes'] == [
        {'kind': 'demote', 'slug': 'dns-blocking', 'tighten': True},
        {'kind': 'threshold', 'from': 0.73, 'to': 0.74, 'tighten': True}]
    assert all(c['met'] for c in res['gate']['evidence']['per_change'])
    assert not verdicts.exists()              # dry-run records nothing


def test_auditor_records_verdict_against_the_nightly_branch_name(tmp_path, monkeypatch):
    verdicts, _ = _auditor_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(a, '_tracked_tree_dirty', lambda: True)   # stop before any git
    res = a.run(now=dt.datetime(2026, 8, 31, 22), dry_run=False)
    assert res.get('skipped') == 'dirty-tree'
    rows = [json.loads(l) for l in verdicts.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['branch'] == 'auto-tune/2026-08-31'
    assert res['gate']['verdict'] == 'merge'


def test_auditor_gate_failure_never_breaks_the_nightly(tmp_path, monkeypatch):
    _auditor_setup(tmp_path, monkeypatch)
    def boom(*_, **__): raise RuntimeError('index missing')
    monkeypatch.setattr(tg, 'run_gate', boom)
    res = a.run(now=dt.datetime(2026, 8, 31, 22), dry_run=True)
    assert res['changed'] is True
    assert res['gate']['verdict'] == 'error' and 'index missing' in res['gate']['reasons'][0]


def test_auditor_main_prints_the_gate_line(tmp_path, monkeypatch, capsys):
    _auditor_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['audit_hooks.py', '--dry-run'])
    monkeypatch.setattr(a.dt, 'datetime', _FixedNow)
    a.main()
    out = capsys.readouterr().out.strip()
    assert out.count('\n') == 0
    assert 'outcome_demoted += dns-blocking' in out
    assert '| gate: merge' in out and 'lost 0/2' in out


import sys


class _FixedNow(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 31, 22, 0, 0)


# --- CLI ------------------------------------------------------------------------

import subprocess


def test_cli_prints_brief_for_a_proposed_file(tmp_path, monkeypatch, capsys):
    verdicts, _ = _setup(tmp_path, monkeypatch, CLEAN_TABLE,
                         outcomes=[_out('dns-blocking', engaged=False)] * 3)
    cur = tmp_path / 'cur.yaml'; cur.write_text(SEED)
    prop = tmp_path / 'prop.yaml'; prop.write_text(PROPOSED)
    monkeypatch.setattr(sys, 'argv', ['tune_gate.py', '--current', str(cur), '--proposed', str(prop),
                                      '--as-of', '2026-08-31', '--no-record'])
    tg.main()
    out = capsys.readouterr().out.strip()
    assert out.startswith('merge · fh 1→0/2 · lost 0/2 · evidence ok · demote dns-blocking')
    assert not verdicts.exists()


def test_cli_json_emits_the_full_result(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, CLEAN_TABLE, outcomes=[])
    cur = tmp_path / 'cur.yaml'; cur.write_text(SEED)
    prop = tmp_path / 'prop.yaml'; prop.write_text(PROPOSED)
    monkeypatch.setattr(sys, 'argv', ['tune_gate.py', '--current', str(cur), '--proposed', str(prop),
                                      '--as-of', '2026-08-31', '--no-record', '--json'])
    tg.main()
    res = json.loads(capsys.readouterr().out)
    assert res['verdict'] == 'hold' and res['as_of'] == '2026-08-31'
    assert res['evidence']['per_change'][0]['met'] is False


def test_branch_texts_come_from_git_show(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'; (repo / 'config').mkdir(parents=True)
    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
    (repo / 'config' / 'memory_tuning.yaml').write_text(SEED)
    git('add', '.'); git('commit', '-q', '-m', 'seed')
    git('checkout', '-q', '-b', 'auto-tune/2026-08-31')
    (repo / 'config' / 'memory_tuning.yaml').write_text(PROPOSED)
    git('commit', '-q', '-am', 'proposal')
    git('checkout', '-q', 'main')
    monkeypatch.setattr(tg, 'BASE_DIR', repo)
    assert tg.branch_texts('auto-tune/2026-08-31') == (SEED, PROPOSED)


def test_branch_date_is_the_default_as_of():
    assert tg.branch_date('auto-tune/2026-08-31') == dt.datetime(2026, 8, 31, 23, 59, 59)
    assert tg.branch_date('feature/x') is None


# --- operator adjudication → (operator, system) agreement pairs -------------------

def _verdict_row(branch, as_of, verdict):
    return {'ts': '2026-09-17T18:00:00', 'as_of': as_of, 'branch': branch, 'verdict': verdict,
            'changes': DEMOTE, 'gate_version': 'stage1-v1'}


def test_record_decision_pairs_with_the_branch_date_verdict(tmp_path, monkeypatch):
    # Ruling 2026-09-18: a backlog branch's pair is keyed on the verdict AS OF THE BRANCH
    # DATE, never a later re-read — so with two rows for the same branch the 08-31 one wins.
    verdicts = tmp_path / 'v.jsonl'
    verdicts.write_text(json.dumps(_verdict_row('auto-tune/2026-08-31', '2026-09-17', 'hold')) + '\n'
                        + json.dumps(_verdict_row('auto-tune/2026-08-31', '2026-08-31', 'merge')) + '\n')
    decisions = tmp_path / 'd.jsonl'
    monkeypatch.setattr(tg, 'VERDICTS_PATH', verdicts)
    monkeypatch.setattr(tg, 'DECISIONS_PATH', decisions)
    row = tg.record_decision('auto-tune/2026-08-31', 'merge', note='pair #1')
    assert row['system'] == 'merge' and row['system_as_of'] == '2026-08-31'
    assert row['operator'] == 'merge' and row['agree'] is True
    assert row['disagreement'] is None
    saved = json.loads(decisions.read_text().splitlines()[0])
    assert saved['branch'] == 'auto-tune/2026-08-31' and saved['note'] == 'pair #1' and saved['ts']


def test_record_decision_names_the_stage2_disagreement_kind(tmp_path, monkeypatch):
    verdicts = tmp_path / 'v.jsonl'
    verdicts.write_text(json.dumps(_verdict_row('auto-tune/2026-09-01', '2026-09-01', 'merge')) + '\n')
    monkeypatch.setattr(tg, 'VERDICTS_PATH', verdicts)
    monkeypatch.setattr(tg, 'DECISIONS_PATH', tmp_path / 'd.jsonl')
    row = tg.record_decision('auto-tune/2026-09-01', 'reject')
    # system-merge / operator-reject is THE disagreement the Stage-2 gate counts.
    assert row['agree'] is False and row['disagreement'] == 'system-merge/operator-reject'


def test_record_decision_refuses_a_branch_with_no_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, 'VERDICTS_PATH', tmp_path / 'missing.jsonl')
    monkeypatch.setattr(tg, 'DECISIONS_PATH', tmp_path / 'd.jsonl')
    import pytest
    with pytest.raises(ValueError, match='no gate verdict'):
        tg.record_decision('auto-tune/2026-09-02', 'merge')


def test_agreement_status_is_the_stage2_gate_arithmetic():
    rows = [{'agree': True, 'disagreement': None}] * 3 + [
        {'agree': False, 'disagreement': 'system-hold/operator-merge'}]   # not the counted kind
    st = tg.agreement_status(rows, n_required=8)
    assert st == {'n': 4, 'n_required': 8, 'counted_disagreements': 0, 'gate_clear': False}
    rows.append({'agree': False, 'disagreement': 'system-merge/operator-reject'})
    st = tg.agreement_status(rows + [{'agree': True, 'disagreement': None}] * 4, n_required=8)
    assert st['n'] == 9 and st['counted_disagreements'] == 1 and st['gate_clear'] is False


def test_cli_adjudicate_records_and_prints_the_pair(tmp_path, monkeypatch, capsys):
    verdicts = tmp_path / 'v.jsonl'
    verdicts.write_text(json.dumps(_verdict_row('auto-tune/2026-08-31', '2026-08-31', 'merge')) + '\n')
    monkeypatch.setattr(tg, 'VERDICTS_PATH', verdicts)
    monkeypatch.setattr(tg, 'DECISIONS_PATH', tmp_path / 'd.jsonl')
    monkeypatch.setattr(sys, 'argv', ['tune_gate.py', 'adjudicate', '--branch', 'auto-tune/2026-08-31',
                                      '--decision', 'merge', '--note', 'pair #1'])
    tg.main()
    out = capsys.readouterr().out
    assert 'pair recorded' in out and 'agree' in out and '1/8' in out


# --- held-out discipline: the gate never tunes against the held_out split ---------

def test_run_gate_excludes_held_out_cases(tmp_path, monkeypatch):
    verdicts, _ = _setup(tmp_path, monkeypatch, CLEAN_TABLE, outcomes=[])
    cases = [dict(c, split='train') for c in CASES] + [
        {'id': 'ho-pos', 'kind': 'paraphrase', 'project': 'p', 'prompt': 'stale widget',
         'expect': ['widget-cache'], 'split': 'held_out'},
        {'id': 'ho-neg', 'kind': 'negative', 'project': 'p', 'prompt': 'commit it',
         'expect': [], 'split': 'held_out'}]
    tg.CASES_PATH.write_text('\n'.join(json.dumps(c) for c in cases) + '\n')
    res = tg.run_gate(SEED, PROPOSED, branch=None, now=dt.datetime(2026, 9, 20), record=False)
    ev = res['evaluation']
    assert ev['n_positives'] == 2 and ev['n_negatives'] == 2          # held_out not counted
    assert 'ho-pos' not in ev['high_hits']['current'] and 'ho-neg' not in ev['false_high_ids']['current']
    assert ev['excluded_held_out'] == 2
    assert res['gate_version'] == 'stage1-v2'
