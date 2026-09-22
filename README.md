# §10

Local-first, self-improving knowledge base. It ingests your notes, URLs, PDFs,
and repo snapshots, summarizes them with a local Ollama LLM (optionally cloud
models), builds topic pages from the summaries — and, if you opt in, runs a
closed feedback loop that injects relevant memories into your Claude Code
sessions and tightens its own tuning from the observed outcomes.

The name is the section of the project's long-horizon roadmap this tool exists
to reach: an operator-grounded system that improves itself from evidence of
what actually helped, not from proxy metrics. See [docs/vision.md](docs/vision.md).

Everything runs on your machine. Nothing leaves it unless you configure a sync
target or a cloud model backend.

## What the core pipeline does

- Ingests web URLs, local text/Markdown files, PDFs, and repo/folder snapshots into `raw/web/`.
- Imports `.md` / `.txt` files dropped into `raw/inbox/`.
- Summarizes only new or changed sources (content-hash change detection).
- Applies topic alias, ignore, manual-topic, and pinned-name rules from `config/topics.yaml`.
- Rebuilds only affected topic pages in `compiled/topics/`.
- Generates a review dashboard and open-questions list in `review/`.
- Tracks all source and topic metadata in `state/manifest.json`.
- Runs nightly from cron via `scripts/run_pipeline.sh`.

## Quick start

Requires Python 3.11+ and [Ollama](https://ollama.com).

```bash
# 1. Clone and enter
git clone https://github.com/sashavrg/s10.git && cd s10

# 2. Create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Activate the git hooks (secret scan on commit/push — see "What is NOT tracked")
git config core.hooksPath hooks

# 4. Copy the example configs
cp config/topics.yaml.example config/topics.yaml

# 5. Pull the default models (or any model you prefer — see "Choosing models")
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5:7b-instruct-q4_K_M

# 6. Ingest something and run the pipeline
python scripts/kb.py add-url "https://example.com/interesting-article"
python scripts/kb.py run
```

Results land in `compiled/sources/` (per-source summaries), `compiled/topics/`
(merged topic pages), and `review/dashboard.md`.

## Ingesting content

```bash
python scripts/kb.py add-file /path/to/file.md --title "Note"
python scripts/kb.py add-pdf /path/to/file.pdf --title "Paper"
python scripts/kb.py add-repo /path/to/project --title "Repo snapshot"
python scripts/kb.py add-url "https://example.com"
```

Or just drop `.md` / `.txt` files into `raw/inbox/` — the next run imports them.

## Topic curation

Edit `config/topics.yaml` (not tracked by git — see `config/topics.yaml.example`).

Supported sections:
- `aliases:` map noisy slugs to canonical slugs
- `ignored:` drop topics completely
- `manual_topics:` force topics onto a specific source_id
- `pinned:` define the preferred display name for a canonical slug

Apply curation rules:
```bash
python scripts/kb.py curate-topics
python scripts/kb.py rebuild-affected
python scripts/kb.py review
```

## Useful commands

```bash
python scripts/kb.py sync-inbox        # import files dropped in raw/inbox/
python scripts/kb.py detect-changes    # mark sources whose content changed
python scripts/kb.py summarize-changed # re-summarize only what changed
python scripts/kb.py curate-topics     # apply config/topics.yaml rules
python scripts/kb.py rebuild-affected  # rebuild only affected topic pages
python scripts/kb.py review            # regenerate review/dashboard.md
python scripts/kb.py list              # all tracked sources
python scripts/kb.py list-topics       # all topics
```

Every command that calls an LLM accepts `--model <tag>` to override the default.

## Choosing models

Defaults: `qwen2.5-coder:7b` for per-source summaries,
`qwen2.5:7b-instruct-q4_K_M` for topic merging. Any Ollama model works — pass
`--model`, or set `KB_MODEL` / `KB_TOPIC_MODEL` for the wrapper.

Rules of thumb:
- A 7B model at q4 wants ~5 GB of VRAM; on smaller cards Ollama offloads layers
  to CPU — slower but fine for a nightly batch.
- Lighter/faster: `llama3.2:3b` (the wrapper's last-resort fallback).
- Some reasoning models (e.g. qwen3) can leak `<think>` traces into output;
  the wrapper detects this and warns.
- Point at a remote Ollama with `KB_OLLAMA_URL=http://host:11434`.

### Optional: cloud model backends

`kb.py` can route generation to Claude models via the Claude Code CLI instead
of Ollama, with automatic fallback to your local model on quota/auth/network
errors. Model specs look like `claude:sonnet`, `claude:opus`, or
`ollama:<tag>`; a bare tag means Ollama. Requirements: the `claude` binary on
PATH and a working subscription login. The wrapper probes a short cloud call
before selecting cloud backends, so authentication or entitlement failures produce
one diagnostic and local fallback. An ambient CLI login works; an optional
`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` overrides that login, so remove
stale tokens from `.env`. Override nightly specs with `KB_SUMMARY_SPEC` /
`KB_TOPIC_SPEC`.

## Nightly runs

```cron
0 22 * * * /path/to/s10/scripts/run_pipeline.sh
```

The wrapper self-logs to `logs/pipeline.log` — do **not** add a `>>` redirect.
It picks the best locally-available model, runs the pipeline, and (only if
configured) syncs with a second machine and sends a Telegram notice.

Optional `.env` keys:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — one consolidated notice per run
- `CLAUDE_CODE_OAUTH_TOKEN` — optional subscription token override; ambient CLI login also works
- `KB_REMOTE_HOST` + `KB_REMOTE_DATA_DIR` (+ `KB_REMOTE_USER`) — rsync-over-SSH
  sync with another machine (inbox pull + cache publish). Unset = fully local.
- `KB_DASHBOARD_DIR` — sync against a local data dir instead (e.g. when the
  pipeline runs on the machine that also hosts your dashboard)
- `KB_JUDGE_MAX_CALLS` — nightly cap on cloud (Sonnet) judge calls in the
  heartbeat's outcome re-scoring pass; default `40`, `0` disables the pass
- `KB_HARVEST_BACKFILL_CAP` — nightly cap on cloud calls spent harvesting
  corrections from the pre-hook transcript backlog; default `60`

## Optional: the self-improvement feedback loop

The differentiating layer, for Claude Code users: two hooks log every
memory-injection decision and its outcome, and a nightly job tightens the
retrieval tuning from that signal.

- `UserPromptSubmit` hook — retrieves settled facts from your KB relevant to
  the prompt and injects them as `[memory: …]` context, scoped by project
  (`config/source_projects.yaml`, see the `.example`).
- `SessionEnd` hook — harvests explicit corrections you made during the
  session and scores whether injected memories were actually engaged.
- Nightly audit — classifies misfires from the logs and (behind
  `KB_HOOK_AUTOTUNE=1`, tighten-only, on a dated branch) updates
  `config/memory_tuning.yaml`.

The core pipeline works without any of this. To wire it up, start at
[docs/feedback-loop.md](docs/feedback-loop.md) — the current-state map of the
loop with per-layer verification commands. Retrieval quality is measurable
with the eval harness: copy `evals/cases.example.jsonl` to `evals/cases.jsonl`,
replace the synthetic cases with real ones from your own KB, and run
`python evals/run_eval.py run`.

### Optional agent integration

`scripts/codex_memory_mcp.py` is a local stdio MCP server exposing `recall_memory`.
Configure your client to launch it with Python and an absolute script path.
Calls accept `query`, optional `project`, and `max_facts` (1–8). Omit `project` to
return global notes only. It reads your local index and does not build it.

`config/codex-hooks.json.example` provides opt-in prompt and session-end hook
commands for clients supporting those events. Merge the template into your client
configuration; the repository does not activate hooks automatically. Both hooks
resolve scripts relative to the current Git repository, so the template is for
sessions launched from this S10 checkout. The shared transcript reader supports
Claude Code message records and Codex rollout message records.

### Reviewing retrieval cases

Use `scripts/eval_foldin.py mine --batch-size 20` to prepare a checklist, then
review relevance against your KB. Mining prioritizes positive proposals and skips
synthetic turns. Supply previous decision files with `--review-history PATH` to
avoid reviewing the same source events again. Import assistant-reviewed labels
with `fold --reviewed PATH`; reviewer, reasoning and operator attestation are
preserved, and additions are limited to train/validation. See
[the workflow and scorecard reference](docs/feedback-loop.md).

## What is NOT tracked (by design)

This repo enforces a hard **data vs machinery** split: only the *system*
(machinery) ships in git; your *data* never does. `.gitignore` is **default-deny**
— every new top-level entry is ignored until it's explicitly allow-listed, so a new
data directory can't accidentally get committed.

Never tracked (data):
- `raw/` — ingested source files
- `compiled/` — summarized sources and topic pages
- `review/` — dashboard output
- `state/` — manifest and metadata
- `logs/` — pipeline and hook logs
- `evals/results/` — regenerated eval runs
- `evals/cases.jsonl` — your golden cases (quote your own sessions)
- `config/topics.yaml`, `config/source_projects.yaml` — your real config
  (use the `.example` files as starting points)
- `.env` — credentials

### Secret scanning (the second guard)

`.gitignore` keeps *data* out of git, but it can't stop a secret pasted into a
*tracked* file (a doc). So `scripts/scan_secrets.py` runs as a git hook —
`hooks/pre-commit` scans staged changes, `hooks/pre-push` scans the whole tree and
runs the tests. Activate once per clone:

```bash
git config core.hooksPath hooks
```

A real false positive? Append `# pragma: allowlist secret` to the line. Need to
bypass once? `git commit --no-verify`. And never paste a live credential into a
tracked file — use `<REDACTED>` or `${ENV_VAR}`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

No Ollama or network calls — the suite redirects all paths to a tmp dir per test.

## License

[GPLv3](LICENSE).
