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
import sys
import datetime as dt
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))

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
    except Exception:
        return

    matches = res.get('matches', [])
    top_tier = res.get('top_tier', 'none')

    # Out-of-band: log what the embedding scorer WOULD inject (still inject lexical).
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
        })
        return

    # No meaningful match: stay silent, but log the miss so recurring-correction
    # cross-referencing can later catch "fact existed but never surfaced".
    log_decision({
        'tier': 'none', 'project': project, 'session_id': session_id,
        'prompt_head': prompt[:120],
        'best_score': matches[0]['score'] if matches else 0.0,
    })


if __name__ == '__main__':
    main()
