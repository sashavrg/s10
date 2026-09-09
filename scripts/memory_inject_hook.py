#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — the EAGER path of the hybrid memory system.

Per user turn it does ONE of three things, then exits:
  HIGH tier      -> inject settled facts as directives (invisible, behavior-changing)
  MODERATE tier  -> inject a one-line pointer (advertises; enables the lazy pull)
  no match       -> inject nothing (silence is correct; nothing to advertise)

Everything is budget-capped and every decision is logged with its tier + score,
so the thresholds in memory_index.py can be tuned from real traffic rather than
guessed. This hook NEVER blocks or fails a turn: any error -> emit nothing, exit 0.

Claude Code contract: UserPromptSubmit receives a JSON payload on stdin
(prompt, cwd, ...). To add context to the turn, we print the context to stdout;
Claude Code injects stdout into the model's context for that turn. Emitting
nothing adds nothing.
"""
from __future__ import annotations

import json
import os
import sys
import datetime as dt
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))

# Live retrieval config, pinned for the collection window: RERANK with bounded
# lexical fallback (measurement-v2 Task 9, operator-signed 2026-07-29).
#
# All three pre-committed criteria held — golden-set hit@1 44% vs lexical 19%
# (bar: +15pts), real-negative false-HIGH 12% (bar: <=15%), shadow judge
# reject-rate 55% (band: 40-65%). Deploy shape, per the operator's resolution of
# the latency problem (rerank measured 7-12s COLD vs the 10s hook kill):
#
#   1. INTERNAL deadline, safely under the kill: KB_RERANK_TIMEOUT=6. On expiry the
#      judge call fails WITHOUT a verdict and memory_index.retrieve() injects the
#      LEXICAL result instead, reporting mode='lexical' + rerank_timeout=True — no
#      code path may end in a dropped row, and every row names its real scorer.
#   2. Semantics-preserving warm strategy only: SessionStart warm-up (warm_reranker.sh)
#      preloads the judge model, and the keepalive spans real inter-turn gaps. Warm,
#      the judge is ~1-2s. NOT allowed under the existing certification: smaller
#      judge model, reduced K, prompt edits — any of those is a NEW scorer and goes
#      through the eval gate before it may ship.
#   3. Burn-in threshold, pre-committed before data: window reopen requires
#      fallback rate (rerank_timeout rows / retrieval rows) < 15% over the 3-day
#      burn-in (probe: scripts/fallback_rate.py). Above it, the flip is fictional
#      — THAT triggers designing a faster rerank variant through the full eval gate.
#
# STATUS 2026-07-29 (operator-signed): live scorer = RERANK-K5F2 with bounded
# lexical fallback — the variant that survived the ladder after the certified
# k10f3's warm-prefill (6-11s/fresh prompt, CPU-offloaded 7B) lost every turn to
# the 6s deadline. Ladder record: (a) full-GPU judge — dead, embed already 0 VRAM
# and a forced num_gpu=99 load FAILS outright on the 6GB card; (b) K/facts sweep
# through the eval gate — k5f2 passed (hit@1 38% >= 34% bar, false-HIGH 8%,
# latency n=13 mean 3.80s max 4.73s, 0/13 over the deadline); (c) smaller judge
# model — not reached. Quality trade, stated not absorbed: -6pp hit@1 vs k10f3
# (38% vs 44%), false-HIGH improved (8% vs 12%), traded for deployability.
#
# The 15% burn-in fallback gate (scripts/fallback_rate.py, stratified by prompt
# length) is the arbiter: a fat latency tail -> stamped fallback rows -> the gate
# re-fires the ABOVE branch. Rows pin the config via retrieval_config
# ('rerank-k5f2'), which names the ATTEMPTED scorer even on fallback rows.
#
# STATUS 2026-08-01 (operator-signed): k5f2-on-7B burn-in TERMINATED BY
# SIDE-CHANNEL — desktop applications crashing under the resident 7B's GPU
# pressure. Same precedent as the prefill early-invoke: the ABOVE-threshold
# branch firing on mechanism evidence. Live retrieval is LEXICAL again; the
# 7B judge is unloaded and the SessionStart warm-up unregistered. The partial
# burn-in data (0 timeouts at termination) is NOT a k5f2 verdict and must never
# be read as one — the window it would have fed never opened.
#
# Next ladder rung: cloud haiku as the reranker judge — CANDIDATE, not choice;
# it becomes the choice when it clears BOTH gate sets (golden hit@1 >= 34%,
# real-negatives false-HIGH <= 15%) plus the pre-committed latency bar. Primary
# candidate is k10f3-on-haiku (k5f2 existed only to shrink prefill through the
# CPU-offloaded 7B; that constraint is gone with a zero-VRAM judge, so the full
# certified shape is recovered). Re-enable = setdefaults for KB_RETRIEVAL=rerank,
# KB_RERANK_BACKEND=claude, K/F per the certified config, fitted timeout; no
# keepalive (nothing resident).
# os.environ.setdefault('KB_RETRIEVAL', 'rerank')     # awaiting haiku certification

LOG_PATH = BASE_DIR / 'logs' / 'memory_injection.jsonl'

# Hard budget. Even a HIGH match never injects more than this many facts; the
# cap is what structurally guarantees "no clogging" regardless of match size.
MAX_FACTS_HIGH = 5
MAX_POINTER_TOPICS = 3

# Synthetic, non-user turns. The UserPromptSubmit hook fires on these too
# (background-task completions, harness reminders, slash-command stdout). They are
# not user intent and must never drive injection — historically 10/11 HIGH fires
# landed on <task-notification> prompts. Skipped turns are still LOGGED (tier
# "skipped") so the auditor can count what was suppressed.
SYNTHETIC_PREFIXES = (
    '<task-notification', '<system-reminder', '[Request interrupted',
    '<command-name', '<command-message', '<local-command',
)


def is_synthetic(prompt: str) -> bool:
    return prompt.lstrip().startswith(SYNTHETIC_PREFIXES)


def log_decision(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record['ts'] = dt.datetime.now().isoformat(timespec='seconds')
        with LOG_PATH.open('a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass  # logging must never break the turn


def maybe_shadow(prompt: str, project: str | None, session_id: str, res: dict) -> None:
    """Shadow mode: spawn a DETACHED process to re-score this prompt with the
    embedding scorer and log it next to the live lexical decision — a zero-risk
    real-traffic A/B. Off by default; on when state/shadow_retrieval.enabled
    exists or KB_SHADOW_RETRIEVAL is set. Never blocks or affects the turn."""
    import os
    if not (os.environ.get('KB_SHADOW_RETRIEVAL')
            or (BASE_DIR / 'state' / 'shadow_retrieval.enabled').exists()):
        return
    try:
        import subprocess
        import tempfile
        matches = res.get('matches', [])
        job = {
            'prompt': prompt, 'project': project, 'session_id': session_id,
            # Field names are historical ('lexical_*' = "the LIVE decision"); since
            # the 2026-07-29 rerank flip the live scorer varies, so live_mode records
            # which one actually produced these values — filter on it in analysis.
            'live_mode': res.get('mode'),
            'lexical_top_tier': res.get('top_tier'),
            'lexical_high': [m['slug'] for m in matches if m['tier'] == 'high'],
            'lexical_injected': (matches[0]['slug']
                                 if res.get('top_tier') == 'high' and matches else None),
        }
        fd, path = tempfile.mkstemp(prefix='kb_shadow_', suffix='.json')
        with os.fdopen(fd, 'w') as f:
            json.dump(job, f)
        # stderr -> logs/shadow.err (NOT DEVNULL): the detached shadow used to fail
        # silently — a missing embed model produced no row and no trace, invisible
        # until someone noticed the GPU wasn't spiking (cost a day, 2026-06-30).
        # shadow_retrieval._diag writes one actionable line here per failed turn;
        # a healthy shadow leaves this file empty.
        errlog_path = BASE_DIR / 'logs' / 'shadow.err'
        errlog_path.parent.mkdir(parents=True, exist_ok=True)
        errlog = open(errlog_path, 'a')
        try:
            subprocess.Popen(
                [sys.executable, str(BASE_DIR / 'scripts' / 'shadow_retrieval.py'), path],
                stdout=subprocess.DEVNULL, stderr=errlog, start_new_session=True,
            )
        finally:
            errlog.close()   # the child keeps its dup'd fd 2; safe to close ours
    except Exception:
        pass  # shadow must never affect the live turn


def main() -> None:
    # Skip KB-internal headless `claude -p` calls (nightly pipeline, harvester).
    # Those are not real user turns and must never receive injected memory; the
    # marker is set by kb.claude_code_generate and propagates to hooks it fires.
    import os
    if os.environ.get('KB_HEADLESS'):
        return
    # Fail-open from the very first line: nothing we do here may disrupt a session.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    prompt = (payload.get('prompt') or payload.get('user_prompt') or '').strip()
    cwd = payload.get('cwd') or ''
    if not prompt:
        return

    project = Path(cwd).name if cwd else None
    # Session correlation key. Lets the SessionEnd outcome scorer
    # (scripts/injection_outcome.py) join each injection DECISION to what
    # actually happened in the session — was the fact engaged with? was its
    # topic later corrected? Without this key, injections can't be evaluated,
    # so it is the substrate for the entire utility/reward signal. Falls back to
    # the transcript filename stem (== session id) when the field is absent.
    session_id = (payload.get('session_id')
                  or Path(payload.get('transcript_path') or '').stem or '')

    if is_synthetic(prompt):
        log_decision({
            'tier': 'skipped', 'project': project, 'session_id': session_id,
            'prompt_head': prompt[:120], 'reason': 'synthetic',
        })
        return

    try:
        import memory_index as mi
    except Exception:
        return

    try:
        res = mi.retrieve(prompt, project=project)
    except Exception as e:
        # No-dropped-row invariant (Task 9): fail-open on the TURN (inject nothing)
        # but never on the LOG — a vanished row is the silent-attrition class that
        # voided the first collection window.
        log_decision({
            'tier': 'error', 'project': project, 'session_id': session_id,
            'prompt_head': prompt[:120],
            'reason': f'retrieve failed: {type(e).__name__}: {e}'[:200],
        })
        return

    matches = res.get('matches', [])
    top_tier = res.get('top_tier', 'none')
    # Per-row scorer provenance: which scorer ACTUALLY produced this decision
    # (retrieval_mode), which config was ATTEMPTED (retrieval_config — the Task 10
    # pin), and the prompt length (prefill scales with it; the burn-in report
    # stratifies fallback rate by this). rerank_timeout appears only when true.
    provenance = {'retrieval_mode': res.get('mode'),
                  'retrieval_config': res.get('retrieval_config'),
                  'prompt_chars': len(prompt)}
    if res.get('rerank_timeout'):
        provenance['rerank_timeout'] = True

    # Out-of-band: log what the shadow scorer WOULD inject (live decision unchanged).
    maybe_shadow(prompt, project, session_id, res)

    if top_tier == 'high':
        top = matches[0]
        out = mi.format_directive(top, max_facts=MAX_FACTS_HIGH)
        log_decision({
            'tier': 'high', 'project': project, 'session_id': session_id,
            'prompt_head': prompt[:120],
            'injected': top['slug'], 'score': top['score'],
            'facts': len(top['key_points'][:MAX_FACTS_HIGH]),
            # The actual injected facts, so the outcome scorer and the future
            # eval set can measure the utility of CONTENT, not just the slug.
            'injected_facts': top['key_points'][:MAX_FACTS_HIGH],
            **provenance,
        })
        print(out)
        return

    if top_tier == 'moderate':
        # HIGH-ONLY mode: the moderate breadcrumb advertises the lazy
        # recall_memory tool, which does not exist yet, and fires very broadly
        # (one incidental token is enough). Until recall_memory ships, LOG the
        # moderate decision — the tier log is the tuning dataset — but inject
        # NOTHING. Flip this back on together with the lazy recall path.
        mods = [m for m in matches if m['tier'] == 'moderate'][:MAX_POINTER_TOPICS]
        log_decision({
            'tier': 'moderate', 'project': project, 'session_id': session_id,
            'prompt_head': prompt[:120],
            'advertised': [m['slug'] for m in mods],
            'top_score': mods[0]['score'] if mods else 0.0,
            'suppressed': True,  # logged for tuning, not injected (HIGH-only mode)
            **provenance,
        })
        return

    # No meaningful match: stay silent, but log the miss so recurring-correction
    # cross-referencing can later catch "fact existed but never surfaced".
    log_decision({
        'tier': 'none', 'project': project, 'session_id': session_id,
        'prompt_head': prompt[:120],
        'best_score': matches[0]['score'] if matches else 0.0,
        **provenance,
    })


if __name__ == '__main__':
    main()
