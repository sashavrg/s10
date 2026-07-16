# Hook tuning notes

Living notes for refining the Claude Code hooks wired into this KB
(`scripts/memory_inject_hook.py` and `scripts/claude_session_hook.sh`). When an
agent or a human observes a hook firing wrongly, record it here so the system can
be refined over time.

**For the full runtime wiring** (what calls what, which logs/config each touches,
and how the nightly auto-tune consumes the signal) see `docs/feedback-loop.md` —
the single current-state topology map. These files are the *notes*; that file is
the *map*.

**Hard rule:** these notes live in `docs/` and must NEVER be placed in
`raw/inbox/` — that path is ingested into topics and would pollute the KB. The
pipeline leaves `docs/` alone.

## Files

Only this README ships in the repo. The observation logs below are **local
working data** — they don't exist until observations accrue, and they are
gitignored (they inevitably quote your own prompts and session logs, so they
must never be committed). Create the first two by hand the first time you have
something to record, using the entry template below:

- `memory-injection.md` — the UserPromptSubmit memory-injection hook (human + agent notes).
- `correction-capture.md` — the SessionEnd correction-harvester hook.
- `auto-audit.md` — **machine-generated**; the nightly `scripts/audit_hooks.py`
  creates and appends to it. Do not hand-edit; add human observations to the
  per-hook files above.

## Entry template

```
### YYYY-MM-DD — <hook> — <one-line symptom>
**Symptom:** what was observed (what fired, where, why it was wrong).
**Log trace:** the relevant line(s) from logs/memory_injection.jsonl (or harvest.log).
**Root cause:** the mechanism, if known.
**Candidate fix:** the smallest change that would prevent it.
**Status:** open | applied | wontfix
```
