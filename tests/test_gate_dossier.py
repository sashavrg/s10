"""Tests for scripts/gate_dossier.py — the ej9 blind gate-labeling dossier
generator (A4 of docs/superpowers/specs/2026-07-20-ej9-gate-read-addendum.md).

Everything here runs on synthetic data: the generator must never be exercised
against real gate rows from tests (blindness discipline).
"""
import json

import pytest

import engagement_judge as ej
import gate_dossier as gd


def _row(sid, ts, slug, tier='high', **extra):
    return dict({'session_id': sid, 'ts': ts, 'injected': slug, 'tier': tier}, **extra)


def _inj(sid, ts, slug, head, facts):
    return {'session_id': sid, 'ts': ts, 'tier': 'high', 'injected': slug,
            'prompt_head': head, 'injected_facts': facts, 'score': 0.9}


def _u(text):
    return {'role': 'user', 'text': text}


def _a(text):
    return {'role': 'assistant', 'text': text}


# --------------------------------------------------------------- selection

def test_select_gate_rows_filters_dedupes_and_sorts():
    v1 = [_row('s1', 't1', 'slug-a'), _row('s1', 't1', 'slug-a'),   # in-file dupe
          _row('s2', 't2', 'slug-b'),
          _row('', 't3', 'slug-c'),                                  # sessionless
          _row('s3', 't4', 'slug-d', tier='moderate'),               # wrong tier
          _row('s4', 't5', 'slug-e'),                                # calibration
          _row('s5', 't6', 'slug-f'),                                # no transcript
          _row('s6', 't7', 'slug-g')]                                # no inj row
    v2 = [_row('s2', 't2', 'slug-b'),                                # cross-file dupe
          _row('s0', 't0', 'slug-z')]                                # v2-only
    cal = {('s4', 't5', 'slug-e')}
    inj_by = {(s, t): _inj(s, t, 'x', 'h', ['f'])
              for s, t in (('s0', 't0'), ('s1', 't1'), ('s2', 't2'), ('s5', 't6'))}
    resolve = lambda sid: '/tx' if sid in ('s0', 's1', 's2', 's6') else None

    rows, stats = gd.select_gate_rows(v1, v2, cal, inj_by, resolve)

    keys = [(r['session_id'], r['ts'], r['injected']) for r in rows]
    assert keys == [('s0', 't0', 'slug-z'), ('s1', 't1', 'slug-a'),
                    ('s2', 't2', 'slug-b')]                          # ts-ascending
    assert stats == {'counted': 5, 'judgeable': 3,
                     'no_transcript': 1, 'no_injection_row': 1}


# --------------------------------------------------- labeler-side evidence

def test_labeler_snippets_wider_than_judges():
    pad = 'x' * 800
    turns = [_u('please service the zeppelinfleet today'), _a('on it.'),
             _a(pad + ' zeppelinfleet ' + pad)]
    inj = _inj('s1', 't1', 'zeppelinfleet-ops',
               'please service the zeppelinfleet today', ['zeppelinfleet fact'])
    ev = gd.build_labeler_evidence(inj, turns)
    assert ev['anchored'] is True
    assert len(ev['later_snippets']) == 1
    assert len(ev['later_snippets'][0]) > 450          # judge width is ~300


def test_labeler_reply_longer_than_judges():
    turns = [_u('kick the zeppelinfleet job'), _a('z' * 4000)]
    inj = _inj('s1', 't1', 'zeppelinfleet-ops', 'kick the zeppelinfleet job', ['f'])
    ev = gd.build_labeler_evidence(inj, turns)
    assert len(ev['assistant_reply']) == gd.REPLY_CAP
    assert gd.REPLY_CAP > 1400                          # judge caps at 1400


def test_labeler_snippet_cap_is_twelve():
    turns = [_u('kick the zeppelinfleet job'), _a('done')]
    turns += [_a(f'pass {i}: zeppelinfleet adjusted') for i in range(14)]
    inj = _inj('s1', 't1', 'zeppelinfleet-ops', 'kick the zeppelinfleet job', ['f'])
    ev = gd.build_labeler_evidence(inj, turns)
    assert len(ev['later_snippets']) == gd.MAX_SNIPPETS == 12


# ------------------------------------------------------------------ render

def _entry(slug='config-widgets', sid='sess-abc', ts='2026-07-04T10:00:00'):
    row = _row(sid, ts, slug,
               engaged_in_assistant=True, engaged_matched=['widget'],
               judge_rationale='R2 because instances count', score=0.95)
    ev = {'tier': 'high', 'topic_slug': slug,
          'topic_identity': ['fact one about widgets', 'fact two'],
          'trigger_prompt': 'please fix the widget layout',
          'assistant_reply': 'Fixing the widget layout now.',
          'later_snippets': ['adjusted the widget grid spacing'],
          'anchored': True}
    return {'row': row, 'evidence': ev, 'transcript': f'/arch/{sid}.jsonl'}


def test_render_contains_evidence_and_blank_label_fields():
    e = _entry()
    md = gd.render_dossier([e])
    for needle in ('config-widgets', '[HIGH]', 'sess-abc', '2026-07-04T10:00:00',
                   '/arch/sess-abc.jsonl', 'fact one about widgets',
                   'please fix the widget layout', 'Fixing the widget layout now.',
                   'adjusted the widget grid spacing',
                   'compiled/topics/config-widgets.md',
                   '- identity_matches_subject: ?',
                   '- label_engaged: ?', '- label_corrected: ?'):
        assert needle in md
    assert md == gd.render_dossier([e])                 # deterministic


def test_render_never_leaks_scorer_or_judge_fields():
    md = gd.render_dossier([_entry()]).lower()
    for forbidden in ('engaged_in_assistant', 'engaged_matched', 'judge_rationale',
                      'judge_evidence', 'r2 because', '0.95', 'proxy said'):
        assert forbidden not in md


def test_render_states_evidence_is_fuller_than_judges():
    # Round-1's "exactly what the judge sees" line must NOT carry over: the
    # labeler-side evidence is deliberately fuller (round-2 freeze record).
    md = gd.render_dossier([_entry()])
    assert 'exactly what the judge sees' not in md
    assert 'FULLER' in md


# ------------------------------------------------------------------- parse

def test_parse_roundtrips_filled_labels():
    md = gd.render_dossier([_entry()])
    md = md.replace('- identity_matches_subject: ?', '- identity_matches_subject: yes')
    md = md.replace('- label_engaged: ?', '- label_engaged: no')
    md = md.replace('- label_corrected: ?', '- label_corrected: no')
    labeled, incomplete = gd.parse_dossier(md)
    assert incomplete == []
    assert labeled == [{'session_id': 'sess-abc', 'ts': '2026-07-04T10:00:00',
                        'injected': 'config-widgets', 'tier': 'high',
                        'identity_matches_subject': True,
                        'label_engaged': False, 'label_corrected': False}]


def test_parse_reports_incomplete_rows_by_ordinal():
    e1, e2 = _entry(sid='sess-one'), _entry(sid='sess-two', ts='2026-07-05T10:00:00')
    md = gd.render_dossier([e1, e2])
    for field in ('identity_matches_subject', 'label_engaged', 'label_corrected'):
        md = md.replace(f'- {field}: ?', f'- {field}: yes', 1)   # fill row 1 only
    labeled, incomplete = gd.parse_dossier(md)
    assert [r['session_id'] for r in labeled] == ['sess-one']
    assert incomplete == [2]


# ----------------------------------------------------- verdict (A3 amended)

@pytest.mark.parametrize('p,r,want', [
    (0.80, 0.70, 'pass'),
    (0.79, 0.90, 'expand'),
    (0.70, 0.70, 'expand'),
    (0.90, 0.60, 'expand'),
    (0.699, 0.90, 'fail'),          # below precision band floor
    (0.90, 0.599, 'fail'),          # below recall band floor (closed silent case)
    (0.75, 0.59, 'fail'),           # in-band P cannot rescue below-floor R
])
def test_gate_verdict_stage1(p, r, want):
    assert gd.gate_verdict(p, r, stage=1) == want


@pytest.mark.parametrize('p,r,want', [
    (0.82, 0.72, 'pass'),
    (0.81, 0.90, 'fail'),           # stage-2 bars are 0.82/0.72, no expand branch
    (0.90, 0.71, 'fail'),
])
def test_gate_verdict_stage2(p, r, want):
    assert gd.gate_verdict(p, r, stage=2) == want


# ----------------------------------------------------------- make() end-to-end

def test_make_writes_dossier_from_synthetic_files(tmp_path, monkeypatch):
    head1, head2 = 'work on the alpha thing', 'work on the beta thing'
    v1 = [_row('sA', '2026-07-04T10:00:00', 'alpha-topic'),
          _row('sB', '2026-07-05T10:00:00', 'beta-topic'),
          _row('sB', '2026-07-05T11:00:00', 'gamma-topic', tier='moderate'),
          _row('sC', '2026-07-06T10:00:00', 'cal-topic')]
    inj = [_inj('sA', '2026-07-04T10:00:00', 'alpha-topic', head1, ['alpha fact']),
           _inj('sB', '2026-07-05T10:00:00', 'beta-topic', head2, ['beta fact']),
           _inj('sC', '2026-07-06T10:00:00', 'cal-topic', 'h', ['f'])]
    cal = [_row('sC', '2026-07-06T10:00:00', 'cal-topic')]

    (tmp_path / 'projects' / 'proj').mkdir(parents=True)
    for sid, head in (('sA', head1), ('sB', head2)):
        turns = [{'message': {'role': 'user', 'content': head}},
                 {'message': {'role': 'assistant', 'content': f'doing {head} now'}}]
        (tmp_path / 'projects' / 'proj' / f'{sid}.jsonl').write_text(
            '\n'.join(json.dumps(x) for x in turns))

    def jl(name, rows):
        p = tmp_path / name
        p.write_text('\n'.join(json.dumps(r) for r in rows))
        return p

    monkeypatch.setattr(gd, 'V1_PATH', jl('v1.jsonl', v1))
    monkeypatch.setattr(gd, 'V2_PATH', tmp_path / 'absent.jsonl')
    monkeypatch.setattr(gd, 'CAL_PATH', jl('cal.jsonl', cal))
    monkeypatch.setattr(gd, 'INJECTION_LOG_PATH', jl('inj.jsonl', inj))
    monkeypatch.setattr(gd, 'OUT_PATH', tmp_path / 'out' / 'dossier.md')
    monkeypatch.setattr(ej, 'TRANSCRIPT_ROOT', tmp_path / 'projects')
    monkeypatch.setattr(ej, 'ARCHIVE_ROOT', tmp_path / 'no-archive')

    stats = gd.make()

    assert stats == {'counted': 2, 'judgeable': 2,
                     'no_transcript': 0, 'no_injection_row': 0}
    md = (tmp_path / 'out' / 'dossier.md').read_text()
    assert md.count('## Row') == 2
    assert 'alpha fact' in md and 'beta fact' in md
    assert 'gamma-topic' not in md and 'cal-topic' not in md
