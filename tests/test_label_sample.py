import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import label_sample as ls


def _row(sid, slug, engaged, ts='t', tier='high'):
    return {'session_id': sid, 'ts': ts, 'injected': slug, 'tier': tier,
            'engaged_in_assistant': engaged, 'topic_corrected': False,
            'engaged_matched': ['tok1', 'tok2'] if engaged else []}


def test_select_sample_stratifies_and_is_deterministic():
    # tier='moderate' here so these rows fall through to the agreement strata
    # instead of being force-included by the HIGH-arm rule under test elsewhere.
    v1 = [_row(f's{i}', 'a', True, tier='moderate') for i in range(30)]
    v2 = ([_row(f's{i}', 'a', False, tier='moderate') for i in range(15)]        # disagree
          + [_row(f's{i}', 'a', True, tier='moderate') for i in range(15, 30)])  # agree-engaged
    s1 = ls.select_sample(v1, v2, per_stratum=(5, 5, 5))
    s2 = ls.select_sample(v1, v2, per_stratum=(5, 5, 5))
    assert s1 == s2                                   # deterministic, no randomness
    strata = {r['stratum'] for r in s1}
    assert 'disagree' in strata and 'agree_engaged' in strata
    assert sum(1 for r in s1 if r['stratum'] == 'disagree') == 5


def test_high_tier_rows_force_included_even_when_v1_v2_agree():
    # F1: the sample must cover the HIGH arm — every joined tier=='high' row lands
    # in stratum 'high', even when v1/v2 AGREE (which would otherwise route it to
    # agree_engaged/agree_not and risk it never appearing in a capped sample).
    v1 = [_row('s1', 'a', True, tier='high')]
    v2 = [_row('s1', 'a', True, tier='high')]   # agree: v1==v2==True
    sample = ls.select_sample(v1, v2, per_stratum=(5, 5, 5))
    assert len(sample) == 1
    assert sample[0]['stratum'] == 'high'


def test_high_stratum_is_uncapped_and_excluded_from_other_strata():
    # Uncapped: even with per_stratum caps of 5, all 12 high-tier rows appear —
    # and none of them leak into disagree/agree_* (no duplicates).
    v1 = [_row(f's{i}', 'a', True, tier='high') for i in range(12)]
    v2 = [_row(f's{i}', 'a', True, tier='high') for i in range(12)]
    sample = ls.select_sample(v1, v2, per_stratum=(5, 5, 5))
    assert len(sample) == 12
    assert all(r['stratum'] == 'high' for r in sample)


def test_v1_rows_with_scorer_version_are_excluded_from_join():
    # F9: post-flip live v2 rows in V1_PATH (tagged scorer_version) must not be
    # treated as true v1 rows for stratum assignment — they'd dilute 'disagree'.
    v1 = [_row('s1', 'a', True, tier='moderate')]
    v1[0]['scorer_version'] = 2   # this row is NOT a true v1 row
    v2 = [_row('s1', 'a', False, tier='moderate')]
    sample = ls.select_sample(v1, v2, per_stratum=(5, 5, 5))
    assert sample == []           # no true-v1 join partner -> dropped, not 'disagree'


def test_render_and_parse_roundtrip():
    sample = [dict(_row('s1', 'ecs-rollout', True), stratum='agree_engaged')]
    md = ls.render_md(sample)
    assert 'label_engaged:' in md and 'ecs-rollout' in md
    labeled = ls.parse_labels(md.replace('label_engaged: ?', 'label_engaged: yes'))
    assert labeled[0]['label_engaged'] is True
    assert labeled[0]['stratum'] == 'agree_engaged'   # F5: stratum parsed back from heading


def test_score_labels_precision_recall():
    labeled = [
        {'engaged_in_assistant': True,  'label_engaged': True},
        {'engaged_in_assistant': True,  'label_engaged': False},
        {'engaged_in_assistant': False, 'label_engaged': True},
        {'engaged_in_assistant': False, 'label_engaged': False},
    ]
    s = ls.score_labels(labeled)
    assert s['engaged_precision'] == 0.5
    assert s['engaged_recall'] == 0.5
    assert s['n'] == 4


def test_score_labels_reports_by_stratum():
    labeled = [
        {'engaged_in_assistant': True,  'label_engaged': True,  'stratum': 'high'},
        {'engaged_in_assistant': False, 'label_engaged': False, 'stratum': 'high'},
        {'engaged_in_assistant': True,  'label_engaged': False, 'stratum': 'disagree'},
    ]
    s = ls.score_labels(labeled)
    assert s['n'] == 3
    assert set(s['by_stratum']) == {'high', 'disagree'}
    assert s['by_stratum']['high'] == {'n': 2, 'engaged_precision': 1.0, 'engaged_recall': 1.0}
    assert s['by_stratum']['disagree']['n'] == 1
    assert s['by_stratum']['disagree']['engaged_precision'] == 0.0
