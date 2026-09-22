#!/usr/bin/env python3
"""Nightly hook auditor.

Scans logs/memory_injection.jsonl for likely memory-injection misfires, then
TIGHTENS config/memory_tuning.yaml (blocklist additions + threshold raises only)
and records suggestions in docs/hook-tuning/auto-audit.md, committing the change
to a dated branch for human merge.

TIGHTEN-ONLY: never lowers a threshold, never removes a blocklist entry.
Fail-open: any error -> no change, exit 0. Stdlib only (no PyYAML): it runs from
the nightly wrapper but shares the hook's stdlib read path and edits the YAML via
surgical text edits so comments/layout survive.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))
import memory_index as mi  # noqa: E402  (stdlib-only module)

LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'
OUTCOME_PATH = BASE_DIR / 'logs' / 'injection_outcomes.jsonl'
TUNING_PATH = BASE_DIR / 'config' / 'memory_tuning.yaml'
AUDIT_DOC = BASE_DIR / 'docs' / 'hook-tuning' / 'auto-audit.md'


def is_synthetic_head(head: str) -> bool:
    h = (head or '').lstrip()
    return h.startswith('<') or h.startswith('[Request interrupted')


def read_log(text: str, window_days: int, now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(days=window_days)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = rec.get('ts')
        if ts:
            try:
                when = dt.datetime.fromisoformat(ts)
            except Exception:
                continue
            if when < cutoff:
                continue
        out.append(rec)
    return out


def classify(records: list[dict], tuning: dict) -> dict:
    high = [r for r in records if r.get('tier') == 'high']
    total_high = len(high)
    synthetic_high = [r for r in high if is_synthetic_head(r.get('prompt_head'))]
    slug_misfires: Counter = Counter()
    slug_projects: dict[str, set] = defaultdict(set)
    for r in high:
        slug = r.get('injected')
        if not slug:
            continue
        if r.get('project'):
            slug_projects[slug].add(r['project'])
        if is_synthetic_head(r.get('prompt_head')):
            slug_misfires[slug] += 1
    cpm = tuning['auto_tune']['cross_project_min']
    promiscuous = sorted(s for s, ps in slug_projects.items() if len(ps) >= cpm)
    false_high_rate = round(len(synthetic_high) / total_high, 4) if total_high else 0.0
    return {
        'total_high': total_high,
        'synthetic_high': len(synthetic_high),
        'false_high_rate': false_high_rate,
        'slug_misfires': dict(slug_misfires),
        'promiscuous': promiscuous,
    }


def classify_outcomes(outcomes: list[dict], tuning: dict) -> dict:
    """Real-traffic UTILITY signal from logs/injection_outcomes.jsonl.

    `engaged` alone is not utility (2026-06-30 readiness review, gate 3). The
    verdict per HIGH outcome is:
      useful  := engaged AND NOT corrected   (the only thing that earns a spare)
      harmful := engaged AND corrected        (seductive-wrong — the model took the
                                               bait and the user corrected it)
      else    := non-useful (unengaged, or corrected-without-engagement)
    Counting `engaged` as positive would reward `harmful`, which is the precise
    anti-pattern Phase-4 governance exists to prevent — so we count UTILITY, not
    engagement. The silent-save case (unengaged-but-valuable) is unmeasurable here
    and lives in the non-useful bucket; that is an honest limitation, not a hole to
    paper over. We aggregate per slug so decide() can demote on `useful == 0`.

    Note: `outcomes` may mix scorer_version 1 rows (v1: substring proxy — lenient,
    e.g. "api" could match "therapist") with scorer_version 2 rows (v2, flipped
    2026-07-02: word-boundary + quorum match — stricter) depending on when each row
    was scored; this function does not distinguish them. An engagement/useful-rate
    shift straddling that date is therefore partly an INSTRUMENT change, not purely
    a behavior change — see outcome_matching.py for the v1->v2 semantics."""
    high = [o for o in outcomes if o.get('tier') == 'high']
    total = len(high)
    per: dict[str, dict] = defaultdict(
        lambda: {'high': 0, 'engaged': 0, 'useful': 0, 'harmful': 0, 'corrected': 0})
    non_useful_total = 0
    for o in high:
        slug = o.get('injected')
        if not slug:
            continue
        d = per[slug]
        d['high'] += 1
        eng = bool(o.get('engaged_in_assistant'))
        corr = bool(o.get('topic_corrected'))
        if eng:
            d['engaged'] += 1
        if corr:
            d['corrected'] += 1
        if eng and not corr:
            d['useful'] += 1
        else:
            non_useful_total += 1
            if eng and corr:
                d['harmful'] += 1
    rate = round(non_useful_total / total, 4) if total else 0.0
    return {
        'outcome_high': total,
        'outcome_non_useful': non_useful_total,
        'outcome_false_high_rate': rate,
        'outcome_slug': {k: dict(v) for k, v in per.items()},
    }


def decide(stats: dict, tuning: dict, ostats: dict | None = None) -> dict:
    at = tuning['auto_tune']
    blocked = set(tuning.get('high_ineligible', []))

    add_block = sorted(
        s for s, n in stats['slug_misfires'].items()
        if n >= at['min_samples'] and s not in blocked
    )
    for s in stats['promiscuous']:
        if s not in blocked and s not in add_block:
            add_block.append(s)

    add_block = sorted(set(add_block))

    # Real-traffic outcome demotion — REVERSIBLE, but by a HUMAN (gate 2 as amended
    # 2026-09-20, tune-merge plan (a)). Demote a slug that was injected HIGH >=
    # min_samples times in the rolling window with ZERO USEFUL outcomes (useful =
    # engaged AND NOT corrected; gate 3 — a seductive-wrong inject that got corrected
    # does NOT earn a spare). The proposal is the UNION of the current `outcome_demoted`
    # set and the freshly-derived one: this auditor only ever ADDS. A current demotion
    # whose evidence has aged out of the window is NOT dropped — it is reported in
    # `aged_out` and as a "consider undemoting" suggestion, because removals are
    # loosenings and loosenings are forever-human (the Stage-1 gate would otherwise
    # hold every post-merge proposal on the decay alone). Contrast `high_ineligible`,
    # permanent for manual catch-alls + synthetic-proven false-firers.
    current_demoted = set(tuning.get('outcome_demoted') or [])
    fresh: list[str] = []
    outcome_reasons: dict[str, str] = {}
    slug_stats = ostats.get('outcome_slug', {}) if ostats else {}
    for s, d in sorted(slug_stats.items()):
        non_useful = d['high'] - d['useful']
        if d['useful'] == 0 and non_useful >= at['min_samples'] and s not in blocked:
            fresh.append(s)
            outcome_reasons[s] = (
                f"injected HIGH {d['high']}x on real prompts, 0 useful"
                + (f" ({d['harmful']} engaged-but-corrected)" if d['harmful'] else ''))
    outcome_demoted = sorted(current_demoted | set(fresh))
    aged_out = sorted(current_demoted - set(fresh))

    new_high = None
    cur_high = tuning['thresholds']['high']
    syn_rate = stats['false_high_rate']
    out_rate = ostats['outcome_false_high_rate'] if ostats else 0.0
    out_samples = ostats['outcome_high'] if ostats else 0
    rate_triggered = (
        syn_rate >= at['false_high_rate_trigger']
        or (out_samples >= at['min_samples'] and out_rate >= at['false_high_rate_trigger']))
    if rate_triggered and cur_high < at['threshold_ceiling']:
        candidate = round(min(at['threshold_ceiling'], cur_high + at['threshold_step']), 4)
        if candidate > cur_high:          # tighten-only guard
            new_high = candidate

    suggestions = [
        f"re-scope candidate: global slug '{s}' injected at HIGH across "
        f">= {at['cross_project_min']} projects — consider narrowing its `projects`."
        for s in stats['promiscuous']
    ]
    for s in aged_out:
        d = slug_stats.get(s, {'high': 0, 'useful': 0})
        suggestions.append(
            f"consider undemoting '{s}' — its real-traffic evidence has aged out of the "
            f"{at['window_days']}-day window (real HIGH {d['high']}x, useful {d['useful']}); "
            f"removals are human-only.")
    return {'add_block': add_block, 'outcome_demoted': outcome_demoted,
            'aged_out': aged_out, 'new_high': new_high, 'suggestions': suggestions,
            'outcome_reasons': outcome_reasons}


def append_to_list_section(text: str, section: str, item: str) -> str:
    """Insert '  - <item>' after the last existing item under a bare 'section:'
    header. Idempotent (no-op if already present). Preserves all other lines,
    comments, and blank lines. Appends a new section if the header is absent."""
    item = str(item)
    lines = text.split('\n')
    hi = next((k for k, ln in enumerate(lines) if ln.rstrip() == f'{section}:'), None)
    if hi is None:
        suffix = '' if text.endswith('\n') else '\n'
        return text + f'{suffix}{section}:\n  - {item}\n'
    k = hi + 1
    last_item_k = None
    while k < len(lines):
        if re.match(r'^[A-Za-z0-9_]', lines[k]):   # next top-level key
            break
        if re.match(r'^\s*-\s+', lines[k]):
            existing = re.sub(r'^\s*-\s+', '', lines[k]).split('#', 1)[0].strip().strip('`"\'')
            if existing == item:
                return text                   # already present
            last_item_k = k
        k += 1
    insert_at = (last_item_k + 1) if last_item_k is not None else (hi + 1)
    lines.insert(insert_at, f'  - {item}')
    return '\n'.join(lines)


def replace_list_section(text: str, section: str, items: list) -> str:
    """Set 'section:' to EXACTLY the consecutive '- item' lines for `items`,
    creating the header if absent and clearing the items if `items` is empty.
    This is the REVERSIBLE counterpart to append_to_list_section: the auditor
    rewrites `outcome_demoted` wholesale each run, so a slug whose misfires aged
    out of the window simply disappears from the list (no one-way latch). Only the
    item lines directly under the header are replaced; comments/other sections are
    preserved."""
    new_items = [f'  - {it}' for it in items]
    lines = text.split('\n')
    hi = next((k for k, ln in enumerate(lines) if ln.rstrip() == f'{section}:'), None)
    if hi is None:
        if not items:
            return text                      # nothing to write, no empty header
        suffix = '' if text.endswith('\n') else '\n'
        return text + f'{suffix}{section}:\n' + '\n'.join(new_items) + '\n'
    k = hi + 1
    while k < len(lines) and re.match(r'^\s*-\s+', lines[k]):   # existing items only
        k += 1
    return '\n'.join(lines[:hi + 1] + new_items + lines[k:])


def set_nested_scalar(text: str, section: str, key: str, value) -> str:
    """Replace the value of 'key:' inside 'section:' block, preserving indentation
    and any trailing inline comment. Raises KeyError if not found."""
    lines = text.split('\n')
    hi = next((k for k, ln in enumerate(lines) if ln.rstrip() == f'{section}:'), None)
    if hi is None:
        raise KeyError(section)
    k = hi + 1
    while k < len(lines):
        if re.match(r'^[A-Za-z0-9_]+:', lines[k]):   # next top-level key
            break
        m = re.match(r'^(\s+)' + re.escape(key) + r':\s*(.*)$', lines[k])
        if m:
            rest = m.group(2)
            comment = ''
            if '#' in rest:
                idx = rest.index('#')
                comment_text = rest[idx:]
                # '#' sits at column len(indent + key + ': ' + rest[:idx]) in the
                # original line.  Derive padding so it stays at the same column;
                # guarantee at least 1 space if the new value is wider.
                orig_col = len(m.group(1)) + len(key) + 2 + idx  # 2 = len(': ')
                new_prefix_len = len(m.group(1)) + len(key) + 2 + len(str(value))
                padding = max(1, orig_col - new_prefix_len)
                comment = ' ' * padding + comment_text
            lines[k] = f'{m.group(1)}{key}: {value}{comment}'
            return '\n'.join(lines)
        k += 1
    raise KeyError(f'{section}.{key}')


def apply_actions(actions: dict, tuning_text: str) -> tuple[str, list[str]]:
    """Apply tighten-only actions to the tuning file TEXT. Returns (new_text,
    changes). Pure string transform — caller writes + commits."""
    text = tuning_text
    changes: list[str] = []
    for slug in actions.get('add_block', []):
        new = append_to_list_section(text, 'high_ineligible', slug)
        if new != text:
            changes.append(f"blocklist += {slug}")
            text = new
    # Outcome demotions: ADD-ONLY. decide() hands us the union with the current set;
    # write it only when there is something new, and report just the additions —
    # a removal can never originate here (human-only, see decide()).
    if 'outcome_demoted' in actions:
        existing = mi._parse_tuning_yaml(text).get('outcome_demoted') or []
        added = [s for s in actions['outcome_demoted'] if s not in existing]
        if added:
            demoted = sorted(set(existing) | set(added))
            new = replace_list_section(text, 'outcome_demoted', demoted)
            if new != text:
                changes.append(f"outcome_demoted += {', '.join(added)}")
                text = new
    if actions.get('new_high') is not None:
        text = set_nested_scalar(text, 'thresholds', 'high', actions['new_high'])
        changes.append(f"thresholds.high -> {actions['new_high']}")
    return text, changes


def render_audit_section(date: str, stats: dict, changes: list[str],
                         suggestions: list[str]) -> str:
    lines = [f"\n### {date} — memory-injection — auto-audit",
             f"- window HIGH fires: {stats.get('total_high', 0)} "
             f"(synthetic: {stats.get('synthetic_high', 0)}, "
             f"false-HIGH rate: {stats.get('false_high_rate', 0.0)})"]
    if stats.get('outcome_high'):
        lines.append(
            f"- real-traffic HIGH outcomes: {stats['outcome_high']} "
            f"(non-useful: {stats.get('outcome_non_useful', 0)}, "
            f"outcome false-HIGH rate: {stats.get('outcome_false_high_rate', 0.0)})")
    for slug, why in (stats.get('outcome_reasons') or {}).items():
        lines.append(f"  - demoted (reversible): {slug} — {why}")
    if changes:
        lines.append("- **applied (tighten-only):**")
        lines.extend(f"  - {c}" for c in changes)
    if suggestions:
        lines.append("- **suggestions (human action):**")
        lines.extend(f"  - {s}" for s in suggestions)
    if not changes and not suggestions:
        lines.append("- no action")
    return '\n'.join(lines) + '\n'


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(BASE_DIR), *args],
                          capture_output=True, text=True)


def _tracked_tree_dirty() -> bool:
    """True if there are uncommitted changes to TRACKED files. Untracked files
    are ignored (KB data, logs/, etc. are gitignored). A dirty tracked tree would
    be carried onto the auto-tune branch by `checkout -B` and could block the
    `checkout` back, stranding changes — so we skip the run entirely instead."""
    return bool(_git('status', '--porcelain', '--untracked-files=no').stdout.strip())


def _commit_to_branch(date: str, message: str, new_text: str, tuning_text: str,
                      audit_section: str) -> tuple[str, bool]:
    """Create auto-tune/<date>, write the tightened tuning file ON that branch,
    commit, push (--no-verify; config-only). The audit section is appended to the
    LOCAL audit doc (gitignored data — it quotes real prompts) and never committed.
    ALWAYS restores the original branch and leaves the working tree clean, even on
    failure. Raises on checkout/commit failure (caller is fail-open)."""
    orig = _git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip() or 'main'
    branch = f'auto-tune/{date}'
    if _git('checkout', '-B', branch).returncode != 0:
        _git('checkout', orig)
        raise RuntimeError('branch checkout failed')
    try:
        if new_text != tuning_text:
            TUNING_PATH.write_text(new_text)
        with AUDIT_DOC.open('a') as f:
            f.write(audit_section)
        _git('add', 'config/memory_tuning.yaml')
        commit = _git('commit', '-m', message)
        if commit.returncode != 0:
            raise RuntimeError(f'commit failed: {commit.stderr.strip()}')
        pushed = _git('push', '--no-verify', '-u', 'origin', branch).returncode == 0
        return branch, pushed
    finally:
        # discard any uncommitted residue for the tuning file, then return to orig,
        # so the tree is left clean whether we committed or failed mid-way. The
        # audit doc is untracked local data — the append survives on disk.
        _git('checkout', 'HEAD', '--', 'config/memory_tuning.yaml')
        _git('checkout', orig)


def run(now: dt.datetime, dry_run: bool = False) -> dict:
    tuning = mi.load_tuning()
    if not tuning['auto_tune'].get('enabled', True):
        return {'changed': False, 'disabled': True}
    window = tuning['auto_tune']['window_days']
    log_text = LOG_PATH.read_text(errors='replace') if LOG_PATH.exists() else ''
    records = read_log(log_text, window, now)
    stats = classify(records, tuning)

    # Real-traffic outcome signal (SessionEnd scorer): closes the loop the
    # synthetic-only view can't see — HIGH injections on genuine user prompts
    # the assistant never engaged with. read_log is a generic ts-windowed reader.
    outcome_text = OUTCOME_PATH.read_text(errors='replace') if OUTCOME_PATH.exists() else ''
    outcomes = read_log(outcome_text, window, now)
    ostats = classify_outcomes(outcomes, tuning)
    actions = decide(stats, tuning, ostats)
    stats = {**stats, **{k: ostats[k] for k in
                         ('outcome_high', 'outcome_non_useful', 'outcome_false_high_rate')},
             'outcome_reasons': actions.get('outcome_reasons', {})}

    tuning_text = TUNING_PATH.read_text() if TUNING_PATH.exists() else ''
    new_text, changes = apply_actions(actions, tuning_text)
    suggestions = actions['suggestions']
    # Only a config CHANGE is a proposal. Suggestions alone (promiscuity re-scopes,
    # aged-out demotions awaiting a human) are reported, never branched: a branch
    # with nothing but an audit note has nothing to adjudicate, and a standing
    # suggestion would otherwise cut one every night.
    changed = bool(changes)

    summary = {'changed': changed, 'changes': changes, 'suggestions': suggestions,
               'stats': stats, 'branch': None, 'pushed': None}
    if changes:
        # Stage-1 shadow merge-judge (docs/feedback-loop.md): a
        # deterministic verdict on THIS proposal, recorded against the branch name the
        # commit below would use. Recorded, never acted on — the human still merges.
        # Fail-open like everything else here: the gate can never break the nightly.
        summary['gate'] = _shadow_gate(tuning_text, new_text, now, record=not dry_run)
    if not changed or dry_run:
        return summary

    # Never operate on a dirty tracked tree: the branch switch could strand
    # unrelated uncommitted changes on the wrong branch. Skip cleanly (fail-open).
    if _tracked_tree_dirty():
        summary['skipped'] = 'dirty-tree'
        return summary

    date = now.strftime('%Y-%m-%d')
    section = render_audit_section(date, stats, changes, suggestions)
    msg = f"auto-tune {date}: " + '; '.join(changes)
    branch, pushed = _commit_to_branch(date, msg, new_text, tuning_text, section)
    summary['branch'], summary['pushed'] = branch, pushed
    return summary


def _shadow_gate(tuning_text: str, new_text: str, now: dt.datetime, record: bool) -> dict:
    branch = f"auto-tune/{now.strftime('%Y-%m-%d')}"
    try:
        import tune_gate                      # lazy: tune_gate imports this module
        return tune_gate.run_gate(tuning_text, new_text, branch=branch, now=now, record=record)
    except Exception as e:
        return {'verdict': 'error', 'reasons': [str(e)], 'branch': branch, 'changes': [],
                'evaluation': None, 'evidence': None}


def gate_brief(gate: dict | None) -> str:
    if not gate:
        return ''
    if gate.get('verdict') == 'error':
        return f"gate: error ({'; '.join(gate.get('reasons') or [])})"
    import tune_gate
    return 'gate: ' + tune_gate.brief(gate)


def main() -> None:
    if os.environ.get('KB_HEADLESS'):
        return
    ap = argparse.ArgumentParser(description='Nightly memory-injection hook auditor.')
    ap.add_argument('--dry-run', action='store_true',
                    help='classify + decide + print, but write/commit nothing')
    args = ap.parse_args()
    try:
        res = run(now=dt.datetime.now(), dry_run=args.dry_run)
    except Exception as e:           # fail-open: never break the nightly
        print(f"audit_hooks: skipped ({e})")
        return
    if res.get('disabled'):
        print("audit_hooks: disabled via config")
        return
    if res.get('skipped') == 'dirty-tree':
        print("audit_hooks: skipped — uncommitted tracked changes in the working tree")
        return
    if not res['changed']:
        sugg = res.get('suggestions') or []
        tail = f" ({len(sugg)} standing suggestion(s): " + ' | '.join(sugg) + ')' if sugg else ''
        print("audit_hooks: no action" + tail)
        return
    where = 'DRY-RUN (no commit)' if args.dry_run else f"branch {res.get('branch')}"
    bits = (res['changes'] + [f"+{len(res['suggestions'])} suggestion(s)"]) if res['suggestions'] else res['changes']
    line = f"audit_hooks: {where} — " + ('; '.join(bits) if bits else 'changes staged')
    gb = gate_brief(res.get('gate'))
    print(line + (f" | {gb}" if gb else ''))


if __name__ == '__main__':
    main()
