import datetime as dt
import json
import subprocess
import audit_hooks as a

TUNING = {
    'thresholds': {'high': 0.72, 'moderate': 0.40},
    'high_ineligible': ['acme'],
    'project_catch_all_tokens': ['acme'],
    'auto_tune': {
        'enabled': True, 'window_days': 14, 'min_samples': 3,
        'cross_project_min': 3, 'threshold_step': 0.01,
        'threshold_ceiling': 0.85, 'false_high_rate_trigger': 0.30,
    },
}


def _rec(tier, slug, project, head, ts='2026-06-20T10:00:00'):
    return {'tier': tier, 'injected': slug, 'project': project,
            'prompt_head': head, 'ts': ts}


def test_read_log_filters_by_window():
    text = '\n'.join([
        '{"tier":"high","ts":"2026-06-20T10:00:00"}',
        '{"tier":"high","ts":"2026-05-01T10:00:00"}',   # outside 14d window
        'not json',                                      # tolerated, skipped
    ])
    now = dt.datetime(2026, 6, 24, 0, 0, 0)
    recs = a.read_log(text, window_days=14, now=now)
    assert len(recs) == 1


def test_is_synthetic_head():
    assert a.is_synthetic_head('<task-notification>\n...')
    assert a.is_synthetic_head('[Request interrupted')
    assert not a.is_synthetic_head('real user prompt')


def test_classify_counts_synthetic_and_rate():
    recs = [
        _rec('high', 'api-changes', 'acme-portal-api', '<task-notification> x'),
        _rec('high', 'api-changes', 'acme-portal-api', '<task-notification> y'),
        _rec('high', 'api-changes', 'acme-portal-api', '<task-notification> z'),
        _rec('high', 'nimbus-auth', 'nimbus', 'real user question'),
    ]
    stats = a.classify(recs, TUNING)
    assert stats['total_high'] == 4
    assert stats['synthetic_high'] == 3
    assert stats['slug_misfires']['api-changes'] == 3
    assert stats['false_high_rate'] == 0.75


def test_decide_quarantines_repeat_offender_tighten_only():
    stats = {'total_high': 4, 'synthetic_high': 3, 'false_high_rate': 0.75,
             'slug_misfires': {'api-changes': 3, 'acme': 5}, 'promiscuous': []}
    actions = a.decide(stats, TUNING)
    assert 'api-changes' in actions['add_block']      # >= min_samples, not yet blocked
    assert 'acme' not in actions['add_block']        # already blocklisted -> not re-added
    # false_high_rate 0.75 >= 0.30 trigger -> raise high by step, capped at ceiling
    assert actions['new_high'] == 0.73
    assert actions['new_high'] > TUNING['thresholds']['high']


def test_decide_never_lowers_or_unblocks():
    # Even with a clean log, decide must never propose lowering or un-blocklisting.
    stats = {'total_high': 10, 'synthetic_high': 0, 'false_high_rate': 0.0,
             'slug_misfires': {}, 'promiscuous': []}
    actions = a.decide(stats, TUNING)
    assert actions['add_block'] == []
    assert actions['new_high'] is None                 # never lowers


def test_decide_respects_ceiling():
    t = {**TUNING, 'thresholds': {'high': 0.85, 'moderate': 0.40}}
    stats = {'total_high': 4, 'synthetic_high': 4, 'false_high_rate': 1.0,
             'slug_misfires': {}, 'promiscuous': []}
    actions = a.decide(stats, t)
    assert actions['new_high'] is None                 # already at ceiling, no raise


def _out(slug, engaged, corrected=False, tier='high', ts='2026-06-20T10:00:00'):
    return {'tier': tier, 'injected': slug, 'engaged_in_assistant': engaged,
            'topic_corrected': corrected, 'ts': ts}


def test_classify_outcomes_tracks_useful_harmful_and_rate():
    # gate 3: useful = engaged AND NOT corrected; harmful = engaged AND corrected.
    outs = [
        _out('off-topic', engaged=False),                        # non-useful (unengaged)
        _out('off-topic', engaged=False),
        _out('off-topic', engaged=False),
        _out('seductive', engaged=True, corrected=True),         # harmful (took the bait)
        _out('helper', engaged=True),                            # useful
        _out('moderate-noise', engaged=False, tier='moderate'),  # not HIGH -> ignored
    ]
    o = a.classify_outcomes(outs, TUNING)
    assert o['outcome_high'] == 5
    assert o['outcome_non_useful'] == 4                          # 3 off-topic + 1 harmful
    assert o['outcome_false_high_rate'] == round(4 / 5, 4)
    assert o['outcome_slug']['off-topic'] == {
        'high': 3, 'engaged': 0, 'useful': 0, 'harmful': 0, 'corrected': 0}
    assert o['outcome_slug']['seductive'] == {
        'high': 1, 'engaged': 1, 'useful': 0, 'harmful': 1, 'corrected': 1}
    assert o['outcome_slug']['helper']['useful'] == 1


def test_decide_demotes_on_zero_useful_and_spares_one_useful():
    # gate 3: a slug whose only engagements were CORRECTIONS (seductive-wrong,
    # useful==0) is demoted; one clean useful inject spares a slug. gate 2: the
    # demotion lands in the SEPARATE reversible set, not the permanent blocklist.
    stats = {'total_high': 0, 'synthetic_high': 0, 'false_high_rate': 0.0,
             'slug_misfires': {}, 'promiscuous': []}
    ostats = {'outcome_high': 9, 'outcome_non_useful': 6, 'outcome_false_high_rate': 0.67,
              'outcome_slug': {
                  'off-topic':   {'high': 3, 'engaged': 0, 'useful': 0, 'harmful': 0, 'corrected': 0},
                  'seductive':   {'high': 3, 'engaged': 3, 'useful': 0, 'harmful': 3, 'corrected': 3},
                  'real-helper': {'high': 3, 'engaged': 3, 'useful': 1, 'harmful': 2, 'corrected': 2},
              }}
    actions = a.decide(stats, TUNING, ostats)
    assert actions['outcome_demoted'] == ['off-topic', 'seductive']   # both useful==0
    assert 'real-helper' not in actions['outcome_demoted']            # one useful -> spared
    assert actions['add_block'] == []                                # NOT the permanent list
    assert 'seductive' in actions['outcome_reasons']


def test_decide_outcome_rate_raises_threshold_only_with_enough_samples():
    stats = {'total_high': 0, 'synthetic_high': 0, 'false_high_rate': 0.0,
             'slug_misfires': {}, 'promiscuous': []}
    big = {'outcome_high': 4, 'outcome_non_useful': 2, 'outcome_false_high_rate': 0.5,
           'outcome_slug': {}}
    assert a.decide(stats, TUNING, big)['new_high'] == 0.73         # samples >= min -> raise
    small = {'outcome_high': 2, 'outcome_non_useful': 2, 'outcome_false_high_rate': 1.0,
             'outcome_slug': {}}
    assert a.decide(stats, TUNING, small)['new_high'] is None       # too few -> no raise


def test_decide_without_ostats_is_backward_compatible():
    stats = {'total_high': 4, 'synthetic_high': 3, 'false_high_rate': 0.75,
             'slug_misfires': {'api-changes': 3}, 'promiscuous': []}
    actions = a.decide(stats, TUNING)            # legacy 2-arg call
    assert 'api-changes' in actions['add_block']
    assert actions['new_high'] == 0.73
    assert actions['outcome_demoted'] == []
    assert actions['outcome_reasons'] == {}


def test_replace_list_section_creates_replaces_and_clears():
    base = "thresholds:\n  high: 0.72\n"
    t1 = a.replace_list_section(base, 'outcome_demoted', ['a', 'b'])      # create
    assert 'outcome_demoted:\n  - a\n  - b' in t1
    t2 = a.replace_list_section(t1, 'outcome_demoted', ['a'])            # shrink (reversible)
    assert '  - a' in t2 and '  - b' not in t2
    t3 = a.replace_list_section(t2, 'outcome_demoted', [])              # clear
    assert '  - a' not in t3
    assert a.replace_list_section(base, 'outcome_demoted', []) == base   # absent + empty -> no-op


def test_apply_actions_outcome_demoted_never_removes_across_runs():
    # Gate-2 as AMENDED (operator ruling 2026-09-20, tune-merge plan (a)): the auditor
    # only ever ADDS to outcome_demoted. A slug that no longer qualifies stays in the
    # file — its removal is a loosening, and loosenings are human-only (decide()
    # surfaces it as an `aged_out` suggestion instead). Pre-amendment this test
    # asserted the opposite (wholesale rewrite → y dropped).
    seed = SEED + "outcome_demoted:\n"
    t1, c1 = a.apply_actions(
        {'add_block': [], 'outcome_demoted': ['x', 'y'], 'new_high': None}, seed)
    assert '  - x' in t1 and '  - y' in t1 and c1 == ['outcome_demoted += x, y']
    t2, c2 = a.apply_actions(
        {'add_block': [], 'outcome_demoted': ['x'], 'new_high': None}, t1)
    assert '  - x' in t2 and '  - y' in t2 and c2 == []   # y stays; no change reported
    assert '  - acme' in t2                             # permanent high_ineligible untouched


SEED = (
    "thresholds:\n  high: 0.72            # >= -> HIGH\n  moderate: 0.4\n\n"
    "high_ineligible:\n  - acme              # seeded\n\n"
    "project_catch_all_tokens:\n  - acme\n  - dashboard\n"
)


def test_append_to_list_section_inserts_after_last_item():
    out = a.append_to_list_section(SEED, 'high_ineligible', 'api-changes')
    assert '  - acme' in out
    assert '  - api-changes' in out
    # inserted under high_ineligible, not under project_catch_all_tokens
    block = out.split('high_ineligible:')[1].split('project_catch_all_tokens:')[0]
    assert 'api-changes' in block


def test_append_to_list_section_is_idempotent():
    out = a.append_to_list_section(SEED, 'high_ineligible', 'acme')
    assert out == SEED                      # already present -> unchanged


def test_set_nested_scalar_preserves_inline_comment():
    out = a.set_nested_scalar(SEED, 'thresholds', 'high', 0.73)
    assert '  high: 0.73            # >= -> HIGH' in out
    assert '  moderate: 0.4' in out          # untouched


def test_apply_actions_blocklist_and_threshold():
    actions = {'add_block': ['api-changes'], 'new_high': 0.73, 'suggestions': []}
    new_text, changes = a.apply_actions(actions, SEED)
    assert '  - api-changes' in new_text
    assert '  high: 0.73' in new_text
    assert any('api-changes' in c for c in changes)
    assert any('0.73' in c for c in changes)


def test_apply_actions_noop_when_nothing_to_do():
    actions = {'add_block': [], 'new_high': None, 'suggestions': []}
    new_text, changes = a.apply_actions(actions, SEED)
    assert new_text == SEED
    assert changes == []


def test_append_to_list_section_creates_section_if_absent():
    out = a.append_to_list_section("thresholds:\n  high: 0.72\n", 'high_ineligible', 'newslug')
    assert 'high_ineligible:\n  - newslug' in out


def test_set_nested_scalar_raises_if_missing():
    import pytest
    with pytest.raises(KeyError):
        a.set_nested_scalar("thresholds:\n  high: 0.72\n", 'thresholds', 'nonexistent', 1.0)


def test_append_to_list_section_dedups_across_blank_separated_items():
    text = "high_ineligible:\n  - acme\n\n  - dashboard\n\nproject_catch_all_tokens:\n  - x\n"
    out = a.append_to_list_section(text, 'high_ineligible', 'dashboard')   # already present after a blank
    assert out == text   # no duplicate


def test_set_nested_scalar_keeps_comment_with_narrower_value():
    text = "thresholds:\n  high: 0.72            # >= -> HIGH\n  moderate: 0.4\n"
    out = a.set_nested_scalar(text, 'thresholds', 'high', 0.8)
    assert 'high: 0.8' in out
    assert '# >= -> HIGH' in out
    assert '  moderate: 0.4' in out


def test_edited_yaml_round_trips_through_memory_index_parser():
    import memory_index as mi
    actions = {'add_block': ['api-changes'], 'outcome_demoted': ['demoted-x'],
               'new_high': 0.73, 'suggestions': []}
    new_text, _ = a.apply_actions(actions, SEED)
    parsed = mi._parse_tuning_yaml(new_text)
    assert parsed['thresholds']['high'] == 0.73
    assert 'api-changes' in parsed['high_ineligible']
    assert 'acme' in parsed['high_ineligible']
    assert 'demoted-x' in parsed['outcome_demoted']        # reversible set round-trips too


def test_render_audit_section_includes_changes_and_suggestions():
    out = a.render_audit_section(
        '2026-06-24',
        {'total_high': 4, 'synthetic_high': 3, 'false_high_rate': 0.75},
        ['blocklist += api-changes', 'thresholds.high -> 0.73'],
        ["re-scope candidate: global slug 'x' ..."],
    )
    assert '2026-06-24' in out
    assert 'api-changes' in out
    assert 're-scope candidate' in out
    assert '0.75' in out


def test_run_dry_run_makes_no_changes(tmp_path, monkeypatch):
    # Point the auditor at a tmp log + tmp tuning file; dry-run must not write.
    log = tmp_path / 'mem.jsonl'
    log.write_text('\n'.join(
        '{"tier":"high","injected":"api-changes","project":"acme-portal-api",'
        '"prompt_head":"<task-notification> n","ts":"2026-06-23T10:00:00"}'
        for _ in range(3)
    ))
    tun = tmp_path / 'memory_tuning.yaml'
    tun.write_text(SEED + "auto_tune:\n  enabled: true\n  window_days: 14\n"
                          "  min_samples: 3\n  cross_project_min: 3\n"
                          "  threshold_step: 0.01\n  threshold_ceiling: 0.85\n"
                          "  false_high_rate_trigger: 0.3\n")
    monkeypatch.setattr(a, 'LOG_PATH', log)
    monkeypatch.setattr(a, 'OUTCOME_PATH', tmp_path / 'no_outcomes.jsonl')   # absent
    monkeypatch.setattr(a, 'TUNING_PATH', tun)
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tun)
    before = tun.read_text()
    res = a.run(now=dt.datetime(2026, 6, 24), dry_run=True)
    assert res['changed'] is True            # it WOULD change
    assert 'api-changes' in str(res['changes'])
    assert tun.read_text() == before          # but wrote nothing (dry-run)


def test_run_consumes_outcome_log_dry_run(tmp_path, monkeypatch):
    # Loop-gap closed end-to-end: NO synthetic misfires in the injection log, the
    # offender is learned ENTIRELY from real-traffic outcomes (HIGH, never engaged).
    log = tmp_path / 'mem.jsonl'
    log.write_text('')                                   # zero synthetic signal
    out = tmp_path / 'outcomes.jsonl'
    out.write_text('\n'.join(json.dumps({
        'tier': 'high', 'injected': 'off-topic-slug', 'engaged_in_assistant': False,
        'ts': '2026-06-23T10:00:00',
    }) for _ in range(3)) + '\n')
    tun = tmp_path / 'memory_tuning.yaml'
    tun.write_text(SEED_WITH_AUTO_TUNE)
    monkeypatch.setattr(a, 'LOG_PATH', log)
    monkeypatch.setattr(a, 'OUTCOME_PATH', out)
    monkeypatch.setattr(a, 'TUNING_PATH', tun)
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tun)
    res = a.run(now=dt.datetime(2026, 6, 24), dry_run=True)
    assert res['changed'] is True
    assert 'off-topic-slug' in str(res['changes'])       # blocklisted from outcomes alone
    assert res['stats']['outcome_high'] == 3
    assert tun.read_text() == SEED_WITH_AUTO_TUNE          # dry-run wrote nothing


def test_run_disabled_is_noop(tmp_path, monkeypatch):
    tun = tmp_path / 'memory_tuning.yaml'
    tun.write_text(SEED + "auto_tune:\n  enabled: false\n  window_days: 14\n"
                          "  min_samples: 3\n  cross_project_min: 3\n"
                          "  threshold_step: 0.01\n  threshold_ceiling: 0.85\n"
                          "  false_high_rate_trigger: 0.3\n")
    log = tmp_path / 'mem.jsonl'
    log.write_text('')
    monkeypatch.setattr(a, 'LOG_PATH', log)
    monkeypatch.setattr(a, 'TUNING_PATH', tun)
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tun)
    res = a.run(now=dt.datetime(2026, 6, 24), dry_run=False)
    assert res['changed'] is False
    assert res.get('disabled') is True


SEED_WITH_AUTO_TUNE = (
    SEED +
    "auto_tune:\n"
    "  enabled: true\n"
    "  window_days: 14\n"
    "  min_samples: 3\n"
    "  cross_project_min: 3\n"
    "  threshold_step: 0.01\n"
    "  threshold_ceiling: 0.85\n"
    "  false_high_rate_trigger: 0.3\n"
)


def _git_c(tmp, *args):
    return subprocess.run(['git', '-C', str(tmp), *args],
                          capture_output=True, text=True)


def _init_tmp_repo(tmp_path):
    """Bootstrap a minimal git repo with the two files audit_hooks.py writes."""
    tmp = tmp_path / 'repo'
    tmp.mkdir()
    _git_c(tmp, 'init', '-b', 'main')
    _git_c(tmp, 'config', 'user.email', 'test@example.com')
    _git_c(tmp, 'config', 'user.name', 'Test User')

    # Create directory structure
    (tmp / 'config').mkdir()
    (tmp / 'docs' / 'hook-tuning').mkdir(parents=True)
    (tmp / 'logs').mkdir()

    tuning = tmp / 'config' / 'memory_tuning.yaml'
    tuning.write_text(SEED_WITH_AUTO_TUNE)
    # The audit doc is local DATA (it quotes real prompts) — never tracked.
    audit_doc = tmp / 'docs' / 'hook-tuning' / 'auto-audit.md'
    audit_doc.write_text('# Auto-audit log\n')
    # Commit logs/.gitkeep so the logs/ dir is tracked and won't appear as
    # untracked noise in git status --porcelain after the run.
    (tmp / 'logs' / '.gitkeep').write_text('')

    _git_c(tmp, 'add', 'config/memory_tuning.yaml', 'logs/.gitkeep')
    _git_c(tmp, 'commit', '-m', 'seed')
    return tmp, tuning, audit_doc


def test_run_clean_tree_invariant(tmp_path, monkeypatch):
    """run() must leave the working tree clean and restore the original branch,
    even when push fails (no remote configured). An auto-tune branch is created
    and contains the updated tuning file."""
    tmp, tuning, audit_doc = _init_tmp_repo(tmp_path)

    # Capture the starting branch name
    start_branch = _git_c(tmp, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()

    # Write synthetic HIGH log entries for 'monitoring-setup' across different projects
    log_path = tmp / 'logs' / 'memory_injection.jsonl'
    lines = []
    for i, proj in enumerate(['proj-a', 'proj-b', 'proj-c']):
        lines.append(json.dumps({
            'tier': 'high',
            'injected': 'monitoring-setup',
            'project': proj,
            'prompt_head': '<task-notification> synthetic',
            'ts': '2026-06-23T10:00:00',
        }))
    log_path.write_text('\n'.join(lines) + '\n')

    # Monkeypatch module-level paths to point at our tmp repo
    monkeypatch.setattr(a, 'BASE_DIR', tmp)
    monkeypatch.setattr(a, 'TUNING_PATH', tuning)
    monkeypatch.setattr(a, 'AUDIT_DOC', audit_doc)
    monkeypatch.setattr(a, 'LOG_PATH', log_path)
    monkeypatch.setattr(a, 'OUTCOME_PATH', tmp / 'logs' / 'no_outcomes.jsonl')   # absent
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tuning)

    res = a.run(now=dt.datetime(2026, 6, 24), dry_run=False)

    # Push will fail (no remote) — that must NOT raise; pushed=False is fine
    assert res.get('pushed') is False or res.get('pushed') is True  # either is ok
    assert res['changed'] is True

    # The tracked file _commit_to_branch touches must be clean (not dirty/staged).
    status_tuning = _git_c(tmp, 'status', '--porcelain',
                           'config/memory_tuning.yaml').stdout.strip()
    assert status_tuning == '', f"memory_tuning.yaml dirty after run(): {status_tuning!r}"
    # The audit doc is local data: never committed (untracked at most), but the
    # appended audit section must survive the branch round-trip on disk.
    status_audit = _git_c(tmp, 'status', '--porcelain',
                          'docs/hook-tuning/auto-audit.md').stdout.strip()
    assert status_audit in ('', '?? docs/hook-tuning/auto-audit.md'), \
        f"auto-audit.md tracked/staged after run(): {status_audit!r}"
    assert audit_doc.read_text() != '# Auto-audit log\n', \
        'audit section was not appended to the local audit doc'

    # Current branch must be back to where we started
    current = _git_c(tmp, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    assert current == start_branch, f"Branch not restored: expected {start_branch!r}, got {current!r}"

    # An auto-tune branch must have been created
    branches = _git_c(tmp, 'branch', '--list', 'auto-tune/*').stdout.strip()
    assert 'auto-tune/2026-06-24' in branches, f"auto-tune branch not found: {branches!r}"

    # The committed tuning file on that branch must reference monitoring-setup
    show = _git_c(tmp, 'show', 'auto-tune/2026-06-24:config/memory_tuning.yaml')
    assert 'monitoring-setup' in show.stdout, \
        f"monitoring-setup not in committed tuning file:\n{show.stdout}"


def test_run_skips_dirty_tracked_tree(tmp_path, monkeypatch):
    """A dirty TRACKED tree must abort auto-tune cleanly: no branch, no commit,
    the tree left exactly as-is. Switching to the auto-tune branch with unrelated
    uncommitted changes present would otherwise strand them on the wrong branch."""
    tmp, tuning, audit_doc = _init_tmp_repo(tmp_path)
    start_branch = _git_c(tmp, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()

    # Dirty an unrelated TRACKED file (logs/.gitkeep is committed by the helper).
    (tmp / 'logs' / '.gitkeep').write_text('dirty\n')

    log_path = tmp / 'logs' / 'memory_injection.jsonl'
    log_path.write_text('\n'.join(json.dumps({
        'tier': 'high', 'injected': 'monitoring-setup', 'project': p,
        'prompt_head': '<task-notification> synthetic', 'ts': '2026-06-23T10:00:00',
    }) for p in ['proj-a', 'proj-b', 'proj-c']) + '\n')

    monkeypatch.setattr(a, 'BASE_DIR', tmp)
    monkeypatch.setattr(a, 'TUNING_PATH', tuning)
    monkeypatch.setattr(a, 'AUDIT_DOC', audit_doc)
    monkeypatch.setattr(a, 'LOG_PATH', log_path)
    monkeypatch.setattr(a, 'OUTCOME_PATH', tmp / 'logs' / 'no_outcomes.jsonl')   # absent
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tuning)

    res = a.run(now=dt.datetime(2026, 6, 24), dry_run=False)

    assert res.get('skipped') == 'dirty-tree'
    # No auto-tune branch created, still on the original branch.
    assert _git_c(tmp, 'branch', '--list', 'auto-tune/*').stdout.strip() == ''
    assert _git_c(tmp, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() == start_branch
    # The dirty file is untouched and the tuning file was never modified.
    assert (tmp / 'logs' / '.gitkeep').read_text() == 'dirty\n'
    assert 'monitoring-setup' not in tuning.read_text()


# --- decay is a human decision (operator ruling 2026-09-20, plan amendment (a)) ------

STATS_QUIET = {'total_high': 0, 'synthetic_high': 0, 'false_high_rate': 0.0,
               'slug_misfires': {}, 'promiscuous': []}


def _ostats(rows):
    return a.classify_outcomes(rows, TUNING)


def test_decide_keeps_a_merged_demotion_whose_evidence_aged_out():
    tuning = {**TUNING, 'outcome_demoted': ['dns-blocking']}
    ostats = _ostats([_out('dns-blocking', engaged=False)])       # 1 row: below min_samples
    actions = a.decide(STATS_QUIET, tuning, ostats)
    assert actions['outcome_demoted'] == ['dns-blocking']          # kept, never auto-removed
    assert actions['aged_out'] == ['dns-blocking']
    assert any("undemoting 'dns-blocking'" in s for s in actions['suggestions'])


def test_decide_unions_fresh_demotions_with_the_current_set():
    tuning = {**TUNING, 'outcome_demoted': ['dns-blocking']}
    ostats = _ostats([_out('ticket-x', engaged=False)] * 3)
    actions = a.decide(STATS_QUIET, tuning, ostats)
    assert actions['outcome_demoted'] == ['dns-blocking', 'ticket-x']
    assert actions['aged_out'] == ['dns-blocking']
    assert 'ticket-x' in actions['outcome_reasons']


def test_decide_no_aged_out_when_evidence_still_present():
    tuning = {**TUNING, 'outcome_demoted': ['dns-blocking']}
    ostats = _ostats([_out('dns-blocking', engaged=False)] * 3)
    actions = a.decide(STATS_QUIET, tuning, ostats)
    assert actions['aged_out'] == [] and actions['outcome_demoted'] == ['dns-blocking']
    assert not any('undemoting' in s for s in actions['suggestions'])


DEMOTED_SEED = SEED + "outcome_demoted:\n  - dns-blocking\n"


def test_apply_actions_reports_only_additions_to_outcome_demoted():
    text, changes = a.apply_actions(
        {'add_block': [], 'outcome_demoted': ['dns-blocking', 'ticket-x'], 'new_high': None},
        DEMOTED_SEED)
    assert changes == ['outcome_demoted += ticket-x']
    parsed = a.mi._parse_tuning_yaml(text)
    assert parsed['outcome_demoted'] == ['dns-blocking', 'ticket-x']


def test_apply_actions_is_a_noop_when_the_set_is_unchanged():
    text, changes = a.apply_actions(
        {'add_block': [], 'outcome_demoted': ['dns-blocking'], 'new_high': None}, DEMOTED_SEED)
    assert changes == [] and text == DEMOTED_SEED


def test_run_with_suggestions_only_cuts_no_branch(tmp_path, monkeypatch):
    # A standing "consider undemoting" suggestion (ruling (a)) must not produce a
    # branch: there is no config change to adjudicate. Reported, not committed.
    log = tmp_path / 'mem.jsonl'; log.write_text('')
    out = tmp_path / 'out.jsonl'; out.write_text('')
    tun = tmp_path / 'memory_tuning.yaml'
    tun.write_text(SEED + "outcome_demoted:\n  - dns-blocking\n"
                   "auto_tune:\n  enabled: true\n  window_days: 14\n  min_samples: 3\n"
                   "  cross_project_min: 3\n  threshold_step: 0.01\n  threshold_ceiling: 0.85\n"
                   "  false_high_rate_trigger: 0.3\n")
    monkeypatch.setattr(a, 'LOG_PATH', log)
    monkeypatch.setattr(a, 'OUTCOME_PATH', out)
    monkeypatch.setattr(a, 'TUNING_PATH', tun)
    monkeypatch.setattr(a.mi, 'TUNING_PATH', tun)
    def never(*_, **__): raise AssertionError('_commit_to_branch must not run')
    monkeypatch.setattr(a, '_commit_to_branch', never)
    monkeypatch.setattr(a, '_tracked_tree_dirty', lambda: False)
    res = a.run(now=dt.datetime(2026, 9, 21), dry_run=False)
    assert res['changed'] is False and res['changes'] == []
    assert any("undemoting 'dns-blocking'" in s for s in res['suggestions'])
    assert res['branch'] is None and 'gate' not in res


def test_main_reports_standing_suggestions_as_no_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(a, 'run', lambda now, dry_run: {
        'changed': False, 'changes': [], 'branch': None,
        'suggestions': ["consider undemoting 'dns-blocking' — evidence aged out"]})
    monkeypatch.setattr('sys.argv', ['audit_hooks.py'])
    a.main()
    out = capsys.readouterr().out.strip()
    assert out.startswith('audit_hooks: no action')          # run_pipeline: no Telegram
    assert "undemoting 'dns-blocking'" in out                  # but the log carries it
