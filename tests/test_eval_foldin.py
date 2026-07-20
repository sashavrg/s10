"""Tests for scripts/eval_foldin.py — #1d eval-case fold-in tooling.
Spec: docs/superpowers/specs/2026-07-20-eval-foldin-design.md. Synthetic only."""
import json

import pytest

import eval_foldin as ef


def _cal(sid, ts, slug, engaged, relabel=False, tier='moderate'):
    r = {'session_id': sid, 'ts': ts, 'injected': slug, 'tier': tier,
         'label_engaged': engaged, 'label_corrected': False,
         'source': 'dev48', 'idx': 1}
    if relabel:
        r['relabel'] = 'readjudicated post-judge'
    return r


def test_build_burn_keys_fresh_high_only():
    v1 = [{'session_id': 's1', 'ts': 't1', 'injected': 'a', 'tier': 'high'},
          {'session_id': '', 'ts': 't2', 'injected': 'b', 'tier': 'high'},
          {'session_id': 's3', 'ts': 't3', 'injected': 'c', 'tier': 'moderate'},
          {'session_id': 's4', 'ts': 't4', 'injected': 'd', 'tier': 'high'}]
    v2 = [{'session_id': 's5', 'ts': 't5', 'injected': 'e', 'tier': 'high'}]
    cal_keys = {('s4', 't4', 'd')}
    assert ef.build_burn_keys(v1, v2, cal_keys) == {('s1', 't1'), ('s5', 't5')}


def test_candidate_id_deterministic_and_prefixed():
    a = ef.candidate_id('sess-1', '2026-07-01T10:00:00')
    assert a == ef.candidate_id('sess-1', '2026-07-01T10:00:00')
    assert a.startswith('fold-') and len(a) == 15
    assert a != ef.candidate_id('sess-2', '2026-07-01T10:00:00')


def test_calibration_autofold_takes_unrevised_uniform_engaged_groups():
    cal = [_cal('s1', 't1', 'slug-a', True),
           _cal('s1', 't1', 'slug-b', True),
           _cal('s2', 't2', 'slug-c', False)]
    auto, checklist = ef.calibration_autofold(cal, burn_keys=set())
    assert len(auto) == 1 and len(checklist) == 0
    assert auto[0]['expect'] == ['slug-a', 'slug-b']
    assert auto[0]['provenance'] == 'calibration'
    assert auto[0]['src'] == {'session_id': 's1', 'ts': 't1'}


def test_calibration_relabeled_positive_routes_to_checklist():
    # The one flipped-toward-judge positive class: engaged=true WITH relabel.
    cal = [_cal('s1', 't1', 'slug-a', True, relabel=True)]
    auto, checklist = ef.calibration_autofold(cal, burn_keys=set())
    assert auto == []
    assert len(checklist) == 1 and 'REVISED' in checklist[0]['signal']


def test_calibration_mixed_label_group_routes_to_checklist():
    cal = [_cal('s1', 't1', 'slug-a', True), _cal('s1', 't1', 'slug-b', False)]
    auto, checklist = ef.calibration_autofold(cal, burn_keys=set())
    assert auto == []
    assert len(checklist) == 1 and checklist[0]['expect_proposed'] == ['slug-a']


def test_calibration_burned_group_fully_excluded():
    cal = [_cal('s1', 't1', 'slug-a', True)]
    auto, checklist = ef.calibration_autofold(cal, burn_keys={('s1', 't1')})
    assert auto == [] and checklist == []


AWS_ENTRY = """### 2026-07-20 — memory-injection — HIGH 0.95 on `aws`
**Log trace:** logs/memory_injection.jsonl ts=2026-01-15T09:00:00, tier=high, score=0.95,
injected=aws, session ab12cd34.
**Status:** open

### 2026-07-02 — memory-injection — two HIGH misfires (prose only)
**Log trace:** logs/memory_injection.jsonl, session of 2026-07-02 (prompts "example prose…").
**Status:** open
"""


def test_parse_hooktuning_traces_structured_only():
    traces = ef.parse_hooktuning_traces(AWS_ENTRY)
    assert traces == [{'ts': '2026-01-15T09:00:00', 'injected': 'aws'}]


def test_hooktuning_autofold_matches_row_and_respects_burn():
    traces = [{'ts': 'tA', 'injected': 'aws'}, {'ts': 'tB', 'injected': 'x'}]
    inj = [{'session_id': 'sA', 'ts': 'tA', 'tier': 'high', 'injected': 'aws',
            'project': 'llm-kb', 'prompt_head': 'what aws the deploy status'},
           {'session_id': 'sB', 'ts': 'tB', 'tier': 'high', 'injected': 'x',
            'project': 'llm-kb', 'prompt_head': 'p'}]
    auto, burned = ef.hooktuning_autofold(traces, inj, burn_keys={('sA', 'tA')})
    assert burned == 1
    assert len(auto) == 1 and auto[0]['expect'] == []
    assert auto[0]['provenance'] == 'hook-tuning'
    assert auto[0]['src'] == {'session_id': 'sB', 'ts': 'tB'}


def test_hooktuning_trace_without_matching_row_is_dropped():
    auto, burned = ef.hooktuning_autofold([{'ts': 'tZ', 'injected': 'z'}], [], set())
    assert auto == [] and burned == 0


def _inj(sid, ts, tier, project='proj', head='the prompt head', **extra):
    return dict({'session_id': sid, 'ts': ts, 'tier': tier,
                 'project': project, 'prompt_head': head}, **extra)


def _out(sid, ts, slug, tier, engaged):
    return {'session_id': sid, 'ts': ts, 'injected': slug, 'tier': tier,
            'engaged_in_assistant': engaged}


def test_mine_moderate_positive_needs_both_proxies():
    inj = [_inj('s1', 't1', 'moderate', advertised=['slug-a']),
           _inj('s2', 't2', 'moderate', advertised=['slug-b'])]
    v1 = [_out('s1', 't1', 'slug-a', 'moderate', True),
          _out('s2', 't2', 'slug-b', 'moderate', True)]
    v2 = [_out('s1', 't1', 'slug-a', 'moderate', True),
          _out('s2', 't2', 'slug-b', 'moderate', False)]      # v2 disagrees
    cands = ef.mine_candidates(inj, v1, v2, set(), set())
    pos = [c for c in cands if c['expect_proposed']]
    assert len(pos) == 1 and pos[0]['expect_proposed'] == ['slug-a']
    # s2 is a proxy DISAGREEMENT — neither class 3 (both engaged) nor class 4
    # (both not-engaged), so per spec §3 it is not a candidate at all
    assert [c for c in cands if c['src']['session_id'] == 's2'] == []


def test_mine_none_skipped_are_negative_candidates():
    inj = [_inj('s1', 't1', 'none'), _inj('s2', 't2', 'skipped')]
    cands = ef.mine_candidates(inj, [], [], set(), set())
    assert all(c['expect_proposed'] == [] for c in cands) and len(cands) == 2


def test_mine_sessionless_high_flagged_hide_score():
    inj = [_inj('', 'tX', 'high', injected='slug-h')]
    cands = ef.mine_candidates(inj, [], [], set(), set())
    assert len(cands) == 1
    c = cands[0]
    assert c['expect_proposed'] == ['slug-h'] and c['hide_score'] is True
    assert 'NOT evidence' in c['signal']


def test_mine_respects_burn_and_taken_keys():
    inj = [_inj('s1', 't1', 'none'), _inj('s2', 't2', 'none')]
    cands = ef.mine_candidates(inj, [], [], burn_keys={('s1', 't1')},
                               taken_keys={('s2', 't2')})
    assert cands == []


def test_recover_prompt_full_turn_else_head(tmp_path, monkeypatch):
    import engagement_judge as ej
    turns = [{'message': {'role': 'user', 'content': 'the prompt head plus the whole rest of the turn'}},
             {'message': {'role': 'assistant', 'content': 'ok'}}]
    (tmp_path / 'p').mkdir()
    (tmp_path / 'p' / 'sA.jsonl').write_text('\n'.join(json.dumps(t) for t in turns))
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', tmp_path)
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', tmp_path / 'none')
    prompt, head_only = ef.recover_prompt(_inj('sA', 't1', 'none'))
    assert head_only is False and prompt.endswith('rest of the turn')
    prompt2, head_only2 = ef.recover_prompt(_inj('sMISSING', 't1', 'none'))
    assert head_only2 is True and prompt2 == 'the prompt head'


def test_propose_kind():
    assert ef.propose_kind('anything', []) == 'negative'
    assert ef.propose_kind('regenerate the stale widget cache', ['widget-cache']) == 'direct'
    assert ef.propose_kind('the brand page shows old data', ['widget-cache']) == 'paraphrase'


def _cand(sid, ts, prompt, expect):
    return {'src': {'session_id': sid, 'ts': ts}, 'prompt': prompt,
            'expect_proposed': expect, 'signal': 's', 'project': 'p',
            'hide_score': False}


def test_dedup_exact_duplicate_is_silent_skip():
    existing = [{'id': 'para-x', 'prompt': 'Fix the widget!', 'expect': ['w']}]
    kept, coll, counts = ef.dedup_candidates(
        [_cand('s1', 't1', 'fix the  widget', ['w'])], existing)
    assert kept == [] and coll == [] and counts['dup_exact'] == 1


def test_dedup_prompt_collision_different_expect_surfaces():
    existing = [{'id': 'para-x', 'prompt': 'fix the widget', 'expect': ['w']}]
    kept, coll, counts = ef.dedup_candidates(
        [_cand('s1', 't1', 'fix the widget', ['other'])], existing)
    assert kept == []
    assert len(coll) == 1
    assert coll[0]['collision'] == {'case_id': 'para-x', 'their_expect': ['w']}


def test_dedup_already_folded_id_skips():
    sid, ts = 's1', 't1'
    existing = [{'id': ef.candidate_id(sid, ts), 'prompt': 'other', 'expect': []}]
    kept, coll, counts = ef.dedup_candidates([_cand(sid, ts, 'p', [])], existing)
    assert kept == [] and counts['dup_id'] == 1


def test_dedup_among_candidates_same_prompt_different_expect_collides():
    c1 = _cand('s1', 't1', 'same prompt', ['a'])
    c2 = _cand('s2', 't2', 'same prompt', ['b'])
    kept, coll, counts = ef.dedup_candidates([c1, c2], [])
    assert len(kept) == 1 and len(coll) == 1


def _full_cand(sid='s1', ts='t1', **over):
    c = {'src': {'session_id': sid, 'ts': ts}, 'prompt': 'do the thing',
         'head_only': False, 'project': 'proj', 'expect_proposed': ['slug-a'],
         'signal': 'moderate: both proxies say engaged', 'hide_score': False}
    c.update(over)
    return c


def test_render_checklist_contains_alignment_question_and_fields():
    md = ef.render_checklist([_full_cand()])
    for needle in ('Should THIS prompt retrieve', '- expect: slug-a',
                   '- keep: ?', '- kind: paraphrase', 'do the thing',
                   '(project: proj)'):
        assert needle in md


def test_render_hides_score_and_never_shows_tier_for_hidden_rows():
    c = _full_cand(hide_score=True, signal='HIGH fire with NO outcome signal — '
                   'judge independently; the scorer fire is NOT evidence')
    md = ef.render_checklist([c])
    assert 'score' not in md.lower().replace('the scorer fire', '')
    assert 'tier' not in md.lower()


def test_render_collision_shows_both_expects():
    c = _full_cand(collision={'case_id': 'para-x', 'their_expect': ['w']})
    md = ef.render_checklist([c])
    assert 'para-x' in md and 'w' in md and 'COLLISION' in md


def test_checklist_roundtrip_with_edits_and_incomplete():
    cands = [_full_cand('s1', 't1'), _full_cand('s2', 't2')]
    md = ef.render_checklist(cands)
    md = md.replace('- keep: ?', '- keep: yes', 1)
    md = md.replace('- expect: slug-a', '- expect: slug-edited', 1)
    md = md.replace('- kind: paraphrase', '- kind: direct', 1)
    answers, incomplete = ef.parse_checklist(md)
    assert incomplete == [2]
    assert answers == [{'src': {'session_id': 's1', 'ts': 't1'}, 'keep': True,
                        'expect': ['slug-edited'], 'kind': 'direct'}]


def test_checklist_in_progress_detection():
    md = ef.render_checklist([_full_cand()])
    assert ef.checklist_in_progress(md) is False
    assert ef.checklist_in_progress(md.replace('- keep: ?', '- keep: no')) is True


def test_assign_fold_split_groups_by_session():
    r1 = {'id': 'fold-aaaaaaaaaa', 'src': {'session_id': 'sess-X', 'ts': 't1'}}
    r2 = {'id': 'fold-bbbbbbbbbb', 'src': {'session_id': 'sess-X', 'ts': 't2'}}
    assert ef.assign_fold_split(r1) == ef.assign_fold_split(r2)
    assert ef.assign_fold_split(r1) in ('train', 'val', 'held_out')


def test_fold_end_to_end(tmp_path, monkeypatch):
    import engagement_judge as ej
    import eval_split
    import memory_index as mi
    monkeypatch.setattr(mi, 'load_index', lambda: {'entries': [{'slug': 'slug-a'}]})
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('# comment line\n'
                     + json.dumps({'id': 'para-x', 'kind': 'paraphrase',
                                   'project': 'p', 'prompt': 'old case',
                                   'expect': ['w'], 'split': 'train'}) + '\n')
    inj = [_inj('sA', 't1', 'moderate', advertised=['slug-a'], head='full prompt here')]
    (tmp_path / 'tx').mkdir()
    turns = [{'message': {'role': 'user', 'content': 'full prompt here indeed'}},
             {'message': {'role': 'assistant', 'content': 'ok'}}]
    (tmp_path / 'tx' / 'sA.jsonl').write_text('\n'.join(json.dumps(t) for t in turns))
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', tmp_path / 'tx')
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', tmp_path / 'none')
    monkeypatch.setattr(ef, 'CASES_PATH', cases)
    monkeypatch.setattr(eval_split, 'CASES_PATH', cases)
    inj_path = tmp_path / 'inj.jsonl'
    inj_path.write_text('\n'.join(json.dumps(r) for r in inj))
    monkeypatch.setattr(ef, 'INJECTION_LOG_PATH', inj_path)
    auto = tmp_path / 'auto.jsonl'
    auto.write_text(json.dumps({'id': ef.candidate_id('sA', 't1'), 'expect': ['slug-a'],
                                'provenance': 'calibration',
                                'src': {'session_id': 'sA', 'ts': 't1'}}) + '\n')
    monkeypatch.setattr(ef, 'AUTO_STAGE_PATH', auto)
    monkeypatch.setattr(ef, 'REVIEW_PATH', tmp_path / 'review.md')

    counts = ef.fold(answers=[])

    lines = [json.loads(l) for l in cases.read_text().splitlines()
             if l.strip() and not l.startswith('#')]
    assert len(lines) == 2
    new = [l for l in lines if l['id'].startswith('fold-')][0]
    assert new['expect'] == ['slug-a'] and new['provenance'] == 'calibration'
    assert new['prompt'].startswith('full prompt here')
    assert new['split'] in ('train', 'val', 'held_out')
    assert lines[0]['split'] == 'train'            # existing split preserved
    assert counts['folded'] == 1
    assert counts['unknown_slugs'] == []
    assert cases.read_text().startswith('# comment line')


def test_status_counts_balance():
    cases = [{'expect': ['a'], 'kind': 'direct', 'split': 'train', 'project': 'p'},
             {'expect': [], 'kind': 'negative', 'split': 'val', 'project': 'p'}]
    s = ef.status_counts(cases)
    assert s['n'] == 2 and s['pos'] == 1 and s['neg'] == 1
    assert s['remaining_to_target'] == ef.TARGET - 2


def test_build_burn_keys_includes_injection_log_high_without_outcome():
    # A HIGH injection whose SessionEnd outcome row has not been written yet
    # (e.g. the session is still running) is a FUTURE gate row — burned.
    inj = [{'session_id': 's9', 'ts': 't9', 'tier': 'high', 'injected': 'aws'},
           {'session_id': '', 'ts': 't8', 'tier': 'high', 'injected': 'x'},
           {'session_id': 'sC', 'ts': 'tC', 'tier': 'high', 'injected': 'cal'}]
    burn = ef.build_burn_keys([], [], {('sC', 'tC', 'cal')}, inj_rows=inj)
    assert burn == {('s9', 't9')}
