Doc Type: reference
Status: current
Audience: agents + the operator
Environment: local-pc
Scope: the runtime self-improvement feedback loop (hooks → logs → nightly auto-tune)

# Runtime feedback loop — wiring map

> **This whole loop is OPTIONAL.** The core KB pipeline (ingest → summarize →
> topic pages → review) works without any of it. The loop only exists if you
> wire the two Claude Code hooks — `UserPromptSubmit` →
> `scripts/memory_inject_hook.py` and `SessionEnd` →
> `scripts/claude_session_hook.sh` — into your `~/.claude/settings.json`.
> Skip this doc entirely if you just want the pipeline.

**Why this doc exists:** the loop spans Claude Code hooks, two-hop shell chains,
detached background processes, and a nightly job. `~/.claude/settings.json` only
shows the *entrypoint* scripts, so reading it alone makes you conclude (wrongly)
that the outcome scorer and shadow collector "aren't wired." They are — through
scripts the entrypoints call. **Read this before reverse-engineering the wiring
from components.** Keep it current when you change the loop.

## The map

```
                         config/memory_tuning.yaml   state/memory_index.json
                          (thresholds, blocklist)     (compiled topics + facts)
                                    │  read by              │ read by
                                    ▼                       ▼
  [SessionStart] ── settings.json ──> scripts/warm_reranker.sh   (detached; preloads the
        session opens                 rerank judge model with the 2h keepalive so first-turn
                                      rerank is warm — semantics-preserving, Task 9 flip)

  [UserPromptSubmit] ── settings.json ──> scripts/memory_inject_hook.py
        every prompt                         │  retrieves through memory_retrieval.py
                                             │  (memory_index.py implements scoring); injects HIGH
                                             │  KB_RETRIEVAL selects lexical (default), embedding,
                                             │  hybrid or rerank. Rerank uses a bounded deadline
                                             │  with lexical fallback. Errors are logged.
                                             ├─> logs/memory_injection.jsonl   (EVERY decision; carries session_id)
                                             └─> if state/shadow_retrieval.enabled:
                                                   Popen(detached) scripts/shadow_retrieval.py
                                                     └─> logs/shadow_retrieval.jsonl
                                                         (+ rerank verdicts when state/shadow_rerank.enabled
                                                          or KB_SHADOW_RERANK=1)

  [SessionEnd] ── settings.json ──> scripts/claude_session_hook.sh   (entrypoint; reads stdin payload)
        session ends                   └─> nohup scripts/session_end.sh <transcript> <project>   (detached, SEQUENTIAL)
                                             ├─ 1) scripts/harvest_session.py  (time-boxed, cloud LLM)
                                             │        └─> corrections into raw/inbox/  (state/harvested_sessions.json)
                                             └─ 2) scripts/injection_outcome.py --session <transcript-stem>
                                                      joins THIS session's injection rows (by session_id) with the
                                                      transcript → engaged?/corrected? → logs/injection_outcomes.jsonl

  [cross-host] the two hooks above ALSO fire on the home server's interactive Claude
        sessions, writing to its own clone's logs/ (excluded from the cache rsync).
        run_pipeline.sh step 1b (PC only, KB_DASHBOARD_DIR unset) ──> scripts/merge_remote_logs.py
          rsyncs the server's logs/{memory_injection,injection_outcomes}.jsonl and
          dedupe-appends new rows into THIS machine's logs (idempotent; server/PC
          session_ids are disjoint so the PC SessionEnd scorer ignores foreign rows).
          Harvested corrections ride the raw/inbox symlink instead, so only these two
          logs need merging. Without this the loop learns from PC traffic only.
          SAME STEP also fetches the TRANSCRIPTS for merged outcome sessions that don't
          resolve locally → logs/transcript_archive/<remote-project-dir>/ (rsync of
          the server's ~/.claude/projects). Rows without a transcript are unjudgeable (A2): they
          count toward gate accrual but can never be labeled/judged — this stranded 8
          of 35 fresh HIGH rows at the 2026-07-29 trip. Steady state = no SSH call
          (already-resolvable sessions are skipped); --no-transcripts opts out.

  [nightly cron 22:00] ── run_pipeline.sh ──> scripts/audit_hooks.py        (step 6, opt-in KB_HOOK_AUTOTUNE)
                                             reads logs/memory_injection.jsonl   (synthetic-event false-HIGH signal)
                                             reads logs/injection_outcomes.jsonl (real-traffic false-HIGH signal)
                                             TIGHTENS config/memory_tuning.yaml (blocklist+= / threshold raise)
                                             → commits to branch auto-tune/<date> for HUMAN merge (never main directly)

                         run_pipeline.sh ──> scripts/topic_records.py       (step 7, unconditional; no git/config mutation)
                                             reads logs/injection_outcomes.jsonl (per-topic useful/harmful counts, BOTH tiers)
                                             REBUILDS state/topic_records.json idempotently
                                             (per-topic Beta confidence α/β + per-tier decay; BUILT, not yet consumed)

  [analysis, on demand] scripts/shadow_report.py  ← logs/shadow_retrieval.jsonl   (embedding/rerank A/B read)
                        evals/run_eval.py          ← evals/cases.jsonl             (golden-set scoring;
                                                     seed yours from evals/cases.example.jsonl)
                        scripts/rescore_outcomes.py  (offline, nightly-able) — re-scores EVERY session in
                          logs/memory_injection.jsonl whose transcript is still on disk, via the shared
                          scripts/outcome_matching.py proxies (scorer_version=2) → writes
                          logs/injection_outcomes_rescored.jsonl, the ANALYSIS dataset (union-merge
                          retention: a row survives even after its transcript later expires). Governance
                          consumers (audit_hooks.classify_outcomes, topic_records.py) deliberately keep
                          reading the LIVE logs/injection_outcomes.jsonl, not the rescored file.
                          Nightly judge pass: v2 rows are queued oldest-first and re-verdicted by
                          └→ scripts/engagement_judge.py (offline semantic ENGAGED/not-engaged verdict,
                             replacing the failed v2 token-overlap proxy) → scorer_version=3 + judge_version
                             stamped per row, capped at KB_JUDGE_MAX_CALLS/run — **ON since 2026-07-29**,
                             default 40/run in heartbeat.sh (the stage-1 gate PASSED at P=0.947/R=0.783,
                             the pre-committed condition for writing verdicts into the analysis dataset;
                             backlog was 903 v2 rows ≈ 23 nights to drain; rows beyond the cap stay v2 for
                             the next night). The production path injects
                             ej.production_generate — the SAME cloud model the gate validated
                             (JUDGE_MODEL_ID = claude-sonnet-5, test-pinned equal to gate_read's pin).
                             The module default generate_fn is OLLAMA, so this injection is load-bearing:
                             a local verdict stamped 'ej9-claude-sonnet' would corrupt the dataset.
                             A cloud failure returns None → the row stays v2; a 3 is never faked.
                             graft_previous_judgments() runs
                             UNCONDITIONALLY (even at max_calls=0) and carries a previously-judged row's
                             verdict forward verbatim when its judge_version is unchanged — a cached
                             fact, never re-rolled (topic_records-style idempotence).
                        scripts/scorecard.py       ← logs/injection_outcomes_rescored.jsonl if present, else
                          the live logs/injection_outcomes.jsonl (tier-stratified HIGH/MODERATE, Wilson CIs,
                          (slug,project)-keyed depth; Perplexity signals: A correctness-on-seen,
                          └→ scripts/recall_miss.py    D compounding curve, B recall [correction-grounded, via recall_miss.stats()]; C unavailable)
                             ← harvested corrections + memory_index + memory_injection.jsonl
                               (under-injection: a correction that retrieve() would inject HIGH but the session didn't surface;
                                stats() is the symmetric hits+misses form recall needs — see recall_miss.py docstring)
                          RULE: judged rows key on the (scorer_version, judge_version) PAIR, via
                          scorecard.current_rows() — never scorer_version==3 alone. A bounded re-judge
                          (the KB_JUDGE_MAX_CALLS cap above) legitimately leaves two judge_versions
                          coexisting on scorer_version=3 rows; keying on scorer_version==3 alone would
                          silently blend two judges' verdicts into one number.
                        scripts/label_sample.py    ← logs/injection_outcomes.jsonl + logs/injection_outcomes_rescored.jsonl
                          (hand-label validation: stratified sample incl. an uncapped HIGH-arm stratum,
                           written to evals/candidates/engagement-labels.md — DATA, never committed)
```

## Assistant-first eval review (2026-09-22)

Candidate mining remains deterministic; the assistant reviews relevance against
the KB and escalates uncertain decisions with reasons. Accepted labels carry
their reviewer, rationale and attestation. Promote them with:

```bash
python scripts/eval_foldin.py fold --reviewed evals/candidates/reviewed-accepted.jsonl
python scripts/eval_foldin.py mine --review-history evals/candidates/review-history.jsonl --batch-size 20
```

The reviewed import preserves the exact reviewed prompt, checks source identity,
fresh-HIGH exclusion and known slugs, and only adds train/validation cases.
Held-out sessions are rejected; existing session splits remain pinned. A repeated
import is a no-op unless its label conflicts with the existing case, which fails.
Mining skips tool notifications/skipped turns and held-out sessions, prioritizes
positive proposals, and omits every event in the supplied review history. Supply
each prior audit file with another `--review-history` argument for later batches.
Use `--reviewed` for assistant labels; the legacy Markdown checklist fold assumes
operator decisions. Audit files, golden cases and their provenance stay local and
ignored in this public repository. Seed `evals/cases.jsonl` from the example file
before importing. These judgments add no operator/system agreement pairs and
provide no independent held-out certification.

Reviewed JSONL rows require `id` (the candidate ID), `src` with `session_id` and
`ts`, `project`, the exact `prompt`, `expect` (known topic slugs), `kind`, `reviewer`,
`reason`, and `basis`. Use `provenance: "agent-review"` with
`operator_attested: false`. An operator-adjudicated label instead uses
`provenance: "operator-reviewed"`, `operator_attested: true`, and an
`operator_adjudication` record; record any recommendation the operator saw.
A review-history row only needs the `src` event identity, so rejected and parked
candidates can be excluded alongside accepted ones.

## Window scorecard (2026-09-21 fix)

`scorecard.py --window START --until END` selects an inclusive start and exclusive
end using local ISO dates/timestamps. Depth is computed over the full accrual
history across tiers before selecting a window, tier or judge version. Helpers
preserve already-enriched depth; mixed raw/enriched inputs raise rather than
silently discarding pre-window history.

The CLI defaults to `scorer_version=3` plus the current `JUDGE_VERSION` (override
with `--judge-version`). `--all-versions` is explicitly diagnostic. Both the
report and tripwire use the live outcome log for event identity and exposure;
duplicate `(session_id, ts, injected)` writes count once, retaining the latest
annotation. A rescored row must also match the live event's project and tier.
Reconstructed rows absent from the live log are reported as analysis-only, not
allowed to redefine the registered accrual population.

The report distinguishes **accrued repeat HIGH events**, **analyzed repeat HIGH
events**, and **unjudged/excluded events**. These are not interchangeable when the
judge lags. It also reports analysis-only rows, scope/tier mismatches and duplicate
log writes. The depth≥1 injection-lift comparison is printed explicitly; the
all-depth difference remains a descriptive metric. Window reads omit all-time
correction recall because that separate population has no window selector yet.

`--json` returns these fields for audit. Explicit `--log PATH` uses that file for
both analysis and history unless `--history-log PATH` is supplied; omitted `--log`
uses the live log for history and the rescored file for analysis. This applies to
`--window-count` too, fixing the old explicit-default-path ambiguity.

The tripwire stays disarmed without `state/window_open`. Register a window and
its decision criteria before collecting evidence; rerunning a report does not
freeze a historical dataset or open a new experiment.

## Retrieval interface (R seam, 2026-09-21)

All retrieval callers use `scripts/memory_retrieval.py`: the prompt hook, Codex
MCP recall, CLI query, shadow passes, eval harness, recall-miss detector and tune
gate. `memory_index.py` remains the index builder and scoring implementation.
Update and consolidation remain in `kb.py`; no schema migration is involved.

`retrieve(query, project=None, max_facts=6, tuning=None, limit=50)` is the default
callable. A `MemoryRetriever` instance exposes the same method with its own paths
and configuration:

```python
from pathlib import Path
from memory_retrieval import MemoryRetriever

root = Path('/tmp/kb-experiment')
retriever = MemoryRetriever(
    index_path=root / 'state/memory_index.json',
    tuning_path=root / 'config/memory_tuning.yaml',
    embedding_path=root / 'state/topic_embeddings.json',
    mode='lexical',
)
result = retriever.retrieve('widget cache', project='example')
```

- Omitted paths use live defaults; specify all three for an isolated store.
  Explicit missing paths use an empty index, default tuning, or lexical fallback
  respectively, never the live file. A malformed index raises to the caller.
- Tuning precedence: per-call dict → instance dict → file merged over defaults.
  Dicts are complete tuning configs, matching the existing engine contract.
- Omitted mode reads `KB_RETRIEVAL` at call time. The tune gate pins lexical and
  shadows select embedding/rerank explicitly, without mutating that environment
  variable. Backend service settings and other embedding/rerank knobs retain
  their existing environment configuration.
- Reads are fresh on each call and never build or write an index/cache. The
  returned dict retains `query`, `project`, `mode`, `retrieval_config`,
  `rerank_timeout`, `top_tier`, and ranked `matches` with their facts and paths.
- Injection policy stays with callers. `project=None` retains the engine's broad
  legacy search; Codex MCP still filters to global notes when project is omitted.

Tests cover store/config isolation, file refresh, all four modes and backend failures.

## Tuning proposal review

The nightly auditor only adds demotions or raises thresholds. If evidence for an
existing demotion ages out, it reports a suggestion for human review. Suggestions
alone do not create a branch. Audit notes contain captured prompts and stay local;
only tuning configuration is committed to an `auto-tune/<date>` proposal branch.

`scripts/tune_gate.py` records a deterministic shadow verdict for each proposal:
false HIGH injections on negatives must not rise, no labeled positive HIGH hit may
be lost, and every tightening needs real traffic evidence. It uses your local
`evals/cases.jsonl`, excludes `held_out`, and pins lexical retrieval. Missing cases
produce a gate error rather than treating the synthetic example set as evidence.
The gate records its verdict; it does not merge. Loosenings require human review.

```bash
python scripts/tune_gate.py --branch auto-tune/YYYY-MM-DD --no-record
python scripts/tune_gate.py adjudicate --branch auto-tune/YYYY-MM-DD --decision merge --note 'reason for this decision'
```

The second command records an operator decision, not a Git merge. Verdicts and
agreement records stay in ignored `state/`. Assistant-reviewed eval labels do not
count as independent operator/system agreement pairs.

## The outcome loop, by layer (the part most easily mis-read)

The reward/utility signal is produced and consumed in three layers. All three are
live; if outcomes look thin, it's youth (session_id logging is recent), not a wiring break.

- **L1 — production:** `injection_outcome.py` runs at SessionEnd, but **two hops deep**
  (`claude_session_hook.sh` → `session_end.sh` → `injection_outcome.py`). It is the
  *second* step of `session_end.sh`, after the correction harvester, so it can see a
  correction harvested the same session. Output: `logs/injection_outcomes.jsonl`
  (`engaged_in_assistant=False` on a HIGH inject = likely off-topic false positive).
- **L2 — join key:** the scorer joins on `session_id`. `memory_inject_hook.py` stamps
  `session_id` on every `memory_injection.jsonl` row; `session_end.sh` passes the
  transcript-filename stem as `--session`. These must match (they do — the stem *is*
  the Claude Code session id). Old rows predate session_id logging → unjoinable.
- **L3 — consumption:** `audit_hooks.classify_outcomes()` scores each HIGH outcome as
  `useful` (engaged AND NOT corrected), `harmful` (engaged AND corrected), or non-useful;
  `decide()` demotes a slug injected HIGH ≥ `min_samples` times with **zero useful**
  outcomes in the window. Demotions go to the **reversible `outcome_demoted`** set
  (add-only proposals; aged-out evidence becomes a human-review suggestion), *not* the
  permanent `high_ineligible`. Added 2026-06-30; before that, auto-tune's only false-HIGH
  signal was synthetic-event injections (which dried up once the hook skipped `<task-notification>`).

> **`engaged` ≠ utility — design notes (2026-06-30 readiness review, gates 2–3, now applied).**
> - **engaged-but-harmful (seductive wrong):** `engaged AND corrected` — the model took the bait on a
>   confidently-wrong inject and the user corrected it. Scored as **non-useful** (`useful := engaged
>   AND NOT corrected`), so it can never *spare* a slug from demotion. **Phase 2's objective must also
>   weight `harmful` strictly negative**, never zero/positive.
> - **silent-save (unengaged-but-valuable):** an inject that prevents a mistake produces *no* visible
>   engagement (the counterfactual error never happened). This case is **genuinely unmeasurable** from
>   these fields and sits in the non-useful bucket — an honest limitation, not papered over. A slug is
>   only demoted on **≥ min_samples** zero-useful outcomes, so one unmeasurable save won't condemn it,
>   and a human can reverse a demotion after reviewing the evidence.
> Demotions use `outcome_demoted`; removing one requires human review. The nightly
> auditor adds to this set and reports aged-out evidence as a suggestion.

## The per-topic record (front #2 — BUILT 2026-06-30, not yet consumed)

A **second** nightly consumer of the same `injection_outcomes.jsonl` signal,
`scripts/topic_records.py`, maintains `state/topic_records.json` — one record per
topic that is simultaneously **L1 revealed-preferences** (`reinforcement_count`,
`last_reinforced`) and the **governance substrate** (per-topic Beta
confidence `alpha`/`beta`, per-tier `decay_lambda`). It is the continuous successor
to the binary `outcome_demoted`: where `audit_hooks` asks "zero useful → demote?",
the record carries `confidence = α/(α+β)` *with an evidence count*, so a
contested-but-sometimes-useful slug (e.g. 3 useful / 2 harmful → conf 0.58) is
legible instead of binary-passing.

- **Idempotent by construction.** `alpha = 0.5 + w·useful`, `beta = 0.5 + w·harmful`,
  `decay_lambda = min(base·2^harmful, 0.05)` — all pure functions of cumulative
  counts over the (append-only) log, never incremented in place. Re-running the
  nightly yields a byte-identical file (verified on real data). No watermark; this
  keeps the evidence record reproducible while the auditor proposes add-only demotions.
- **Verdict in lockstep** with `audit_hooks.classify_outcomes`: `useful = engaged
  AND NOT corrected`, `harmful = engaged AND corrected`, else neutral. Difference:
  the record counts **both** high+moderate injects as evidence (more posterior
  signal), where `audit_hooks` filters HIGH-only (its concern is false-HIGH demotion).
- **Built, NOT consumed.** v1 maintains the record only. Wiring `recency`/`confidence`
  into retrieval scoring, archival/supersession, and the `outcome_demoted →
  confidence-floor` migration are the next layers — deliberately deferred until
  confidence is trusted ("don't outrun trust; don't rip out what works"). The binary
  C2/C3 demotion keeps running untouched meanwhile.

## Shared config + state flags

| File | Role | Read by |
|------|------|---------|
| `config/memory_tuning.yaml` | thresholds, `high_ineligible` blocklist, `auto_tune` knobs | `memory_index.py` (live scoring), `audit_hooks.py` (tightening) |
| `state/memory_index.json` | compiled topics + key-point facts | `memory_index.py` |
| `state/shadow_retrieval.enabled` | flag: turn embedding-shadow on | `memory_inject_hook.py` |
| `state/shadow_rerank.enabled` (or `KB_SHADOW_RERANK=1`) | flag: add a rerank pass in shadow | `shadow_retrieval.py` |
| `state/harvested_sessions.json` | which sessions were harvested (+ note path) | `harvest_session.py`, `injection_outcome.py` |
| `state/topic_records.json` | per-topic Beta confidence (α/β) + decay substrate (front #2) | `topic_records.py` (writes); no live reader yet |
| `logs/injection_outcomes_rescored.jsonl` | v2 ANALYSIS dataset (union-merge retained); written by `rescore_outcomes.py`, offline/nightly-able | `scorecard.py` (default `--log` when present), `label_sample.py` |

## Verify it's wired (don't re-derive — run these)

```bash
# UserPromptSubmit + SessionEnd entrypoints registered:
python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks'])"
# the SessionEnd chain actually calls the scorer (grep the two shell hops):
grep -n injection_outcome scripts/session_end.sh
# cross-host merge (PC only): what the server's interactive sessions WOULD contribute
# (idempotent — a real run appends; re-run is +0, and `transcripts+0 (all resolvable)`
# means no SSH listing was even needed). Needs SSH to the home server:
python scripts/merge_remote_logs.py --dry-run
# gate accrual: the JUDGEABLE count (A2 selection), not raw fresh HIGH — the tripwire
# delegates to gate_dossier so the two definitions cannot drift:
python scripts/gate_dossier.py count && ./scripts/gate_accrual_check.sh --count-only
# compounding-read tripwire (fires once at the reopened window's read point,
# depth>=1 HIGH >= 40; silent until state/window_open exists — Task 10 Step 4).
# Counts via scorecard --window-count, i.e. the same enrich_depth() the curve uses.
# Run from heartbeat.sh; --count-only is the safe manual probe:
./scripts/compounding_read_check.sh --count-only
KB_WINDOW_OPEN=2026-07-01 ./scripts/compounding_read_check.sh --count-only   # dry-run a date
# the scorer is producing rows (status lines):
tail -3 logs/injection_outcome.log
# auto-tune consumes both signals + what it WOULD do (no commit):
python scripts/audit_hooks.py --dry-run
# per-topic records rebuilt from the outcome log (idempotent — re-run is a no-op diff):
python scripts/topic_records.py && python scripts/topic_records.py
# offline re-scorer — rebuilds the v2 analysis dataset from live transcripts (idempotent,
# union-merge retained: prints a carried_forward count for sessions whose transcript expired):
python scripts/rescore_outcomes.py
# ^ wired-check for the judge pass: look for the `judge [<label>]: grafted N, judged N,
#   failed(stay v2) N, remaining N` line. `grafted` must be >0 whenever previously-judged
#   rows exist, even with the judge disabled (KB_JUDGE_MAX_CALLS=0, the pre-gate default) —
#   graft runs unconditionally; only the judging loop is gated by max_calls.
# production scorecard — tier-stratified (HIGH/MODERATE), A correctness-on-seen,
# B recall (via recall_miss.stats()), D compounding curve; prints the scorer_version mix first:
python scripts/scorecard.py
# the under-injection events feeding recall (a correction retrieve() would inject HIGH but we didn't):
python scripts/recall_miss.py
# hand-label validation sample (stratified incl. an uncapped HIGH-arm stratum) -> evals/candidates/:
python scripts/label_sample.py make
# eval set: (re)seed train/val/held_out splits (idempotent), then run stratified by split + project×kind:
python scripts/eval_split.py && python evals/run_eval.py run --label lexical
# shadow collection state:
ls state/shadow_*.enabled 2>/dev/null; python scripts/shadow_report.py | head
# shadow's MODEL deps — a missing embed model makes shadow write NOTHING (it embeds
# first, then falls back to lexical and bails). No GPU spike on prompts = this:
ollama list | grep -E 'nomic-embed-text|qwen2.5:7b-instruct' || echo "MISSING a shadow model -> ollama pull it"
# shadow failures now self-report here (empty = healthy; one line per failed turn = broken):
tail -5 logs/shadow.err 2>/dev/null
```

> **Silent-failure note (2026-06-30):** shadow runs detached. Until this date its
> stderr went to `/dev/null`, so a vanished `nomic-embed-text` produced no rows and
> no trace — invisible until the GPU stopped spiking. Now `shadow_retrieval._diag`
> writes one actionable line to `logs/shadow.err` per failed turn, and the spawn in
> `memory_inject_hook.py` routes stderr there. **If shadow looks dead, read that file
> and run the `ollama list` check above first.**

## Pointers
- Per-hook misfire notes + entry template: `docs/hook-tuning/README.md`.
- Auto-tune internals + tests: `scripts/audit_hooks.py`, `tests/test_audit_hooks.py`.
