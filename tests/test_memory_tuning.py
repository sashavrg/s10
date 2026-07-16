import memory_index as mi

TUNING = {
    'thresholds': {'high': 0.72, 'moderate': 0.40},
    'high_ineligible': ['acme'],
    'project_catch_all_tokens': ['acme', 'dashboard', 'llm-kb'],
}


def _entry(slug, key_points):
    disp = slug.replace('-', ' ')
    mt = mi.tokenize(slug.replace('-', ' ')) | mi.tokenize(disp)
    for kp in key_points:
        mt |= mi.tokenize(kp)
    return {
        'slug': slug, 'display': disp, 'projects': [],
        'key_points': key_points,
        'match_tokens': sorted(mt),
        'slug_tokens': sorted(mi.tokenize(slug.replace('-', ' ')) | mi.tokenize(disp)),
        'path': f'compiled/topics/{slug}.md',
    }


def test_blocklisted_slug_cannot_reach_high():
    e = _entry('acme', ['install aws cli', 'create iam user', 'poll codepipeline'])
    q = mi.tokenize('acme deploy poll codepipeline iam')   # would score ~1.0 normally
    score = mi.score_entry(q, e, TUNING)
    assert mi.tier_for(score, TUNING) != 'high'


def test_bare_project_token_slug_demoted_even_with_fact_overlap():
    e = _entry('dashboard', ['uses grafana panels', 'alert rules in yaml'])
    q = mi.tokenize('dashboard grafana alert')
    score = mi.score_entry(q, e, TUNING)
    assert mi.tier_for(score, TUNING) != 'high'


def test_outcome_demoted_slug_cannot_reach_high():
    # The reversible demotion list is enforced at scoring time exactly like the
    # permanent blocklist (it just leaves the list on its own when misfires age out).
    tuning = {**TUNING, 'outcome_demoted': ['some-topic']}
    e = _entry('some-topic', ['does a specific thing', 'and another detail'])
    q = mi.tokenize('some topic does a specific thing and another detail')   # ~1.0 normally
    score = mi.score_entry(q, e, tuning)
    assert mi.tier_for(score, tuning) != 'high'


def test_normal_topic_still_reaches_high():
    e = _entry('nimbus-auth-token-refresh', ['rotate the sessionhash every 24h'])
    q = mi.tokenize('how do I do nimbus auth token refresh with sessionhash')
    score = mi.score_entry(q, e, TUNING)
    assert mi.tier_for(score, TUNING) == 'high'


def test_single_distinctive_token_plus_one_incidental_fact_is_not_high():
    # LIVE bug (api-changes / config-api-migration class): a slug whose distinctive
    # set is ONE common token scores a perfect slug-hit, and a single incidental
    # fact token used to bypass the guard -> false HIGH on off-topic prompts.
    e = _entry('api-changes', ['regenerate the widget endpoint',
                               'poll admin status until done', 'backoff on 5xx errors'])
    q = mi.tokenize('what are the changes in the status report')  # 1 distinctive + 1 incidental fact
    score = mi.score_entry(q, e, TUNING)
    assert mi.tier_for(score, TUNING) != 'high'


def test_single_distinctive_token_with_strong_fact_overlap_still_high():
    # Don't over-suppress: a single distinctive token WITH genuine topical
    # engagement (>=2 fact hits) is a real match and may still reach HIGH.
    e = _entry('api-changes', ['regenerate the widget endpoint',
                               'poll admin status until done', 'backoff on 5xx errors'])
    q = mi.tokenize('the changes to regenerate the widget endpoint and poll admin status')
    score = mi.score_entry(q, e, TUNING)
    assert mi.tier_for(score, TUNING) == 'high'


def test_project_name_slug_in_blocklist_cannot_reach_high():
    # Project-name mega-topics (acme-portal-api) are inherent catch-alls; the
    # blocklist must demote them even though they have 2+ distinctive tokens.
    tuning = {**TUNING, 'high_ineligible': ['acme-portal-api']}
    e = _entry('acme-portal-api', ['kanban ticket tracking', 'sprint board state'])
    q = mi.tokenize('acme portal kanban ticket sprint board')   # would score ~1.0 normally
    score = mi.score_entry(q, e, tuning)
    assert mi.tier_for(score, tuning) != 'high'


def test_tier_for_uses_tuning_thresholds():
    assert mi.tier_for(0.75, {'thresholds': {'high': 0.80, 'moderate': 0.40}}) == 'moderate'
    assert mi.tier_for(0.85, {'thresholds': {'high': 0.80, 'moderate': 0.40}}) == 'high'


def test_load_tuning_defaults_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, 'TUNING_PATH', tmp_path / 'missing.yaml')
    t = mi.load_tuning()
    assert t['thresholds']['high'] == 0.72
    assert t['thresholds']['moderate'] == 0.40
    assert t['high_ineligible'] == []
    assert t['auto_tune']['enabled'] is True
    assert t['auto_tune']['threshold_ceiling'] == 0.85


def test_load_tuning_parses_seed_shape(tmp_path, monkeypatch):
    f = tmp_path / 'memory_tuning.yaml'
    f.write_text(
        "thresholds:\n  high: 0.80  # inline comment\n  moderate: 0.4\n"
        "# a full-line comment\n"
        "high_ineligible:\n  - acme\n  - dashboard\n"
        "auto_tune:\n  enabled: false\n  window_days: 7\n"
    )
    monkeypatch.setattr(mi, 'TUNING_PATH', f)
    t = mi.load_tuning()
    assert t['thresholds']['high'] == 0.80          # parsed, inline comment stripped
    assert t['high_ineligible'] == ['acme', 'dashboard']
    assert t['auto_tune']['enabled'] is False
    assert t['auto_tune']['window_days'] == 7
    assert t['auto_tune']['min_samples'] == 3        # missing key filled from defaults


def test_load_tuning_falls_back_on_garbage(tmp_path, monkeypatch):
    f = tmp_path / 'memory_tuning.yaml'
    f.write_text('\x00 not: [valid')
    monkeypatch.setattr(mi, 'TUNING_PATH', f)
    t = mi.load_tuning()
    assert t['thresholds']['high'] == 0.72           # defaults, no exception


def test_format_directive_uses_softened_framing():
    out = mi.format_directive({'display': 'X', 'key_points': ['do thing']}, max_facts=5)
    assert 'authoritative' not in out.lower()
    assert 'verify against the live code' in out.lower()
    assert '- do thing' in out
