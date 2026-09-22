#!/usr/bin/env python3
"""Stage-1 deterministic tune gate — the shadow merge-judge for auto-tune proposals.

See docs/feedback-loop.md for the public workflow. For a PROPOSED config/memory_tuning.yaml vs the CURRENT
one, the gate answers "does this tightening suppress real misfires without losing real
hits?" with no LLM in the loop:

  * false_high_on_negatives over the eval set must be strictly non-increasing;
  * golden positives: zero lost HIGH injections (a tightening that silences a
    labeled-correct injection is the harmful case, categorically);
  * evidence floor: every proposed change must be reproduced by the auditor's own
    decide() when fed REAL-traffic evidence only (no tune on synthetic-only signal);
  * any LOOSENING (threshold lowered, a slug un-blocked/un-demoted) is forever-human
    and can never receive a `merge` verdict here.

Verdict = `merge` | `hold` + the numbers, appended to state/tune_verdicts.jsonl. The
gate is SHADOW: it records, it never merges. The operator still merges or rejects,
and each decision accumulates an (operator, system) agreement pair for Stage 2.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))
sys.path.insert(0, str(BASE_DIR / 'evals'))

import memory_index as mi          # noqa: E402
from memory_retrieval import MemoryRetriever  # noqa: E402
from run_eval import load_cases    # noqa: E402  (skips '#' comment lines)

CASES_PATH = BASE_DIR / 'evals' / 'cases.jsonl'
LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
OUTCOME_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
VERDICTS_PATH = BASE_DIR / 'state' / 'tune_verdicts.jsonl'
DECISIONS_PATH = BASE_DIR / 'state' / 'tune_decisions.jsonl'
GATE_VERSION = 'stage1-v2'   # bump when a bar, the evidence definition or the eval population changes
                             # v2 (2026-09-20): held_out split excluded from the gate's eval arms
STAGE2_N = 8                 # agreement gate: most recent n>=8 pairs, zero counted disagreements


def diff_tuning(current: dict, proposed: dict) -> list[dict]:
    """What a proposal changes, each tagged tighten=True/False.

    Tightening: a slug added to `outcome_demoted` (demote) or `high_ineligible`
    (block), or `thresholds.high` raised. Anything in the other direction is a
    loosening and is flagged tighten=False so the verdict can refuse it."""
    changes: list[dict] = []
    cur_dem = set(current.get('outcome_demoted') or [])
    new_dem = set(proposed.get('outcome_demoted') or [])
    cur_blk = set(current.get('high_ineligible') or [])
    new_blk = set(proposed.get('high_ineligible') or [])
    for slug in sorted(new_dem - cur_dem):
        changes.append({'kind': 'demote', 'slug': slug, 'tighten': True})
    for slug in sorted(cur_dem - new_dem):
        changes.append({'kind': 'undemote', 'slug': slug, 'tighten': False})
    for slug in sorted(new_blk - cur_blk):
        changes.append({'kind': 'block', 'slug': slug, 'tighten': True})
    for slug in sorted(cur_blk - new_blk):
        changes.append({'kind': 'unblock', 'slug': slug, 'tighten': False})
    cur_hi = (current.get('thresholds') or {}).get('high')
    new_hi = (proposed.get('thresholds') or {}).get('high')
    if cur_hi is not None and new_hi is not None and new_hi != cur_hi:
        changes.append({'kind': 'threshold', 'from': cur_hi, 'to': new_hi,
                        'tighten': new_hi > cur_hi})
    return changes


def _arm(cases: list[dict], tuning: dict, retrieve) -> dict:
    """One tuning arm over the eval set: which negatives fire HIGH (false_high) and
    which positives get their expected slug injected at HIGH (hit@1, the hook's
    actual inject condition: top_tier == 'high' and matches[0] is expected)."""
    false_high: list[str] = []
    high_hits: list[str] = []
    for c in cases:
        res = retrieve(c['prompt'], project=c.get('project'), tuning=tuning)
        top = (res.get('matches') or [None])[0]
        is_high = res.get('top_tier') == 'high' and top is not None
        if not c.get('expect'):
            if is_high:
                false_high.append(c['id'])
        elif is_high and top.get('slug') in c['expect']:
            high_hits.append(c['id'])
    return {'false_high': false_high, 'high_hits': high_hits}


def evaluate_cases(cases: list[dict], current: dict, proposed: dict, retrieve) -> dict:
    """Run every case under CURRENT and PROPOSED tuning through the same retrieve
    callable (MemoryRetriever in production). Returns the two bar inputs per arm plus
    the positives lost/gained by the proposal."""
    cur, prop = _arm(cases, current, retrieve), _arm(cases, proposed, retrieve)
    cur_hits, prop_hits = set(cur['high_hits']), set(prop['high_hits'])
    return {
        'n_positives': sum(1 for c in cases if c.get('expect')),
        'n_negatives': sum(1 for c in cases if not c.get('expect')),
        'false_high': {'current': len(cur['false_high']), 'proposed': len(prop['false_high'])},
        'false_high_ids': {'current': cur['false_high'], 'proposed': prop['false_high']},
        'high_hits': {'current': cur['high_hits'], 'proposed': prop['high_hits']},
        'lost': sorted(cur_hits - prop_hits),
        'gained': sorted(prop_hits - cur_hits),
    }


def evidence_floor(changes: list[dict], tuning: dict, records: list[dict],
                   outcomes: list[dict]) -> dict:
    """Evidence floor: re-run the auditor's own decide() on REAL-traffic evidence only
    and require each proposed change to be reproduced. Synthetic-head HIGH rows (the
    pre-guard `<task-notification>`-style fires) are dropped before classify(), so a
    tune that only synthetic signal would justify is unsupported here. Reusing
    decide() means the floor is, by construction, the auditor's existing thresholds."""
    import audit_hooks as a   # lazy: audit_hooks imports this module for the nightly hook
    real = [r for r in records if not a.is_synthetic_head(r.get('prompt_head', ''))]
    stats = a.classify(real, tuning)
    ostats = a.classify_outcomes(outcomes, tuning)
    actions = a.decide(stats, tuning, ostats)
    per: list[dict] = []
    for ch in changes:
        kind = ch['kind']
        if kind == 'demote':
            slug = ch['slug']
            met = slug in actions.get('outcome_demoted', [])
            d = ostats.get('outcome_slug', {}).get(slug, {})
            detail = (f"{slug}: real HIGH {d.get('high', 0)}x, useful {d.get('useful', 0)}"
                      f" (needs >= {tuning['auto_tune']['min_samples']} non-useful, 0 useful)")
        elif kind == 'block':
            slug = ch['slug']
            met = slug in actions.get('add_block', [])
            detail = (f"{slug}: {'promiscuous on real prompts' if met else 'no real-traffic block signal'}"
                      f" (real HIGH rows {sum(1 for r in real if r.get('tier') == 'high' and r.get('injected') == slug)})")
        elif kind == 'threshold':
            new_high = actions.get('new_high')
            met = new_high is not None and abs(float(new_high) - float(ch['to'])) < 1e-9
            detail = (f"real outcome false-HIGH rate {ostats.get('outcome_false_high_rate')}"
                      f" over {ostats.get('outcome_high', 0)} HIGH outcomes"
                      f" (trigger {tuning['auto_tune']['false_high_rate_trigger']}, "
                      f"min {tuning['auto_tune']['min_samples']}); real-only decide → {new_high}")
        else:                          # loosenings never have an evidence path
            met, detail = False, f"{kind}: loosening — no automatic evidence path"
        per.append({**ch, 'met': met, 'detail': detail})
    return {'met': all(c['met'] for c in per) if per else True, 'per_change': per}


def _change_label(ch: dict) -> str:
    if ch['kind'] == 'threshold':
        return f"threshold {ch['from']}→{ch['to']}"
    return f"{ch['kind']} {ch['slug']}"


def verdict(changes: list[dict], evaluation: dict, evidence: dict) -> dict:
    """`merge` only when every bar holds; otherwise `hold` with every failed bar named
    (an operator adjudicating pair #N needs all of them, not the first)."""
    reasons: list[str] = []
    if not changes:
        reasons.append('empty proposal — nothing to merge')
    for ch in changes:
        if not ch.get('tighten', False):
            reasons.append(f"loosening is forever-human: {_change_label(ch)}")
    fh = evaluation['false_high']
    if fh['proposed'] > fh['current']:
        reasons.append(f"false_high_on_negatives rose {fh['current']}→{fh['proposed']}"
                       f"/{evaluation['n_negatives']}")
    if evaluation['lost']:
        reasons.append("lost HIGH hit(s): " + ', '.join(evaluation['lost']))
    if not evidence.get('met', False):
        thin = [c for c in evidence.get('per_change', []) if not c.get('met')]
        reasons.append("evidence floor unmet: " + '; '.join(c['detail'] for c in thin))
    return {'verdict': 'merge' if not reasons else 'hold', 'reasons': reasons}


def brief(result: dict) -> str:
    """One line for the nightly Telegram: verdict · the numbers · the change · branch."""
    ev, fh = result['evaluation'], result['evaluation']['false_high']
    bits = [result['verdict'],
            f"fh {fh['current']}→{fh['proposed']}/{ev['n_negatives']}",
            f"lost {len(ev['lost'])}/{ev['n_positives']}",
            'evidence ' + ('ok' if result['evidence'].get('met') else 'THIN'),
            ', '.join(_change_label(c) for c in result['changes']) or 'no changes']
    if result.get('branch'):
        bits.append(result['branch'])
    line = ' · '.join(bits)
    if result['reasons']:
        line += ' — ' + '; '.join(result['reasons'])
    return line.replace('\n', ' ')


def parse_tuning(text: str) -> dict:
    """A tuning dict from YAML text, merged over the defaults exactly as
    memory_index.load_tuning() does for the live file."""
    return mi._deep_merge(mi.DEFAULT_TUNING, mi._parse_tuning_yaml(text))


def _lexical_retrieve(query, project=None, tuning=None):
    """Pin the gate to lexical without changing process-wide scorer selection."""
    return MemoryRetriever(mode='lexical').retrieve(query, project=project, tuning=tuning)


def record_verdict(row: dict, path: Path | None = None) -> None:
    path = path or VERDICTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(row) + '\n')


def run_gate(current_text: str, proposed_text: str, *, branch: str | None,
             now: dt.datetime, record: bool = True) -> dict:
    """The whole Stage-1 gate for one proposal. Evidence is windowed exactly as the
    auditor windows it (auto_tune.window_days back from `now`), so `now` = the
    proposal's own night reproduces the evidence it was made on."""
    import audit_hooks as a
    current, proposed = parse_tuning(current_text), parse_tuning(proposed_text)
    changes = diff_tuning(current, proposed)
    # Gate-1 discipline (roadmap §11 #1): the held_out split is never tuned against,
    # and a merge decision IS tuning — so the gate sees train + val only.
    all_cases = load_cases(CASES_PATH)
    cases = [c for c in all_cases if c.get('split') != 'held_out']
    evaluation = evaluate_cases(cases, current, proposed, retrieve=_lexical_retrieve)
    evaluation['excluded_held_out'] = len(all_cases) - len(cases)
    window = current['auto_tune']['window_days']
    log_text = LOG_PATH.read_text(errors='replace') if LOG_PATH.exists() else ''
    out_text = OUTCOME_PATH.read_text(errors='replace') if OUTCOME_PATH.exists() else ''
    evidence = evidence_floor(changes, current, records=a.read_log(log_text, window, now),
                              outcomes=a.read_log(out_text, window, now))
    v = verdict(changes, evaluation, evidence)
    result = {
        'ts': dt.datetime.now().isoformat(timespec='seconds'),
        'as_of': now.strftime('%Y-%m-%d'),
        'branch': branch,
        'changes': changes,
        'evaluation': evaluation,
        'evidence': evidence,
        'verdict': v['verdict'],
        'reasons': v['reasons'],
        'gate_version': GATE_VERSION,
    }
    if record:
        record_verdict(result)
    return result


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors='replace').splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def branch_verdict(branch: str, verdicts: list[dict]) -> dict | None:
    """The system's verdict for a branch, keyed at the BRANCH DATE (operator ruling
    2026-09-18: a backlog branch's pair uses the verdict as of the night it was cut,
    never a later re-read — a later re-read measures evidence decay, not the
    proposal). Falls back to the earliest recorded as_of when no row matches the
    branch date exactly."""
    rows = [r for r in verdicts if r.get('branch') == branch]
    if not rows:
        return None
    date = branch.rsplit('/', 1)[-1]
    exact = [r for r in rows if r.get('as_of') == date]
    return exact[-1] if exact else sorted(rows, key=lambda r: r.get('as_of') or '')[0]


def record_decision(branch: str, decision: str, note: str = '') -> dict:
    """Append one (operator, system) agreement pair. `disagreement` names the kind;
    only system-merge/operator-reject counts against the Stage-2 gate (a hold the
    operator overrides to merge is the human being MORE permissive, which autonomy
    never is)."""
    if decision not in ('merge', 'reject'):
        raise ValueError(f"decision must be merge|reject, got {decision!r}")
    v = branch_verdict(branch, _read_jsonl(VERDICTS_PATH))
    if v is None:
        raise ValueError(f"no gate verdict recorded for {branch} — run the gate first")
    system = v['verdict']
    agree = (system == 'merge') == (decision == 'merge')
    disagreement = None if agree else f"system-{system}/operator-{decision}"
    row = {'ts': dt.datetime.now().isoformat(timespec='seconds'), 'branch': branch,
           'system': system, 'system_as_of': v.get('as_of'), 'gate_version': v.get('gate_version'),
           'operator': decision, 'agree': agree, 'disagreement': disagreement, 'note': note}
    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_PATH.open('a') as f:
        f.write(json.dumps(row) + '\n')
    return row


def agreement_status(decisions: list[dict], n_required: int = STAGE2_N) -> dict:
    """Stage-2 gate arithmetic (plan, signed): over the most recent n_required pairs,
    zero system-merge/operator-reject. Reported, never acted on by this module."""
    recent = decisions[-n_required:]
    counted = sum(1 for r in recent if r.get('disagreement') == 'system-merge/operator-reject')
    return {'n': len(decisions), 'n_required': n_required, 'counted_disagreements': counted,
            'gate_clear': len(decisions) >= n_required and counted == 0}


def branch_texts(branch: str, base: str = 'main') -> tuple[str, str]:
    """(CURRENT, PROPOSED) tuning YAML for an auto-tune branch, both via `git show` so
    the working tree is never touched: PROPOSED is the branch tip, CURRENT is the
    tuning it was cut against (the branch's merge-base with `base`)."""
    def show(rev: str) -> str:
        return subprocess.run(['git', 'show', f'{rev}:config/memory_tuning.yaml'],
                              cwd=BASE_DIR, check=True, capture_output=True, text=True).stdout
    mb = subprocess.run(['git', 'merge-base', base, branch], cwd=BASE_DIR, check=True,
                        capture_output=True, text=True).stdout.strip()
    return show(mb), show(branch)


def branch_date(branch: str) -> dt.datetime | None:
    """auto-tune/YYYY-MM-DD → end of that day (the evidence window the proposal was
    made on); None for any other branch name."""
    tail = branch.rsplit('/', 1)[-1]
    try:
        d = dt.datetime.strptime(tail, '%Y-%m-%d')
    except ValueError:
        return None
    return d.replace(hour=23, minute=59, second=59)


def _main_adjudicate(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog='tune_gate.py adjudicate',
                                 description='Record the operator decision on a judged proposal.')
    ap.add_argument('--branch', required=True)
    ap.add_argument('--decision', required=True, choices=['merge', 'reject'])
    ap.add_argument('--note', default='')
    args = ap.parse_args(argv)
    row = record_decision(args.branch, args.decision, note=args.note)
    st = agreement_status(_read_jsonl(DECISIONS_PATH))
    print(f"pair recorded: {row['branch']} system={row['system']}@{row['system_as_of']} "
          f"operator={row['operator']} → {'agree' if row['agree'] else row['disagreement']} · "
          f"Stage-2 gate: {st['n']}/{st['n_required']} pairs, {st['counted_disagreements']} counted "
          f"disagreement(s), {'CLEAR' if st['gate_clear'] else 'not clear'}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == 'adjudicate':
        _main_adjudicate(sys.argv[2:])
        return
    ap = argparse.ArgumentParser(description='Stage-1 deterministic tune gate (shadow merge-judge).')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--branch', help='auto-tune/YYYY-MM-DD branch to judge (read via git show)')
    src.add_argument('--proposed', help='proposed memory_tuning.yaml file')
    ap.add_argument('--current', help='current memory_tuning.yaml (default: live config; '
                                      'with --branch: the merge-base version)')
    ap.add_argument('--as-of', help='evidence window end, YYYY-MM-DD (default: branch date, else today)')
    ap.add_argument('--no-record', action='store_true', help='do not append to state/tune_verdicts.jsonl')
    ap.add_argument('--json', action='store_true', help='print the full result as JSON')
    args = ap.parse_args()

    if args.branch:
        current_text, proposed_text = branch_texts(args.branch)
        if args.current:
            current_text = Path(args.current).read_text()
        now = branch_date(args.branch) or dt.datetime.now()
    else:
        proposed_text = Path(args.proposed).read_text()
        current_text = Path(args.current).read_text() if args.current else mi.TUNING_PATH.read_text()
        now = dt.datetime.now()
    if args.as_of:
        now = dt.datetime.strptime(args.as_of, '%Y-%m-%d').replace(hour=23, minute=59, second=59)

    result = run_gate(current_text, proposed_text, branch=args.branch, now=now,
                      record=not args.no_record)
    print(json.dumps(result, indent=2) if args.json else brief(result))


if __name__ == '__main__':
    main()
