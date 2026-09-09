"""Tests for scripts/label_gate.py — the operator's terminal labeler for the
blind gate dossier.

Synthetic dossiers only (blindness discipline: tests never touch real gate rows).
The load-bearing property is that writing a label edits ONLY that row's three
field lines and leaves every other byte of the dossier untouched — the file is
the labeling session's state, and `gate_dossier.parse` reads it back.
"""
import pytest

import label_gate as lg


def _dossier(n_rows=2, mentions=True):
    out = ['# EJ9 ENGAGEMENT GATE — blind labeling dossier', '#', '# protocol…', '']
    for i in range(1, n_rows + 1):
        out += [
            f'## Row {i} — `slug-{i}`  [HIGH]',
            f'_session `sess-{i}` · ts 2026-07-0{i}T10:00:00 · transcript: /tx/{i}.jsonl_',
            f'_full topic page: compiled/topics/slug-{i}.md_',
            '',
            '**Injected facts (this is the identity you are rating):**',
            f'  - fact one for {i}',
            f'  - fact two for {i}',
            '',
            f'**Trigger prompt:** trigger text {i}',
            '',
            f'**Assistant reply:** reply text {i}',
        ]
        if mentions:
            out += ['**Later mentions:**', f'  · mention A {i}', f'  · mention B {i}']
        out += ['', '- identity_matches_subject: ?', '- label_engaged: ?',
                '- label_corrected: ?', '', '---', '']
    return '\n'.join(out)


# ------------------------------------------------------------------ parsing

def test_parse_rows_extracts_every_evidence_section():
    rows = lg.parse_rows(_dossier())

    assert [r['ordinal'] for r in rows] == [1, 2]
    r1 = rows[0]
    assert r1['slug'] == 'slug-1'
    assert r1['session_id'] == 'sess-1'
    assert r1['ts'] == '2026-07-01T10:00:00'
    assert r1['transcript'] == '/tx/1.jsonl'
    assert r1['topic_page'] == 'compiled/topics/slug-1.md'
    assert r1['facts'] == ['fact one for 1', 'fact two for 1']
    assert r1['trigger'] == 'trigger text 1'
    assert r1['reply'] == 'reply text 1'
    assert r1['mentions'] == ['mention A 1', 'mention B 1']
    assert r1['labels'] == {'identity_matches_subject': None,
                            'label_engaged': None, 'label_corrected': None}


def test_parse_rows_handles_a_row_without_later_mentions():
    rows = lg.parse_rows(_dossier(n_rows=1, mentions=False))
    assert rows[0]['mentions'] == []
    assert rows[0]['reply'] == 'reply text 1'


def test_parse_rows_reads_back_already_filled_labels():
    md = lg.set_labels(_dossier(), 2, {'label_engaged': 'yes'})
    rows = lg.parse_rows(md)
    assert rows[1]['labels'] == {'identity_matches_subject': None,
                                 'label_engaged': True, 'label_corrected': None}


def test_pending_returns_rows_missing_any_field():
    md = _dossier(n_rows=2)
    md = lg.set_labels(md, 1, {'identity_matches_subject': 'yes',
                               'label_engaged': 'no', 'label_corrected': 'no'})
    md = lg.set_labels(md, 2, {'identity_matches_subject': 'yes'})  # partial
    assert [r['ordinal'] for r in lg.pending(lg.parse_rows(md))] == [2]


# ------------------------------------------------------------------ writing

def test_set_labels_touches_only_that_rows_field_lines():
    before = _dossier()
    after = lg.set_labels(before, 2, {'identity_matches_subject': 'yes',
                                      'label_engaged': 'no',
                                      'label_corrected': 'no'})
    b, a = before.splitlines(), after.splitlines()
    assert len(b) == len(a)
    changed = [i for i in range(len(b)) if b[i] != a[i]]
    assert [a[i] for i in changed] == ['- identity_matches_subject: yes',
                                       '- label_engaged: no',
                                       '- label_corrected: no']
    # …and they are row 2's, not row 1's
    assert all(i > a.index('## Row 2 — `slug-2`  [HIGH]') for i in changed)


def test_set_labels_partial_leaves_the_others_unanswered():
    after = lg.set_labels(_dossier(), 1, {'label_engaged': 'yes'})
    rows = lg.parse_rows(after)
    assert rows[0]['labels']['label_engaged'] is True
    assert rows[0]['labels']['identity_matches_subject'] is None


def test_set_labels_overwrites_a_previous_answer():
    md = lg.set_labels(_dossier(), 1, {'label_engaged': 'yes'})
    md = lg.set_labels(md, 1, {'label_engaged': 'no'})
    assert lg.parse_rows(md)[0]['labels']['label_engaged'] is False


def test_set_labels_accepts_tui_keystrokes_and_writes_canonical_tokens():
    """The TUI answers with single keys; the FILE must always hold yes/no —
    `gate_dossier.parse_dossier` accepts nothing else, so writing a bare 'y'
    would silently leave the row unlabeled at parse time."""
    import gate_dossier as gd
    md = lg.set_labels(_dossier(n_rows=1), 1, {'identity_matches_subject': 'y',
                                               'label_engaged': 'n',
                                               'label_corrected': 'n'})
    assert '- identity_matches_subject: yes' in md
    assert '- label_engaged: no' in md
    labeled, incomplete = gd.parse_dossier(md)
    assert incomplete == []
    assert labeled[0]['identity_matches_subject'] is True
    assert labeled[0]['label_engaged'] is False


def test_set_labels_rejects_unknown_ordinal():
    with pytest.raises(KeyError):
        lg.set_labels(_dossier(), 99, {'label_engaged': 'yes'})


def test_set_labels_rejects_a_non_yes_no_value():
    with pytest.raises(ValueError):
        lg.set_labels(_dossier(), 1, {'label_engaged': 'maybe'})


def test_round_trips_through_gate_dossier_parse():
    """What this writes must be what the gate read reads."""
    import gate_dossier as gd
    md = _dossier(n_rows=1)
    md = lg.set_labels(md, 1, {'identity_matches_subject': 'yes',
                               'label_engaged': 'no', 'label_corrected': 'no'})
    labeled, incomplete = gd.parse_dossier(md)
    assert incomplete == []
    assert labeled == [{'session_id': 'sess-1', 'ts': '2026-07-01T10:00:00',
                        'injected': 'slug-1', 'tier': 'high',
                        'identity_matches_subject': True,
                        'label_engaged': False, 'label_corrected': False}]


# --------------------------------------------------------------------- loop
# The prompt loop is where the wiring bugs live (the answer token the prompt
# returns must be what the writer accepts, and navigation must actually
# navigate), so drive label_session with a scripted _ask instead of a terminal.

def _run(tmp_path, md, answers, revisit_all=False, start=None):
    p = tmp_path / 'dossier.md'
    p.write_text(md)
    script = iter(answers)
    return p, script, lambda: lg.label_session(p, start, revisit_all), script


def test_loop_writes_the_answers_it_is_given(tmp_path, monkeypatch):
    p, script, run, _ = _run(tmp_path, _dossier(n_rows=1), ['y', 'n', 'n'])
    monkeypatch.setattr(lg, '_ask', lambda *a, **k: next(script))
    run()
    assert lg.parse_rows(p.read_text())[0]['labels'] == {
        'identity_matches_subject': True, 'label_engaged': False,
        'label_corrected': False}


def test_back_key_rewinds_and_re_answers_the_previous_field(tmp_path, monkeypatch):
    # y, then 'b' at question 2 → question 1 asked again, answered n
    p, script, run, _ = _run(tmp_path, _dossier(n_rows=1), ['y', 'b', 'n', 'n', 'n'])
    monkeypatch.setattr(lg, '_ask', lambda *a, **k: next(script))
    run()
    assert lg.parse_rows(p.read_text())[0]['labels'] == {
        'identity_matches_subject': False, 'label_engaged': False,
        'label_corrected': False}


def test_resume_does_not_re_ask_fields_already_on_disk(tmp_path, monkeypatch):
    md = lg.set_labels(_dossier(n_rows=1), 1, {'identity_matches_subject': 'yes'})
    p, script, run, remaining = _run(tmp_path, md, ['n', 'n'])
    monkeypatch.setattr(lg, '_ask', lambda *a, **k: next(script))
    run()
    assert next(remaining, 'EXHAUSTED') == 'EXHAUSTED'   # exactly two asked
    assert lg.parse_rows(p.read_text())[0]['labels'] == {
        'identity_matches_subject': True, 'label_engaged': False,
        'label_corrected': False}


def test_skip_defers_the_row_and_comes_back_to_it(tmp_path, monkeypatch):
    # row 1 skipped → row 2 labeled → row 1 comes back and is labeled
    p, script, run, _ = _run(tmp_path, _dossier(n_rows=2),
                             ['s', 'y', 'y', 'n', 'n', 'n', 'n'])
    monkeypatch.setattr(lg, '_ask', lambda *a, **k: next(script))
    run()
    rows = lg.parse_rows(p.read_text())
    assert rows[1]['labels'] == {'identity_matches_subject': True,
                                 'label_engaged': True, 'label_corrected': False}
    assert rows[0]['labels'] == {'identity_matches_subject': False,
                                 'label_engaged': False, 'label_corrected': False}


def test_quit_stops_immediately_and_keeps_prior_answers(tmp_path, monkeypatch):
    p, script, run, remaining = _run(tmp_path, _dossier(n_rows=2), ['y', 'q', 'n'])
    monkeypatch.setattr(lg, '_ask', lambda *a, **k: next(script))
    run()
    assert next(remaining) == 'n'          # the third answer was never consumed
    rows = lg.parse_rows(p.read_text())
    assert rows[0]['labels']['identity_matches_subject'] is True
    assert rows[0]['labels']['label_engaged'] is None
    assert all(v is None for v in rows[1]['labels'].values())


# ------------------------------------------------------------------ display

def test_wrap_block_indents_and_wraps_without_losing_words():
    out = lg.wrap_block('alpha beta gamma delta', width=22, indent='  ')
    assert out == ['  alpha beta gamma', '  delta']


def test_wrap_block_never_wraps_narrower_than_the_floor():
    # a tiny/undetectable terminal must not shred the evidence one word per line
    assert lg.wrap_block('alpha beta gamma delta', width=4, indent='  ') == \
        ['  alpha beta gamma', '  delta']


def test_wrap_block_of_empty_text_is_empty():
    assert lg.wrap_block('', width=40, indent='  ') == []


def test_head_lines_marks_truncation_only_when_it_truncates():
    long_lines = ['l%d' % i for i in range(10)]
    head, cut = lg.head_lines(long_lines, 3)
    assert head == ['l0', 'l1', 'l2'] and cut == 7
    head, cut = lg.head_lines(['a', 'b'], 5)
    assert head == ['a', 'b'] and cut == 0
