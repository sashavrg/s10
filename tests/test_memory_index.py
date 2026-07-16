"""Parser fixes in memory_index.py:
- extract_section now recognizes ### and **bold** headers (was '## '-only, which
  silently dropped 6 topics whose 'Key points' was emitted at ### or in bold).
- split_frontmatter strips the quotes yaml.safe_dump adds to ':'-bearing scalars
  (e.g. compiled_at), so the value is ISO-parseable — the prerequisite for any
  freshness/staleness gate.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import memory_index as mi  # noqa: E402


def test_extract_section_recognizes_h3_header():
    body = "### Key points\n- fact one\n- fact two\n\n## Other\n- nope\n"
    assert mi.extract_section(body, 'Key points') == ['fact one', 'fact two']


def test_extract_section_recognizes_bold_header():
    body = "**Key points**\n- bold fact\n\n**Other:**\n- nope\n"
    assert mi.extract_section(body, 'Key points') == ['bold fact']


def test_extract_section_recognizes_unicode_bullets():
    # weak local models emit '•' instead of '-'/'*' — these were silently dropped
    body = "## Key Points\n• first fact\n• second fact\n\n## Evidence\n• nope\n"
    assert mi.extract_section(body, 'Key points') == ['first fact', 'second fact']


def test_extract_section_recognizes_numbered_lists():
    body = "## Key points\n1. **Token**: corrected color\n2) added new token\n\n## Evidence\n1. nope\n"
    assert mi.extract_section(body, 'Key points') == ['**Token**: corrected color', 'added new token']


def test_extract_section_still_handles_h2_and_stops_at_next_section():
    body = "## Key points\n- classic\n\n## Open questions\n- not this\n"
    assert mi.extract_section(body, 'Key points') == ['classic']


def test_extract_section_empty_when_absent():
    assert mi.extract_section("## Summary\n- x\n", 'Key points') == []


## --- ambiguity-gated rerank (KB_RETRIEVAL=rerank) ----------------------------
# The gate invokes the costly LLM judge only when it can change the outcome:
# a plausibly-relevant top candidate (>= floor) AND the top-2 too close for
# embedding order to be decisive. When one candidate dominates, trust the cheap
# embedding rank-cap. We control the scenario by making cosine == the stored
# "vector", so each entry's similarity is whatever we pass in.

def _rerank_scenario(monkeypatch, scores):
    entries = [{
        'slug': f't{i}', 'display': f'Topic {i}', 'projects': [],
        'key_points': [f'point {i}'], 'path': f'/tmp/t{i}.md',
        'match_tokens': [], 'slug_tokens': [],
    } for i in range(len(scores))]
    monkeypatch.setattr(mi, 'load_index', lambda: {'entries': entries})
    import embedding_rescorer as er
    vecs = {f't{i}': sc for i, sc in enumerate(scores)}
    monkeypatch.setattr(er, 'load_topic_vectors', lambda: vecs)
    monkeypatch.setattr(er, 'embed', lambda q: [1.0])
    monkeypatch.setattr(er, 'cosine', lambda a, b: b)   # the "vector" IS the cosine
    monkeypatch.setenv('KB_RETRIEVAL', 'rerank')


def _spy_reranker(monkeypatch, winner):
    import reranker
    calls = []
    monkeypatch.setattr(reranker, 'rerank', lambda q, c: (calls.append(c), winner)[1])
    return calls


def test_rerank_default_reranks_every_would_inject_turn(monkeypatch):
    # DEFAULT (no KB_RERANK_GAP): rerank fires even when top-1 dominates, because
    # the gate was measured to hurt (50% -> 38%). This is the corrected default.
    _rerank_scenario(monkeypatch, [0.70, 0.62, 0.45])
    calls = _spy_reranker(monkeypatch, winner=1)
    res = mi.retrieve('q')
    assert res['mode'] == 'rerank'
    assert len(calls) == 1                           # judge invoked despite dominance
    assert res['matches'][0]['slug'] == 't1'         # judge's pick promoted
    assert res['matches'][0]['tier'] == 'high'       # ...and injected (0.62 >= 0.58 floor)


def test_rerank_winner_below_high_floor_is_not_injected(monkeypatch):
    # THE fix for real-traffic over-firing: the judge picks WHICH candidate, but
    # embedding's confidence gates WHETHER it injects. A sub-floor winner (the judge
    # grabbing a weak topic on a conversational non-query) must NOT become HIGH.
    _rerank_scenario(monkeypatch, [0.55, 0.53])
    _spy_reranker(monkeypatch, winner=0)             # judge grabbed a weak candidate
    res = mi.retrieve('q')
    assert res['matches'][0]['slug'] == 't0'         # promoted to top...
    assert res['top_tier'] != 'high'                 # ...but not injected (0.55 < 0.58)


def test_rerank_winner_above_high_floor_is_injected(monkeypatch):
    _rerank_scenario(monkeypatch, [0.65, 0.60])
    _spy_reranker(monkeypatch, winner=1)
    res = mi.retrieve('q')
    assert res['matches'][0]['slug'] == 't1'
    assert res['matches'][0]['tier'] == 'high'       # strong pick (0.60 >= 0.58) injected


def test_rerank_gate_skips_judge_when_top1_dominates_and_gap_set(monkeypatch):
    # The ambiguity gate is OPT-IN: only KB_RERANK_GAP being set engages it.
    _rerank_scenario(monkeypatch, [0.70, 0.50, 0.45])
    monkeypatch.setenv('KB_RERANK_GAP', '0.04')
    calls = _spy_reranker(monkeypatch, winner=1)
    res = mi.retrieve('q')
    assert calls == []                              # dominant + gate on -> judge skipped
    assert res['matches'][0]['slug'] == 't0'
    assert res['top_tier'] == 'high'                # 0.70 >= 0.58


def test_rerank_gate_invokes_judge_when_top2_close(monkeypatch):
    _rerank_scenario(monkeypatch, [0.62, 0.60, 0.40])
    monkeypatch.setenv('KB_RERANK_GAP', '0.04')      # gate on, but top-2 are close
    calls = _spy_reranker(monkeypatch, winner=1)
    res = mi.retrieve('q')
    assert len(calls) == 1                           # ambiguous -> judge invoked
    assert res['matches'][0]['slug'] == 't1'         # winner promoted to top
    assert res['matches'][0]['tier'] == 'high'


def test_rerank_judge_rejection_yields_no_high(monkeypatch):
    _rerank_scenario(monkeypatch, [0.62, 0.60])
    calls = _spy_reranker(monkeypatch, winner=None)  # judge says "none relevant"
    res = mi.retrieve('q')
    assert len(calls) == 1
    assert all(m['tier'] != 'high' for m in res['matches'])
    assert res['top_tier'] != 'high'


def test_rerank_gate_skips_when_top1_below_floor(monkeypatch):
    _rerank_scenario(monkeypatch, [0.40, 0.38])
    calls = _spy_reranker(monkeypatch, winner=0)
    res = mi.retrieve('q')
    assert calls == []                               # nothing worth judging
    assert res['top_tier'] == 'none'


def test_rerank_always_forces_judge_despite_dominance(monkeypatch):
    _rerank_scenario(monkeypatch, [0.70, 0.50])
    monkeypatch.setenv('KB_RERANK_ALWAYS', '1')      # shadow / full-signal mode
    calls = _spy_reranker(monkeypatch, winner=0)
    mi.retrieve('q')
    assert len(calls) == 1                           # forced even though dominant


def test_rerank_dominant_below_high_floor_is_moderate(monkeypatch):
    _rerank_scenario(monkeypatch, [0.55, 0.40])
    monkeypatch.setenv('KB_RERANK_GAP', '0.04')      # gate on -> dominant skips judge
    calls = _spy_reranker(monkeypatch, winner=0)
    res = mi.retrieve('q')
    assert calls == []
    assert res['matches'][0]['tier'] == 'moderate'   # 0.52 <= 0.55 < 0.58


def test_split_frontmatter_strips_yaml_quotes():
    # kb.py emits compiled_at single-quoted because yaml quotes strings with ':'.
    text = (
        "---\n"
        "topic: Foo\n"
        "compiled_at: '2026-04-06T11:17:17+00:00'\n"
        "source_ids:\n"
        "  - 'abc'\n"
        "  - def\n"
        "---\n\n"
        "## Key points\n- y\n"
    )
    meta, _body = mi.split_frontmatter(text)
    assert meta['compiled_at'] == '2026-04-06T11:17:17+00:00'   # quotes stripped
    dt.datetime.fromisoformat(meta['compiled_at'])               # now ISO-parseable, must not raise
    assert meta['source_ids'] == ['abc', 'def']                  # list items unquoted too
    assert meta['topic'] == 'Foo'
