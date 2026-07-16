"""shadow_retrieval logs what alternative scorers WOULD inject on real traffic,
injecting nothing. The rerank pass answers the precision question the golden set
can't: when embedding wants to HIGH-inject, does the LLM judge CONFIRM it, SWITCH
to a sibling, or REJECT it (the over-firing we saw in shadow)? build_record is the
pure verdict logic, unit-tested here without Ollama or IO."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import shadow_retrieval as sr  # noqa: E402


def _m(slug, tier, score):
    return {'slug': slug, 'tier': tier, 'score': score}


def _emb(matches):
    return {'mode': 'embedding',
            'top_tier': matches[0]['tier'] if matches else 'none',
            'matches': matches}


def _rr(matches):
    return {'mode': 'rerank', 'matches': matches}


JOB = {'prompt': 'how do I connect to the db', 'project': 'p', 'session_id': 's',
       'lexical_top_tier': 'none', 'lexical_injected': None, 'lexical_high': []}


def test_verdict_confirm_when_judge_keeps_embedding_top():
    emb = _emb([_m('a', 'high', 0.62), _m('b', 'moderate', 0.55)])
    rr = _rr([_m('a', 'high', 0.62), _m('b', 'moderate', 0.55)])
    rec = sr.build_record(JOB, emb, rr, 'T')
    assert rec['rerank_verdict'] == 'confirm'
    assert rec['rerank_top'] == 'a'
    assert rec['embedding_gap'] == 0.07


def test_verdict_switch_when_judge_picks_a_sibling():
    emb = _emb([_m('a', 'high', 0.62), _m('b', 'moderate', 0.60)])
    rr = _rr([_m('b', 'high', 0.60), _m('a', 'moderate', 0.62)])
    rec = sr.build_record(JOB, emb, rr, 'T')
    assert rec['rerank_verdict'] == 'switch'
    assert rec['rerank_top'] == 'b'


def test_verdict_reject_when_judge_returns_no_high():
    emb = _emb([_m('a', 'high', 0.62), _m('b', 'moderate', 0.55)])
    rr = _rr([_m('a', 'moderate', 0.62), _m('b', 'moderate', 0.55)])  # judge said none
    rec = sr.build_record(JOB, emb, rr, 'T')
    assert rec['rerank_verdict'] == 'reject'
    assert rec['rerank_top'] is None


def test_no_embedding_high_means_nothing_to_judge():
    emb = _emb([_m('a', 'moderate', 0.54)])
    rr = _rr([_m('a', 'moderate', 0.54)])
    rec = sr.build_record(JOB, emb, rr, 'T')
    assert rec['rerank_verdict'] == 'no_embedding_high'


def test_rerank_fields_absent_when_pass_not_run():
    emb = _emb([_m('a', 'high', 0.62)])
    rec = sr.build_record(JOB, emb, None, 'T')          # rerank pass disabled
    assert 'rerank_verdict' not in rec
    assert rec['embedding_high'] == ['a']               # embedding fields still present


def test_rerank_fields_absent_when_pass_fell_back_to_lexical():
    emb = _emb([_m('a', 'high', 0.62)])
    rr = {'mode': 'lexical', 'matches': []}             # ollama/cache unavailable
    rec = sr.build_record(JOB, emb, rr, 'T')
    assert 'rerank_verdict' not in rec


def test_record_preserves_existing_embedding_schema():
    # shadow_report.py reads these keys; they must not regress.
    emb = _emb([_m('a', 'high', 0.62)])
    rec = sr.build_record(JOB, emb, None, 'T')
    for k in ('ts', 'session_id', 'project', 'prompt_head', 'lexical_high',
              'embedding_top', 'embedding_high', 'embedding_only_high', 'agree_injected'):
        assert k in rec
