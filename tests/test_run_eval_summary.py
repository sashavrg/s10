"""Tests for run_eval.summarize stratification (front #1a).

Gate-1 wants the held-out slice reported separately; the operator wants project×kind
stratification (reads whether a config generalizes across clients and query-shapes).
summarize is pure (rows -> dict), so these use synthetic rows — no index needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'evals'))
import run_eval  # noqa: E402


def _row(rid, kind, project, split, expect, *, hit=False, false_high=False):
    return {
        'id': rid, 'kind': kind, 'project': project, 'split': split, 'expect': expect,
        'top_slug': 'x', 'top_tier': 'high' if (hit or false_high) else 'none',
        'top_score': 0.8, 'rank_of_expect': 0 if hit else None,
        'expect_tier': 'high' if hit else None, 'in_topk': hit,
        'recall_at_high': hit, 'hit_at_1': hit, 'retrieved': hit, 'false_high': false_high,
    }


def test_summarize_reports_held_out_split_separately():
    rows = [
        _row('a', 'paraphrase', 'acme', 'train', ['s'], hit=True),
        _row('b', 'paraphrase', 'acme', 'held_out', ['s'], hit=False),
        _row('c', 'paraphrase', 'acme', 'held_out', ['s'], hit=True),
    ]
    s = run_eval.summarize(rows, k=5)
    assert 'held_out' in s['by_split']
    assert s['by_split']['held_out']['n'] == 2
    assert s['by_split']['train']['n'] == 1


def test_summarize_stratifies_by_project_kind():
    rows = [
        _row('a', 'paraphrase', 'acme', 'train', ['s'], hit=True),
        _row('b', 'negative', 'llm-kb', 'val', [], false_high=True),
    ]
    s = run_eval.summarize(rows, k=5)
    assert 'acme·paraphrase' in s['by_project_kind']
    assert 'llm-kb·negative' in s['by_project_kind']
    # negative cell carries the false-HIGH rate
    assert s['by_project_kind']['llm-kb·negative']['false_high_on_negatives'] == '1/1 (100%)'


def test_summarize_keeps_backward_compatible_overall_keys():
    rows = [_row('a', 'paraphrase', 'acme', 'train', ['s'], hit=True)]
    s = run_eval.summarize(rows, k=5)
    for key in ('recall_at_high', 'recall_at_k', 'hit_at_1', 'retrieved'):
        assert key in s['overall']
