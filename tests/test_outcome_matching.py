import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import outcome_matching as om


def test_match_word_boundary_not_substring():
    # v1 bug: "api" matched "therapist". v2 must not.
    r = om.match({'api', 'widget'}, 'the therapist made a widgetry note')
    assert r['hit'] is False
    assert r['matched'] == []


def test_match_quorum_two_tokens_required():
    probe = {'ecs', 'rollout', 'portal', 'deploy'}
    one = om.match(probe, 'we shipped the portal today')
    assert one['hit'] is False and one['matched'] == ['portal']
    two = om.match(probe, 'the portal deploy finished')
    assert two['hit'] is True and two['matched'] == ['deploy', 'portal']


def test_match_single_token_probe_needs_one():
    r = om.match({'tailscale'}, 'check the tailscale acl')
    assert r['hit'] is True and r['matched'] == ['tailscale']


def test_match_empty_probe_never_hits():
    assert om.match(set(), 'anything at all')['hit'] is False


def test_score_row_shape_and_version():
    row = {'session_id': 's1', 'project': 'p', 'ts': '2026-07-01T10:00:00',
           'tier': 'high', 'injected': 'ecs-rollout-polling',
           'injected_facts': ['poll rolloutState until COMPLETED'], 'score': 0.9}
    out = om.score_row(row, 'we poll rolloutstate until the ecs rollout is completed', '')
    assert out['scorer_version'] == 2
    assert out['engaged_in_assistant'] is True
    assert out['topic_corrected'] is False
    assert out['injected'] == 'ecs-rollout-polling'
    assert isinstance(out['engaged_matched'], list) and len(out['engaged_matched']) <= 10


def test_score_row_moderate_uses_advertised_slug():
    row = {'session_id': 's1', 'project': 'p', 'ts': 't', 'tier': 'moderate',
           'advertised': ['openrgb-shutdown-fix'], 'score': 0.5}
    out = om.score_row(row, 'unrelated text', '')
    assert out['injected'] == 'openrgb-shutdown-fix'
    assert out['engaged_in_assistant'] is False


def test_score_row_engagement_diagnostics_uncapped_and_slug_only():
    # F2: sensitivity-parity diagnostics, verdict UNCHANGED.
    facts = ['alpha beta gamma delta epsilon zeta eta theta iota kappa lambda omega']
    row = {'session_id': 's1', 'project': 'p', 'ts': 't', 'tier': 'high',
           'injected': 'foo-bar', 'injected_facts': facts}
    text = ('alpha beta gamma delta epsilon zeta eta theta iota kappa lambda omega '
            'values were discussed at length')
    out = om.score_row(row, text, '')
    # engagement came entirely from fact tokens, not the slug tokens ('foo'/'bar'
    # never appear) -> the quorum rule using ONLY slug_tokens would NOT fire.
    assert out['engaged_in_assistant'] is True
    assert out['engaged_slug_only'] is False
    # 12 distinct probe tokens matched, but engaged_matched is capped at 10.
    assert out['engaged_matched_n'] == 12
    assert len(out['engaged_matched']) == 10
    assert out['engaged_matched_n'] > len(out['engaged_matched'])


def test_score_row_corrected_uses_full_probe_with_quorum():
    row = {'session_id': 's1', 'project': 'p', 'ts': 't', 'tier': 'high',
           'injected': 'qwen-model-vram', 'injected_facts': ['qwen3 8b spills on 6gb vram']}
    out = om.score_row(row, 'x', 'actually the vram limit applies to qwen3 only above 8b')
    assert out['topic_corrected'] is True
    assert 'vram' in out['corrected_matched']
