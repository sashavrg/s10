# §10 — the vision this tool is named after

Doc Type: vision
Status: aspirational — the pipeline and feedback loop below it are real; this
describes where they are headed.
Audience: contributors and curious users

## The one-paragraph version

Most "self-improving" AI systems optimize a proxy: a benchmark score, a reward
model, token overlap with a reference answer. Proxies drift from what anyone
actually wanted (Goodhart's law), and systems that optimize themselves against
a frozen objective converge — they stop finding anything new. §10 is a bet on
the inverted arrangement: the **operator** stays the ground truth, the system's
job is to keep producing *evidence* of whether it helped, and its health metric
is **learning progress** — the derivative of quality, not its level. A system
whose improvement rate hits zero is treated as dying even if its scores look
fine.

## The concrete substrate

This repository is the trial grounds, deliberately small enough to reason
about:

1. **A knowledge base** built from what the operator actually reads and writes
   (notes, papers, repos), summarized and organized into topics.
2. **A memory loop** that injects settled facts from that KB into the
   operator's working sessions — and logs, for every injection, whether it was
   engaged, ignored, or corrected.
3. **A nightly tightening pass** that turns those logs into tuning changes,
   conservatively (tighten-only, on a branch, behind a flag).

Each layer generates the ground truth for the layer above it. Nothing is graded
against a synthetic benchmark; everything is graded against what the operator
did next.

## Principles

- **Operator-grounded, not benchmark-grounded.** The eval set is built from
  real usage and hand labels, and it is allowed to be small. A score against 50
  honest cases beats a score against 5,000 synthetic ones.
- **Document the process as data.** Decisions, corrections, and misfires are
  recorded in machine-readable logs; the system's history is its training
  signal.
- **Measure the derivative.** The interesting number is not "how good is
  retrieval" but "is retrieval still getting better." Flat is a warning; the
  goal is a system that keeps finding profitable changes to itself.
- **Don't converge.** Curation and tuning must never collapse the system onto
  one narrow behavior. Rules are tighten-only where mistakes are cheap and
  reviewed where they aren't, and nothing gets a zero selection probability
  forever.
- **Autonomy with a small blast radius.** Every self-modification lands where
  it can be inspected and reverted: a dated branch, a flagged config, a log
  entry — never a silent in-place change.

## Where it stands

Layers 1–3 above ship in this repo today (pipeline, hooks, nightly audit).
What does *not* exist yet is the agenda level: the system choosing for itself
which of its weaknesses to work on next, ranked by expected learning progress.
That is the part still being designed, and the reason the project keeps its
measurement honest first — an agenda built on a broken signal would optimize
noise with confidence.
