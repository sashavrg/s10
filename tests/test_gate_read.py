"""Tests for scripts/gate_read.py — the ej9 gate-read runner (A1 model pin +
A3 amended verdict + A4 operations order, per the operator-signed addendum
docs/superpowers/specs/2026-07-20-ej9-gate-read-addendum.md).

All synthetic: no network, no real gate rows (blindness discipline). The judge
is always a fake generate/judge fn.
"""
import json

import pytest

import engagement_judge as ej
import gate_read as gr


def _label(i, engaged=True, identity=True):
    return {'session_id': f's{i}', 'ts': f'2026-07-{i:02d}T10:00:00',
            'injected': f'slug-{i}', 'tier': 'high',
            'identity_matches_subject': identity,
            'label_engaged': engaged, 'label_corrected': False}


def _judged(label_engaged, judged_engaged, identity=True):
    return {'label_engaged': label_engaged, 'judged_engaged': judged_engaged,
            'identity_matches_subject': identity}


# ------------------------------------------------------------------ scoring

def test_score_rows_counts_and_point_estimates():
    rows = [_judged(True, True), _judged(True, True),      # tp x2
            _judged(False, True),                          # fp
            _judged(True, False),                          # fn
            _judged(False, False)]                         # tn
    s = gr.score_rows(rows)
    assert (s['n'], s['tp'], s['fp'], s['fn'], s['tn']) == (5, 2, 1, 1, 1)
    assert s['precision'] == round(2 / 3, 4) and s['recall'] == round(2 / 3, 4)
    assert s['p_ci'] and s['r_ci']                         # Wilson, informational


def test_exclusion_read_drops_only_preflagged_rows():
    rows = [_judged(True, True), _judged(False, True, identity=False),
            _judged(True, False)]
    s = gr.exclusion_read(rows)
    assert s['n'] == 2 and s['fp'] == 0                    # the flagged fp is out


# ----------------------------------------------------------------- run_read

def test_run_read_refuses_underpowered_set():
    labels = [_label(i) for i in range(34)]
    with pytest.raises(gr.GateReadError, match='full-power'):
        gr.run_read(labels, lambda l: True, stage=1)
    with pytest.raises(gr.GateReadError, match='full-power'):
        gr.run_read([_label(i) for i in range(49)], lambda l: True, stage=2)


def test_run_read_retries_none_once_then_aborts():
    labels = [_label(i) for i in range(35)]
    calls = {}
    def judge(l):
        calls[l['session_id']] = calls.get(l['session_id'], 0) + 1
        return None if l['session_id'] == 's3' else True
    with pytest.raises(gr.GateReadError, match='unjudged'):
        gr.run_read(labels, judge, stage=1)
    assert calls['s3'] == 2                                # exactly one retry
    assert calls['s4'] == 1


def test_run_read_retry_recovers_transient_none():
    labels = [_label(i) for i in range(35)]
    seen = set()
    def judge(l):
        if l['session_id'] == 's3' and 's3' not in seen:
            seen.add('s3')
            return None
        return l['label_engaged']
    report = gr.run_read(labels, judge, stage=1)
    assert report['main']['n'] == 35


def test_run_read_report_verdict_and_exclusion():
    # 35 rows: 20 operator-engaged. Judge: 18 tp, 2 fn, 1 fp (identity-flagged),
    # 14 tn -> P 18/19=0.947, R 18/20=0.90 -> stage-1 pass.
    labels = ([_label(i, engaged=True) for i in range(20)]
              + [_label(20 + i, engaged=False) for i in range(15)])
    labels[20]['identity_matches_subject'] = False         # the fp row, pre-flagged
    def judge(l):
        i = int(l['session_id'][1:])
        if i in (0, 1):
            return False                                   # 2 fn
        if i == 20:
            return True                                    # 1 fp
        return l['label_engaged']
    report = gr.run_read(labels, judge, stage=1)
    m = report['main']
    assert (m['tp'], m['fp'], m['fn'], m['tn']) == (18, 1, 2, 14)
    assert report['verdict'] == 'pass'
    assert report['exclusion']['fp'] == 0 and report['exclusion']['n'] == 34
    assert report['stage'] == 1
    assert report['judge_version'] == ej.JUDGE_VERSION
    assert report['model'] == gr.GATE_MODEL_ID == 'claude-sonnet-5'


def test_run_read_aborts_on_degenerate_margins():
    labels = [_label(i, engaged=(i < 20)) for i in range(35)]
    with pytest.raises(gr.GateReadError, match='degenerate'):
        gr.run_read(labels, lambda l: False, stage=1)      # judge all-no: P undefined


# ------------------------------------------------------------- A1 model pin

def test_pinned_generate_returns_result_on_matching_model(monkeypatch):
    import kb
    monkeypatch.setattr(kb, 'claude_code_payload', lambda model, prompt: {
        'result': 'ENGAGED: yes\nWHY: R2', 'modelUsage': {'claude-sonnet-5': {}}})
    gr.PIN_VIOLATIONS.clear()
    assert gr.pinned_generate('p') == 'ENGAGED: yes\nWHY: R2'
    assert gr.PIN_VIOLATIONS == []


def test_pinned_generate_raises_and_records_on_model_mismatch(monkeypatch):
    import kb
    monkeypatch.setattr(kb, 'claude_code_payload', lambda model, prompt: {
        'result': 'ENGAGED: yes', 'modelUsage': {'claude-opus-4-8': {}}})
    gr.PIN_VIOLATIONS.clear()
    with pytest.raises(gr.GateReadError, match='pin'):
        gr.pinned_generate('p')
    assert len(gr.PIN_VIOLATIONS) == 1


def test_claude_code_payload_exposes_model_usage(monkeypatch):
    import types
    import kb

    def fake_run(cmd, **kwargs):
        assert '--model' in cmd and 'claude-sonnet-5' in cmd
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {'result': 'ok', 'modelUsage': {'claude-sonnet-5': {'in': 1}}}), stderr='')

    monkeypatch.setattr(kb.subprocess, 'run', fake_run)
    payload = kb.claude_code_payload('claude-sonnet-5', 'p')
    assert payload['modelUsage'] == {'claude-sonnet-5': {'in': 1}}
    assert payload['result'] == 'ok'


# ------------------------------------------------------- evidence job build

def test_build_gate_jobs_uses_judge_parity_evidence(tmp_path, monkeypatch):
    head = 'work on the alpha thing'
    labels = [_label(1)]
    labels[0]['session_id'], labels[0]['ts'] = 'sA', '2026-07-04T10:00:00'
    labels[0]['injected'] = 'alpha-topic'
    inj_by = {('sA', '2026-07-04T10:00:00'): {
        'session_id': 'sA', 'ts': '2026-07-04T10:00:00', 'tier': 'high',
        'prompt_head': head, 'injected_facts': ['alpha fact']}}
    (tmp_path / 'proj').mkdir(parents=True)
    turns = [{'message': {'role': 'user', 'content': head}},
             {'message': {'role': 'assistant', 'content': 'z' * 4000}}]
    (tmp_path / 'proj' / 'sA.jsonl').write_text(
        '\n'.join(json.dumps(x) for x in turns))
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', tmp_path)
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', tmp_path / 'none')

    jobs = gr.build_gate_jobs(labels, inj_by)
    label, ev = jobs[0]
    assert label is labels[0]
    assert ev['topic_identity'] == ['alpha fact']
    assert len(ev['assistant_reply']) == 1400              # judge cap, NOT labeler's


def test_build_gate_jobs_missing_evidence_is_none(monkeypatch):
    monkeypatch.setattr(ej, 'resolve_transcript', lambda sid: None)
    jobs = gr.build_gate_jobs([_label(1)], {})
    assert jobs[0][1] is None


# ------------------------------------------------------------------- render

def test_render_verdict_md_carries_the_decision():
    report = {'stage': 1, 'model': 'claude-sonnet-5', 'judge_version': 'ej9-claude-sonnet',
              'verdict': 'expand',
              'main': {'n': 35, 'tp': 10, 'fp': 3, 'fn': 3, 'tn': 19,
                       'precision': 0.7692, 'recall': 0.7692,
                       'p_ci': (0.5, 0.9), 'r_ci': (0.5, 0.9)},
              'exclusion': {'n': 33, 'tp': 10, 'fp': 1, 'fn': 3, 'tn': 19,
                            'precision': 0.9091, 'recall': 0.7692,
                            'p_ci': (0.6, 0.98), 'r_ci': (0.5, 0.9)}}
    md = gr.render_verdict_md(report)
    for needle in ('VERDICT: EXPAND', 'ej9-claude-sonnet', 'claude-sonnet-5',
                   'stage 1', 'P=0.7692', 'R=0.7692', 'exclusion'):
        assert needle in md
