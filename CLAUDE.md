# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

§10: a local-first knowledge base that ingests notes, URLs, PDFs, and repo snapshots, summarizes them with a local Ollama LLM (optionally Claude cloud backends), and builds topic pages from the summaries. An optional feedback loop injects KB facts into Claude Code sessions and tunes itself from the logged outcomes. All processing stays on-device unless a sync target or cloud backend is configured. The pipeline runs nightly via cron. The name: see `docs/vision.md`.

## Common commands

All commands require the venv to be active first:

```bash
source .venv/bin/activate
```

**Run the full pipeline manually:**
```bash
python scripts/kb.py run
# or via the shell wrapper (adds model auto-pick, optional sync + Telegram):
./scripts/run_pipeline.sh
```

**Ingest content:**
```bash
python scripts/kb.py add-file /path/to/file.md --title "My Note"
python scripts/kb.py add-pdf /path/to/file.pdf --title "Paper"
python scripts/kb.py add-url "https://example.com"
python scripts/kb.py add-repo /path/to/project --title "Repo snapshot"
```

**Incremental operations (run individually when you want more control):**
```bash
python scripts/kb.py sync-inbox           # import files dropped in raw/inbox/
python scripts/kb.py detect-changes      # mark sources whose content changed
python scripts/kb.py summarize-changed
python scripts/kb.py curate-topics       # apply alias/ignore/manual rules from config/topics.yaml
python scripts/kb.py rebuild-affected
python scripts/kb.py review              # regenerate review/dashboard.md and review/open_questions.md
```

**Inspect state:**
```bash
python scripts/kb.py list
python scripts/kb.py list-topics
```

**Tests:**
```bash
pip install -r requirements-dev.txt   # one-time
pytest tests/
```
No Ollama or network calls — `tests/conftest.py` redirects `kb.py`'s module-level paths to a tmp dir per test.

## Architecture

**Single-file core:** All pipeline logic lives in `scripts/kb.py` — functions and an argparse `main()`, no packages. All paths derive from `BASE_DIR = Path(__file__).resolve().parent.parent`.

**Data flow:**
```
raw/inbox/      → sync-inbox         → raw/web/<source_id>.md
raw/web/        → summarize          → compiled/sources/<source_id>.md
compiled/sources/ → compile-topic    → compiled/topics/<slug>.md
compiled/topics/ → review            → review/dashboard.md + open_questions.md
```

**State:** `state/manifest.json` is the single source of truth for all tracked sources and topics (per-source content hash, topics, `needs_summary` / `needs_topic_rebuild` flags; per-topic slug, display name, source list). Always load/save via `load_manifest()` / `save_manifest()`.

**Change detection:** `content_hash` (SHA-256 of raw text) vs `last_seen_hash` in the manifest; a mismatch sets `needs_summary=True`.

**Topic curation:** `config/topics.yaml` (gitignored; copy from `config/topics.yaml.example`) controls `aliases`, `ignored`, `manual_topics`, and `pinned`. Run `curate-topics` after editing; it rewrites manifest topics in place and removes stale topic files.

**Ollama integration:** All local LLM calls go through `ollama_generate()`, which POSTs to `${KB_OLLAMA_URL:-http://127.0.0.1:11434}/api/generate`. Prompts live in `prompts/`. The model is passed at call time — no global model state.

**Cloud LLM backends (optional):** the dispatcher `llm_generate(primary, fallback, prompt, …)` parses `backend:model` specs — `claude:opus`, `claude:sonnet`, `ollama:<tag>`, or a bare tag (= ollama) — via `parse_model_spec`. Cloud calls run `claude -p --model <m> --output-format json` with the prompt on stdin, from a neutral cwd and with `ANTHROPIC_API_KEY` scrubbed so subscription OAuth (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, in `.env`) is used. On any `CloudLLMError` the dispatcher falls back to the local model and records the event in `state/last_run_report.json`; the wrapper turns these into one consolidated Telegram notice. Only per-source summaries and topic pages call an LLM; the review dashboard is deterministic assembly.

**Shell wrapper (`scripts/run_pipeline.sh`):** wraps `kb.py run` with model auto-pick (candidate lists probed against `/api/tags`; aborts loudly if no summary model resolves), optional sync, and Telegram notifications (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env`). Sync is opt-in: `KB_DASHBOARD_DIR` (local data dir) or `KB_REMOTE_HOST` + `KB_REMOTE_DATA_DIR` (rsync over SSH); with neither set the run is fully local. The wrapper self-logs to `logs/pipeline.log` via `tee -a` — the crontab entry must NOT add its own `>>` redirect.

**File format:** every file in `raw/web/` and `compiled/sources/` is YAML frontmatter (`---` delimited) + Markdown body. Write with `frontmatter()`, read with `read_frontmatter_doc()`.

## Document classification conventions

Markdown meant to be classified by the KB should carry an identity block in the first 10 lines — see `docs/classification-conventions.md` for the required fields per artifact type (repo docs, system docs, memory entries).

## Self-improvement feedback loop (optional runtime wiring)

Two Claude Code hooks (UserPromptSubmit memory injection, SessionEnd correction harvest + outcome scoring) log every decision, and a nightly job tightens the tuning from that signal. The wiring is not obvious from `~/.claude/settings.json` alone — the registered entrypoints spawn further scripts. Before assuming something "isn't wired," read the single current-state map: **`docs/feedback-loop.md`** (topology + per-layer detail + copy-paste "verify it's wired" checks). Update that map whenever you change the loop.

## Hook misfire notes

When one of the hooks fires wrongly (irrelevant `[memory: …]` injection, or the correction harvester capturing noise), append a dated entry to `docs/hook-tuning/<hook>.md` locally, using the template in `docs/hook-tuning/README.md` (those dated log files are your data — gitignored). NEVER put these notes in `raw/inbox/` — that path is ingested into topics. The nightly `scripts/audit_hooks.py` also auto-detects misfires from `logs/memory_injection.jsonl`.

## Data vs machinery (repo hygiene — READ BEFORE ADDING FILES)

This repo ingests personal notes/repos/PDFs that routinely contain secrets, so it enforces a hard split:

- **Machinery** = the system itself: `scripts/`, `tests/`, `prompts/`, `docs/`, config **templates** (`config/*.example`, plus the committed seed map `config/memory_tuning.yaml`), the eval harness + example cases (`evals/`), `hooks/`, and the root README/CLAUDE/LICENSE/requirements. This is what ships in git.
- **Data** = anything the system reads or produces: `raw/`, `compiled/`, `state/`, `logs/`, `review/`, `evals/results/`, `evals/cases.jsonl`, the real `config/topics.yaml` and `config/source_projects.yaml`, and `.env`. This is NEVER committed.

`.gitignore` is **default-deny** (`/*` then an allow-list): any new top-level entry is ignored until you consciously allow-list it with a `!` line. When in doubt, it's data — leave it out.

`.gitignore` can't protect a secret **pasted into a tracked machinery file** (a doc) — that is what the commit-time secret scan is for: `scripts/scan_secrets.py`, run by `hooks/pre-commit` (staged content) and `hooks/pre-push` (whole tree + pytest). Activate once per clone: `git config core.hooksPath hooks`. For a genuine false positive append `# pragma: allowlist secret` to the line. **Never paste a live credential into any tracked file** — reference it as `<REDACTED>` or `${ENV_VAR}`.

## Key constraints

- Model defaults in `kb.py`: `DEFAULT_MODEL` / `FALLBACK_MODEL` (`qwen2.5-coder:7b`) and `TOPIC_MODEL` (`qwen2.5:7b-instruct-q4_K_M`). The wrapper probes what's actually installed and falls through candidate lists (`KB_MODEL` / `KB_TOPIC_MODEL` override) down to `llama3.2:3b`; keep those lists in sync with `ollama list`.
- Context budgets: `DEFAULT_NUM_CTX=16384` (summaries) and `TOPIC_NUM_CTX=16384` (topic merge), passed via Ollama's `options.num_ctx`. On ~6 GB VRAM cards a 7B q4 model partially offloads to CPU — slower but fine for a nightly batch. Some reasoning models (e.g. qwen3) leak `<think>` traces into output; the wrapper has a leak detector.
- Repo snapshots cap at `MAX_REPO_FILES = 40` files and `MAX_FILE_CHARS = 5000` chars per file to keep prompts manageable.
- If NO summary model resolves, the wrapper aborts loudly (writes `last_error` to `state/sync_meta.json`, Telegram alert, non-zero exit) instead of running with a phantom tag.
