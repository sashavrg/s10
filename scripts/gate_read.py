#!/usr/bin/env python3
"""ej9 gate-read runner — mechanizes the ONE blind gate read per the
operator-signed addendum (docs/superpowers/specs/2026-07-20-ej9-gate-read-addendum.md).

Operations order (A4): the operator's labels exist FIRST (`gate_dossier.py
parse` wrote evals/candidates/ej9-gate-labels.jsonl) — this runner refuses to
start without them. It then judges every labeled row with the FROZEN judge
(ej9 prompt/evidence from engagement_judge.py) through a model-pinned cloud
call (A1: explicit `claude-sonnet-5`, payload `modelUsage` asserted — a silent
alias remap aborts the read instead of invalidating it), and scores ONE pass
through the amended A3 verdict function (gate_dossier.gate_verdict).

Refusals are loud and total: under-powered set (< 35 stage-1 / < 50 stage-2),
any row unjudged after one retry, degenerate margins, or any pin violation.
A partial or compromised read never produces a verdict.

Outputs (DATA — evals/candidates/ is gitignored, never commit):
  ej9-gate-judged.jsonl      per-row labels + judge verdicts (audit trail)
  ej9-gate-read-VERDICT.md   the decision block (round-1 VERDICT format)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import engagement_judge as ej
import gate_dossier as gd
import kb
from injection_outcome import parse_turns
from scorecard import wilson

BASE_DIR = Path(__file__).resolve().parent.parent
LABELS_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-labels.jsonl'
INJECTION_LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
JUDGED_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-judged.jsonl'
VERDICT_PATH = BASE_DIR / 'evals' / 'candidates' / 'ej9-gate-read-VERDICT.md'

GATE_MODEL_ID = 'claude-sonnet-5'   # A1 pin (alias resolution recorded 2026-07-20)
STAGE_N = {1: 35, 2: 50}            # full-power minimums (A2 / A3)
PIN_VIOLATIONS: list[str] = []


class GateReadError(RuntimeError):
    pass


def pinned_generate(prompt: str) -> str:
    """One judge call with the A1 model pin asserted on the CLI payload."""
    payload = kb.claude_code_payload(GATE_MODEL_ID, prompt)
    models = sorted((payload.get('modelUsage') or {}).keys())
    if models != [GATE_MODEL_ID]:
        PIN_VIOLATIONS.append(f'payload reports {models}')
        raise GateReadError(f"model pin violated (A1): payload reports {models}, "
                            f"expected ['{GATE_MODEL_ID}']")
    return (payload.get('result') or '').strip()


def score_rows(rows: list[dict]) -> dict:
    tp = sum(1 for r in rows if r['judged_engaged'] and r['label_engaged'])
    fp = sum(1 for r in rows if r['judged_engaged'] and not r['label_engaged'])
    fn = sum(1 for r in rows if not r['judged_engaged'] and r['label_engaged'])
    tn = sum(1 for r in rows if not r['judged_engaged'] and not r['label_engaged'])
    return {'n': len(rows), 'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn,
            'precision': round(tp / (tp + fp), 4) if tp + fp else None,
            'recall': round(tp / (tp + fn), 4) if tp + fn else None,
            'p_ci': wilson(tp, tp + fp) if tp + fp else None,
            'r_ci': wilson(tp, tp + fn) if tp + fn else None}


def exclusion_read(rows: list[dict]) -> dict:
    """The §2.8 informational read: minus rows the operator pre-flagged
    `identity_matches_subject: no` at labeling time (the timing is the guard)."""
    return score_rows([r for r in rows if r.get('identity_matches_subject', True)])


def run_read(labels: list[dict], judge_fn, stage: int = 1) -> dict:
    """Judge every labeled row (one retry for a None verdict), then ONE scoring
    pass. judge_fn(label) -> bool | None. Refuses partial or degenerate reads."""
    need = STAGE_N.get(stage)
    if need is None:
        raise GateReadError(f'unknown stage {stage}')
    if len(labels) < need:
        raise GateReadError(f'full-power requirement not met: {len(labels)} labeled '
                            f'rows < {need} (stage {stage}, addendum A2/A3)')
    judged, failed = [], 0
    for label in labels:
        v = judge_fn(label)
        if v is None:
            v = judge_fn(label)                    # exactly one retry
        if v is None:
            failed += 1
            continue
        judged.append({**label, 'judged_engaged': bool(v)})
    if failed:
        raise GateReadError(f'{failed} rows unjudged after retry — '
                            'the read must be complete before scoring')
    main = score_rows(judged)
    if main['precision'] is None or main['recall'] is None:
        raise GateReadError('degenerate read: an empty precision or recall margin '
                            'cannot decide the gate')
    return {'stage': stage, 'model': GATE_MODEL_ID, 'judge_version': ej.JUDGE_VERSION,
            'main': main, 'exclusion': exclusion_read(judged),
            'verdict': gd.gate_verdict(main['precision'], main['recall'], stage=stage),
            'rows': judged}


def build_gate_jobs(labels: list[dict], inj_by_key: dict) -> list[tuple]:
    """(label, judge-parity evidence | None) per labeled row — evidence built
    exactly as production/dev would (ej.build_evidence, HIGH: no topic page)."""
    jobs, turns_cache = [], {}
    for label in labels:
        sid = label.get('session_id') or ''
        inj = inj_by_key.get((sid, label.get('ts')))
        ev = None
        if inj is not None:
            if sid not in turns_cache:
                tp = ej.resolve_transcript(sid)
                turns_cache[sid] = parse_turns(tp) if tp else []
            if turns_cache[sid]:
                inj_eff = {**inj, 'injected': label.get('injected'), 'tier': 'high'}
                ev = ej.build_evidence(inj_eff, turns_cache[sid], None)
        jobs.append((label, ev))
    return jobs


def render_verdict_md(report: dict) -> str:
    m, x = report['main'], report['exclusion']

    def cells(s):
        return (f"tp{s['tp']} fp{s['fp']} fn{s['fn']} tn{s['tn']} -> "
                f"P={s['precision']} R={s['recall']}")

    return '\n'.join([
        f"EJ9 GATE READ — stage {report['stage']} — judge {report['judge_version']} "
        f"· model {report['model']} (A1-pinned)",
        f"HIGH (DECISION, n={m['n']}): {cells(m)} · "
        f"CIs informational: P {m['p_ci']} R {m['r_ci']}",
        f"§2.8 exclusion read (minus pre-flagged, n={x['n']}, info): {cells(x)}",
        f"VERDICT: {report['verdict'].upper()} — stage-1 bars 0.80/0.70 "
        "(band floors 0.70/0.60); stage-2 bars 0.82/0.72 (addendum A3)",
        '',
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description='ej9 gate read (addendum A1/A3/A4).')
    ap.add_argument('--stage', type=int, choices=(1, 2), default=1,
                    help='1 = first read at n>=35; 2 = the one EXPAND re-read at n>=50')
    args = ap.parse_args()
    if not LABELS_PATH.exists():
        raise SystemExit(f'no labels at {LABELS_PATH} — run `gate_dossier.py parse` '
                         'first (A4 order: labels BEFORE judge)')
    labels = [json.loads(l) for l in LABELS_PATH.read_text().splitlines() if l.strip()]
    inj_by = {(r.get('session_id'), r.get('ts')): r
              for r in gd._read_jsonl(INJECTION_LOG_PATH)}
    jobs = build_gate_jobs(labels, inj_by)
    missing = sum(1 for _, ev in jobs if ev is None)
    if missing:
        raise SystemExit(f'{missing} rows lack evidence (transcript / injection row '
                         'gone) — aborting before any judge call')

    PIN_VIOLATIONS.clear()
    # Smoke: pin + transport + parse checked OUTSIDE the fail-open judge path,
    # so a systematic failure aborts loudly before the batch.
    if ej.parse_verdict(pinned_generate(ej._build_prompt(jobs[0][1]))) is None:
        raise SystemExit('smoke call produced no parseable verdict — aborting')

    ev_by_key = {(l['session_id'], l['ts'], l['injected']): ev for l, ev in jobs}

    def judge_fn(label):
        ev = ev_by_key[(label['session_id'], label['ts'], label['injected'])]
        v = ej.judge_engagement(ev, generate_fn=pinned_generate)
        return None if v is None else v['engaged']

    try:
        report = run_read(labels, judge_fn, stage=args.stage)
    except GateReadError:
        if PIN_VIOLATIONS:
            raise SystemExit(f'model pin violated during batch (A1): '
                             f'{PIN_VIOLATIONS[0]} — read INVALID, not scored')
        raise
    JUDGED_PATH.write_text('\n'.join(json.dumps(r) for r in report['rows']) + '\n')
    md = render_verdict_md(report)
    VERDICT_PATH.write_text(md)
    print(md)
    print(f'judged rows -> {JUDGED_PATH}\nverdict -> {VERDICT_PATH}')


if __name__ == '__main__':
    main()
