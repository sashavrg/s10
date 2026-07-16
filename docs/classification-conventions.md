# Classification Conventions

Purpose: make topic classification resilient even when notes or docs are surfaced without their parent directory path.

## Decision

Use a lightweight identity block near the top of each markdown document, not mandatory YAML frontmatter.

Reasoning:
- easier to retrofit into existing docs
- readable in terminals and GitHub previews
- easy for classifiers to parse with simple line matching
- low-friction for hand-written operational notes

YAML frontmatter is allowed for systems that already use it, but the plain-text identity block is the required baseline.

## Required fields by artifact type

### 1. Repo docs

Add these fields within the first 10 lines:

```md
Repo: acme-portal-api
Path: api/docs/CONFIG-SYSTEM.md
Audience: engineers and handoff readers
Status: canonical
Doc Type: architecture
Environment: repo-only
```

Required:
- `Repo`
- `Audience`
- `Status`
- `Doc Type`
- `Environment`

Recommended:
- `Path`
- `Source of Truth`
- `Owner`

### 2. System docs

Add these fields within the first 10 lines:

```md
Environment: local-pc
Hostname: operator-pc
Project: system-docs
Status: active
Doc Type: runbook
```

Required:
- `Environment`
- `Status`
- `Doc Type`

Recommended:
- `Hostname`
- `Project`
- `System`

### 3. Shared notes / cross-machine docs

```md
Environment: cross-environment
Project: shared-team-docs
Status: active
Doc Type: implementation-note
```

If a note covers multiple machines, say so explicitly and split the body into sections per environment.

### 4. Hermes memory entries

Each entry must begin with bracketed tags.

Format:
```text
[environment: home-server] [scope: service] [project: homeserver-infra] Note text here.
```

Required:
- `environment`
- `scope`

Recommended:
- `project`
- `hostname`
- `access`

## Allowed values

### Environment
- `local-pc`
- `home-server`
- `remote-vast`
- `remote-other`
- `repo-only`
- `cross-environment`
- `unknown`

### Scope
- `system`
- `service`
- `repo`
- `workflow`
- `infra`
- `memory`

### Status
- `canonical`
- `active`
- `historical`
- `scratch`
- `archived`

### Doc Type
- `runbook`
- `architecture`
- `handoff`
- `api-doc`
- `implementation-note`
- `backlog`
- `test-report`
- `scratch`
- `memory`

### Project
- `acme-portal-api`
- `dashboard`
- `dashboard-api`
- `homeserver-infra`
- `system-docs`
- `llm-kb`
- `shared-team-docs`
- `none`

(This list is illustrative — replace it with your own project names.)

## Classification rules

1. Prefer explicit identity block or YAML frontmatter.
2. Then parse marker lines like `Repo:`, `Environment:`, `Hostname:`, `Status:`.
3. Then use stable keywords in content.
4. Use file path only as a fallback hint.
5. If still ambiguous, classify as low-confidence instead of guessing.

## Confidence downgrade rule

If content does not identify repo or environment clearly enough:
- `environment=unknown`
- `project=unknown`
- `confidence=low`

## Filename policy

High-risk generic names must carry a strong identity block:
- `README.md`
- `TODO.md`
- `TEST-RESULTS.md`
- `CLAUDE.md`
- `HANDOVER.md`

Rename only when there is a clear benefit. Default to keeping filenames stable and strengthening headers.

## Writing guidance

- Put identity markers before narrative text.
- Keep values stable and enumerable.
- Prefer one primary environment per doc.
- Mark historical docs explicitly instead of rewriting all content.
- For repo docs, name the repo in the opening block even if the path already implies it.

## Examples

### Good repo doc opening

```md
# Config System

Repo: acme-portal-api
Path: api/docs/CONFIG-SYSTEM.md
Audience: engineers working on the config-driven system
Status: canonical
Doc Type: architecture
Environment: repo-only
```

### Good system doc opening

```md
# Local LLM Integration

Environment: local-pc
Hostname: operator-pc
Project: system-docs
Status: active
Doc Type: implementation-note
```

### Good memory entry

```text
[environment: remote-vast] [scope: service] [project: none] Vast.ai instance <ID> uses ssh4.vast.ai:<PORT> and hosts ComfyUI at /workspace/ComfyUI.
```
