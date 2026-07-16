import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import engagement_judge as ej

EV = {
    'tier': 'high', 'topic_slug': 'ecs-rollout-polling',
    'topic_identity': ['poll rolloutState until COMPLETED'],
    'trigger_prompt': 'deploy it', 'assistant_reply': 'polling the ecs rollout now',
    'later_snippets': [], 'n_snippets': 0, 'nomination_empty': True, 'anchored': True,
}


def test_parse_exact_yes_only():
    assert ej.parse_verdict('ENGAGED: yes\nWHY: used the facts')['engaged'] is True
    assert ej.parse_verdict('engaged: YES\nWHY: x')['engaged'] is True   # case-insensitive
    assert ej.parse_verdict('ENGAGED: no\nWHY: overlap only')['engaged'] is False
    # ANY non-exact-yes value on the line biases to not-engaged (spec mechanical boundary)
    assert ej.parse_verdict('ENGAGED: maybe\nWHY: unclear')['engaged'] is False
    assert ej.parse_verdict('ENGAGED: yes.\nWHY: x')['engaged'] is False


def test_parse_no_engaged_line_is_none():
    assert ej.parse_verdict('the assistant clearly engaged') is None
    assert ej.parse_verdict('') is None
    assert ej.parse_verdict(None) is None


def test_parse_rationale_capped():
    out = ej.parse_verdict('ENGAGED: yes\nWHY: ' + 'x' * 500)
    assert len(out['rationale']) <= 200


def test_prompt_carries_null_bias_clause_and_contract():
    p = ej._build_prompt(EV)
    assert ej.NULL_BIAS_CLAUSE in p
    assert 'ENGAGED: yes|no' in p
    assert 'ecs-rollout-polling' in p


def test_prompt_moderate_framing_differs():
    high_p = ej._build_prompt(EV)
    mod_p = ej._build_prompt(dict(EV, tier='moderate'))
    assert 'INJECTED into' in high_p and 'NOTHING was injected' not in high_p
    assert 'NOTHING was injected' in mod_p and 'INJECTED into' not in mod_p


def test_judge_engagement_fails_open_to_none():
    def boom(prompt):
        raise RuntimeError('ollama down')
    assert ej.judge_engagement(EV, generate_fn=boom) is None
    assert ej.judge_engagement(EV, generate_fn=lambda p: 'garbage') is None


def test_judge_engagement_happy_path_uses_generate_fn():
    calls = []
    def fake(prompt):
        calls.append(prompt)
        return 'ENGAGED: yes\nWHY: acted on the injected facts'
    out = ej.judge_engagement(EV, generate_fn=fake)
    assert out == {'engaged': True, 'rationale': 'acted on the injected facts'}
    assert len(calls) == 1


def _turns():
    return [
        {'role': 'user', 'text': 'please deploy the widget and poll the rollout'},
        {'role': 'assistant', 'text': 'polling the ecs rollout state until completed'},
        {'role': 'user', 'text': 'thanks'},
        {'role': 'assistant', 'text': 'done. also the rollout finished cleanly'},
        {'role': 'assistant', 'text': 'unrelated closing remark about lunch'},
    ]


def test_build_evidence_anchors_and_takes_next_reply():
    inj = {'tier': 'high', 'injected': 'ecs-rollout-polling',
           'prompt_head': 'please deploy the widget and poll',
           'injected_facts': ['poll rolloutState until COMPLETED']}
    ev = ej.build_evidence(inj, _turns(), None)
    assert ev['anchored'] is True
    assert ev['trigger_prompt'].startswith('please deploy')
    assert ev['assistant_reply'].startswith('polling the ecs')
    assert ev['topic_identity'] == ['poll rolloutState until COMPLETED']


def test_nomination_is_recall_biased_single_hit_nominates():
    inj = {'tier': 'high', 'injected': 'ecs-rollout-polling',
           'prompt_head': 'please deploy the widget and poll', 'injected_facts': []}
    ev = ej.build_evidence(inj, _turns(), None)
    # 'rollout' alone (1 token) nominates the later turn — v2's quorum-2 must NOT gate here
    assert any('finished cleanly' in s for s in ev['later_snippets'])
    assert ev['nomination_empty'] is False
    assert ev['n_snippets'] == len(ev['later_snippets']) <= 8


def test_nomination_empty_flagged():
    inj = {'tier': 'high', 'injected': 'zz-qq-xx',
           'prompt_head': 'please deploy the widget and poll', 'injected_facts': []}
    ev = ej.build_evidence(inj, _turns(), None)
    assert ev['nomination_empty'] is True and ev['later_snippets'] == []


def test_moderate_identity_from_topic_page():
    inj = {'tier': 'moderate', 'injected': 'design-tokens', 'prompt_head': 'thanks'}
    page = {'overview': 'Design tokens for the widget system.', 'points': ['token A', 'token B']}
    ev = ej.build_evidence(inj, _turns(), page)
    assert ev['topic_identity'][0].startswith('Design tokens')
    assert 'token A' in ev['topic_identity']


def test_unanchored_falls_back_gracefully():
    inj = {'tier': 'high', 'injected': 'ecs-rollout-polling',
           'prompt_head': 'THIS PROMPT NEVER HAPPENED', 'injected_facts': ['f']}
    ev = ej.build_evidence(inj, _turns(), None)
    assert ev['anchored'] is False
    assert ev['trigger_prompt'] == '' and ev['assistant_reply'] == ''


def test_resolve_transcript_archive_fallback(tmp_path, monkeypatch):
    live = tmp_path / 'live'; arch = tmp_path / 'arch'
    (arch / 'proj').mkdir(parents=True)
    (arch / 'proj' / 'sess-1.jsonl').write_text('{}')
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', live)
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', arch)
    p = ej.resolve_transcript('sess-1')
    assert p is not None and p.name == 'sess-1.jsonl'
    assert ej.resolve_transcript('sess-missing') is None


def test_resolve_transcript_prefers_live_over_archive(tmp_path, monkeypatch):
    # F3: same session id in both roots — live must win (checked first).
    live = tmp_path / 'live'; arch = tmp_path / 'arch'
    (live / 'proj').mkdir(parents=True)
    (arch / 'proj').mkdir(parents=True)
    (live / 'proj' / 'sess-2.jsonl').write_text('{"from": "live"}')
    (arch / 'proj' / 'sess-2.jsonl').write_text('{"from": "archive"}')
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', live)
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', arch)
    p = ej.resolve_transcript('sess-2')
    assert p == live / 'proj' / 'sess-2.jsonl'


def test_unanchored_scans_snippets_from_turn_zero():
    # F2: prompt_head never matches -> unanchored -> scan starts at turns[0], not
    # ti+2, so it can reach turns[1] (the early assistant turn) which an anchored
    # scan (start = ti+2) would skip entirely.
    inj = {'tier': 'high', 'injected': 'ecs-rollout-polling',
           'prompt_head': 'THIS PROMPT NEVER HAPPENED',
           'injected_facts': ['poll rolloutState until COMPLETED']}
    ev = ej.build_evidence(inj, _turns(), None)
    assert ev['anchored'] is False
    assert ev['n_snippets'] >= 1
    assert any('polling the ecs' in s for s in ev['later_snippets'])


def test_snippet_centers_on_word_boundary_not_substring_match():
    # C1b: 'ecs' is also a substring of 'specs' — a plain str.find would center the
    # snippet on the wrong (unrelated) occurrence inside 'specs' instead of the
    # genuine word-boundary 'ecs' occurrence further along in the text.
    filler = 'lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod '
    text = ('check the specs are ready before we begin. ' + filler * 5
            + 'the ecs rollout finished successfully after testing today')
    inj = {'tier': 'high', 'injected': 'ecs-rollout-polling',
           'prompt_head': 'THIS PROMPT NEVER HAPPENED', 'injected_facts': []}
    turns = [{'role': 'assistant', 'text': text}]
    ev = ej.build_evidence(inj, turns, None)
    assert ev['n_snippets'] == 1
    snippet = ev['later_snippets'][0]
    assert 'rollout finished' in snippet
    assert 'specs are ready' not in snippet


def _topic_body(*, heading: str, overview: str, points: list[str]) -> str:
    lines = ['---', 'topic: t', '---', '', '# t', '', heading, overview, '',
              '## Key points']
    lines += [f'- {pt}' for pt in points]
    return '\n'.join(lines) + '\n'


def test_load_topic_page_h1_topic_overview(tmp_path, monkeypatch):
    # Existing (pre-fix) behavior: H1 "# Topic overview" heading.
    monkeypatch.setattr(ej, 'TOPICS_DIR', tmp_path)
    body = _topic_body(heading='# Topic overview', overview='H1 overview text.',
                        points=['point one', 'point two'])
    (tmp_path / 't.md').write_text(body)
    out = ej.load_topic_page('t')
    assert out['overview'] == 'H1 overview text.'
    assert out['points'] == ['point one', 'point two']


def test_load_topic_page_h2_topic_overview(tmp_path, monkeypatch):
    # F1: H2 "## Topic overview" heading must also be recognized.
    monkeypatch.setattr(ej, 'TOPICS_DIR', tmp_path)
    body = _topic_body(heading='## Topic overview', overview='H2 overview text.',
                        points=['alpha'])
    (tmp_path / 't.md').write_text(body)
    out = ej.load_topic_page('t')
    assert out['overview'] == 'H2 overview text.'
    assert out['points'] == ['alpha']


def test_load_topic_page_h2_summary_heading(tmp_path, monkeypatch):
    # F1: "## Summary" is one of the recognized overview-equivalent headings.
    monkeypatch.setattr(ej, 'TOPICS_DIR', tmp_path)
    body = _topic_body(heading='## Summary', overview='Summary body text.',
                        points=['beta'])
    (tmp_path / 't.md').write_text(body)
    out = ej.load_topic_page('t')
    assert out['overview'] == 'Summary body text.'
    assert out['points'] == ['beta']


def test_load_topic_page_h1_title_not_mistaken_for_overview(tmp_path, monkeypatch):
    # Regression guard: a document title like "# Widget Overview" must NOT be
    # treated as the overview section — only exact-normalized headings qualify.
    monkeypatch.setattr(ej, 'TOPICS_DIR', tmp_path)
    body = '\n'.join([
        '---', 'topic: t', '---', '',
        '# Widget Overview', '',
        '## Topic overview', 'The real overview.', '',
        '## Key points', '- only point',
    ]) + '\n'
    (tmp_path / 't.md').write_text(body)
    out = ej.load_topic_page('t')
    assert out['overview'] == 'The real overview.'
    assert out['points'] == ['only point']


def test_score_dev_math_and_pop_weighting():
    results = (
        [{'stratum': 'high', 'label_engaged': True, 'judged_engaged': True}] * 3
        + [{'stratum': 'high', 'label_engaged': False, 'judged_engaged': True}]      # 1 FP
        + [{'stratum': 'high', 'label_engaged': True, 'judged_engaged': False}]      # 1 FN
        + [{'stratum': 'agree_not', 'label_engaged': False, 'judged_engaged': False}] * 2
        + [{'stratum': 'agree_not', 'label_engaged': True, 'judged_engaged': None}]  # unjudged
    )
    s = ej.score_dev(results)
    hi = s['by_stratum']['high']
    assert (hi['tp'], hi['fp'], hi['fn']) == (3, 1, 1)
    assert abs(hi['precision'] - 0.75) < 1e-9 and abs(hi['recall'] - 0.75) < 1e-9
    assert s['fp_not_engaged'] == 1
    assert s['unjudged'] == 1
    assert s['pooled']['precision'] == 0.75          # only high has predictions
    assert 0 < s['pop_weighted']['precision'] <= 1


def test_score_dev_unjudged_rows_excluded_from_counts():
    s = ej.score_dev([{'stratum': 'high', 'label_engaged': True, 'judged_engaged': None}])
    assert s['by_stratum']['high']['tp'] == 0 and s['unjudged'] == 1
    assert s['by_stratum']['high']['precision'] is None


def test_rubric_constants_pinned_to_signed_rubric_file():
    # The implementation contract (rubric header + spec Round-2): the judge embeds a
    # VERBATIM copy; any rubric edit must fail this pin -> judge_version bump + fresh gate.
    rubric = (Path(__file__).resolve().parent.parent / 'docs' / 'engagement-rubric.md').read_text()
    assert ej.RUBRIC_TEXT in rubric
    assert ej.NULL_BIAS_CLAUSE in rubric
    assert ej.RUBRIC_IDENTITY_NOTE in rubric
    assert rubric.splitlines()[1].startswith('Status: SIGNED by operator')


def test_prompt_quotes_rubric_and_requires_rule_citation():
    p = ej._build_prompt(EV)
    assert 'R7 — Per-slug discipline' in p          # rubric body actually present
    assert "OPERATOR'S RUBRIC" in p
    assert 'naming the rule you applied' in p
