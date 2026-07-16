#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests
import trafilatura
import yaml
from pypdf import PdfReader

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_WEB_DIR = BASE_DIR / 'raw' / 'web'
RAW_INBOX_DIR = BASE_DIR / 'raw' / 'inbox'
COMPILED_SOURCES_DIR = BASE_DIR / 'compiled' / 'sources'
COMPILED_TOPICS_DIR = BASE_DIR / 'compiled' / 'topics'
REVIEW_DIR = BASE_DIR / 'review'
STATE_DIR = BASE_DIR / 'state'
LOGS_DIR = BASE_DIR / 'logs'
CONFIG_DIR = BASE_DIR / 'config'
TOPIC_CONFIG_PATH = CONFIG_DIR / 'topics.yaml'
SOURCE_PROJECTS_PATH = CONFIG_DIR / 'source_projects.yaml'
MANIFEST_PATH = STATE_DIR / 'manifest.json'
SOURCE_PROMPT_PATH = BASE_DIR / 'prompts' / 'source_summary.txt'
TOPIC_PROMPT_PATH = BASE_DIR / 'prompts' / 'topic_merge.txt'
DEFAULT_MODEL = 'qwen2.5-coder:7b'
FALLBACK_MODEL = 'qwen2.5-coder:7b'
TOPIC_MODEL = 'qwen2.5:7b-instruct-q4_K_M'
DEFAULT_NUM_CTX = 16384
TOPIC_NUM_CTX = 16384
# Cap sources fed into a single topic merge. Without this, gather_topic_inputs
# grows unbounded as sources accumulate and eventually overflows TOPIC_NUM_CTX,
# silently dropping whichever sources fall off. Capping + newest-first sort makes
# the drop explicit (oldest go) instead of emergent.
MAX_TOPIC_INPUTS = 24
TEXT_FILE_SUFFIXES = {'.md', '.txt', '.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.yaml', '.yml', '.toml', '.cfg', '.ini', '.html', '.css', '.scss', '.sass', '.sh', '.bash', '.zsh', '.env', '.sql', '.csv'}
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next', '.cache'}
MAX_REPO_FILES = 40
MAX_FILE_CHARS = 5000
DEFAULT_TOPIC_CONFIG = {
    'aliases': {},
    'ignored': [],
    'manual_topics': {},
    'pinned': {},
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def ensure_dirs() -> None:
    for path in [RAW_WEB_DIR, RAW_INBOX_DIR, COMPILED_SOURCES_DIR, COMPILED_TOPICS_DIR, REVIEW_DIR, STATE_DIR, LOGS_DIR, CONFIG_DIR]:
        path.mkdir(parents=True, exist_ok=True)
    if not TOPIC_CONFIG_PATH.exists():
        TOPIC_CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_TOPIC_CONFIG, sort_keys=False))


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {'sources': [], 'topics': []}
    manifest = json.loads(MANIFEST_PATH.read_text())
    manifest.setdefault('sources', [])
    manifest.setdefault('topics', [])
    return manifest


def save_manifest(manifest: dict[str, Any]) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')


def load_source_projects() -> dict[str, str]:
    """Committed source_id -> project map (config/source_projects.yaml).

    This is the authoritative, reproducible classification seed. Sources absent
    from it are implicitly 'global'. The manifest 'project' field is a derived
    cache (what memory_index actually reads); apply_source_projects() syncs it.
    """
    if not SOURCE_PROJECTS_PATH.exists():
        return {}
    data = yaml.safe_load(SOURCE_PROJECTS_PATH.read_text()) or {}
    return data.get('projects') or {}


def apply_source_projects() -> dict[str, int]:
    """Sync each manifest source's 'project' from the committed config: listed
    sources get their mapped project, every other source becomes 'global'.
    Idempotent; safe to run every pipeline. Keeps the gitignored manifest in step
    with the committed config so the classification survives a manifest rebuild."""
    mapping = load_source_projects()
    with manifest_lock():
        manifest = load_manifest()
        updated = 0
        for s in manifest['sources']:
            want = mapping.get(s['source_id'], 'global')
            if s.get('project') != want:
                s['project'] = want
                updated += 1
        save_manifest(manifest)
    scoped = sum(1 for s in manifest['sources'] if s.get('project') and s['project'] != 'global')
    return {'sources': len(manifest['sources']), 'scoped': scoped, 'updated': updated}


@contextlib.contextmanager
def manifest_lock():
    ensure_dirs()
    lock_path = STATE_DIR / 'manifest.lock'
    with open(lock_path, 'a+') as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_topic_config() -> dict[str, Any]:
    ensure_dirs()
    config = yaml.safe_load(TOPIC_CONFIG_PATH.read_text()) or {}
    merged = DEFAULT_TOPIC_CONFIG | config
    merged['aliases'] = dict(DEFAULT_TOPIC_CONFIG['aliases'] | (config.get('aliases') or {}))
    merged['ignored'] = list(config.get('ignored') or [])
    merged['manual_topics'] = dict(config.get('manual_topics') or {})
    merged['pinned'] = dict(config.get('pinned') or {})
    return merged


def slugify(value: str, max_length: int = 60) -> str:
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', value).strip('-').lower()
    return slug[:max_length] or 'source'


def canonical_topic_slug(topic: str, config: dict[str, Any] | None = None) -> str:
    cfg = config or load_topic_config()
    slug = slugify(topic, 80)
    aliases = cfg.get('aliases') or {}
    seen = set()
    while slug in aliases and slug not in seen:
        seen.add(slug)
        slug = aliases[slug]
    return slug


def topic_display_name(slug: str, raw_name: str | None = None, config: dict[str, Any] | None = None) -> str:
    cfg = config or load_topic_config()
    pinned = cfg.get('pinned') or {}
    if slug in pinned:
        return pinned[slug]
    if raw_name:
        return raw_name
    return slug.replace('-', ' ').strip().title()


def normalize_topics(topics: list[str], source_id: str | None = None, config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or load_topic_config()
    ignored = {canonical_topic_slug(item, cfg) for item in (cfg.get('ignored') or [])}
    manual_topics = cfg.get('manual_topics') or {}
    normalized: dict[str, str] = {}
    for topic in topics:
        slug = canonical_topic_slug(topic, cfg)
        if slug in ignored:
            continue
        normalized.setdefault(slug, topic_display_name(slug, raw_name=topic, config=cfg))
    if source_id and source_id in manual_topics:
        for manual in manual_topics[source_id]:
            slug = canonical_topic_slug(manual, cfg)
            if slug in ignored:
                continue
            normalized[slug] = topic_display_name(slug, raw_name=manual, config=cfg)
    return list(normalized.values())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def frontmatter(metadata: dict[str, Any], body: str) -> str:
    return f"---\n{yaml.safe_dump(metadata, sort_keys=False).strip()}\n---\n\n{body.strip()}\n"


def read_frontmatter_doc(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text()
    if not text.startswith('---\n'):
        raise RuntimeError(f'Missing frontmatter in {path}')
    _, rest = text.split('---\n', 1)
    yaml_blob, body = rest.split('\n---\n', 1)
    metadata = yaml.safe_load(yaml_blob)
    return metadata, body.strip()


def fetch_article(url: str) -> tuple[str, str]:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        response = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0 (compatible; llm-kb/0.1)'})
        response.raise_for_status()
        downloaded = response.text
    extracted = trafilatura.extract(downloaded, output_format='markdown', include_links=True, include_images=False, favor_precision=True)
    if not extracted:
        raise RuntimeError(f'Unable to extract article content from: {url}')
    metadata = trafilatura.metadata.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else url
    return title.strip(), extracted.strip()


def upsert_manifest_record(collection: str, key_name: str, record: dict[str, Any], sort_key: str) -> None:
    with manifest_lock():
        manifest = load_manifest()
        items = [item for item in manifest[collection] if item[key_name] != record[key_name]]
        items.append(record)
        items.sort(key=lambda item: item.get(sort_key) or '')
        manifest[collection] = items
        save_manifest(manifest)


def find_source_by(*, url: str | None = None, original_path: str | None = None, source_type: str | None = None) -> dict[str, Any] | None:
    for source in load_manifest()['sources']:
        if url and source.get('url') == url:
            return source
        if original_path and source.get('original_path') == original_path and (source_type is None or source.get('source_type') == source_type):
            return source
    return None


def update_source_body(record: dict[str, Any], body: str, imported_at: str) -> dict[str, Any]:
    new_hash = sha256_text(body)
    raw_path = BASE_DIR / record['raw_path']
    metadata, _ = read_frontmatter_doc(raw_path)
    metadata['content_hash'] = new_hash
    metadata['imported_at'] = imported_at
    raw_path.write_text(frontmatter(metadata, body))
    if new_hash != record.get('content_hash'):
        record['content_hash'] = new_hash
        record['needs_summary'] = True
        record['needs_topic_rebuild'] = True
        record['status'] = 'changed'
        record['raw_updated_at'] = imported_at
        update_source_record(record)
    return record


def get_manifest_record(collection: str, key_name: str, value: str) -> dict[str, Any]:
    manifest = load_manifest()
    for item in manifest[collection]:
        if item[key_name] == value:
            return item
    raise KeyError(f'Unknown {key_name}: {value}')


def update_source_record(record: dict[str, Any]) -> None:
    upsert_manifest_record('sources', 'source_id', record, 'imported_at')


def update_topic_record(record: dict[str, Any]) -> None:
    upsert_manifest_record('topics', 'topic_slug', record, 'topic_slug')


def new_source_record(*, source_id: str, source_type: str, title: str, raw_path: Path, content_hash: str, imported_at: str, url: str | None = None, original_path: str | None = None, inbox_origin: str | None = None) -> dict[str, Any]:
    return {
        'source_id': source_id,
        'source_type': source_type,
        'title': title,
        'url': url,
        'original_path': original_path,
        'inbox_origin': inbox_origin,
        'imported_at': imported_at,
        'content_hash': content_hash,
        'raw_path': str(raw_path.relative_to(BASE_DIR)),
        'compiled_path': None,
        'compiled_at': None,
        'model': None,
        'status': 'ingested',
        'topics': [],
        'open_questions': [],
        'raw_updated_at': imported_at,
        'last_seen_hash': content_hash,
        'needs_summary': True,
        'needs_topic_rebuild': True,
    }


def ingest_text_body(*, source_type: str, title: str, body: str, imported_at: str, url: str | None = None, original_path: str | None = None, inbox_origin: str | None = None) -> dict[str, Any]:
    source_id = f"{source_type}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(title, 36)}"
    content_hash = sha256_text(body)
    raw_path = RAW_WEB_DIR / f'{source_id}.md'
    metadata = {
        'source_id': source_id,
        'source_type': source_type,
        'title': title,
        'url': url,
        'original_path': original_path,
        'imported_at': imported_at,
        'content_hash': content_hash,
        'status': 'ingested',
    }
    raw_path.write_text(frontmatter(metadata, body))
    record = new_source_record(source_id=source_id, source_type=source_type, title=title, raw_path=raw_path, content_hash=content_hash, imported_at=imported_at, url=url, original_path=original_path, inbox_origin=inbox_origin)
    update_source_record(record)
    return record


def add_file(path_str: str, title: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    source_path = Path(path_str).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    body = source_path.read_text()
    existing = find_source_by(original_path=str(source_path), source_type='file')
    if existing:
        return update_source_body(existing, body, now_iso())
    return ingest_text_body(source_type='file', title=title or source_path.stem, body=body, imported_at=now_iso(), original_path=str(source_path))


def add_url(url: str) -> dict[str, Any]:
    ensure_dirs()
    title, body = fetch_article(url)
    existing = find_source_by(url=url)
    if existing:
        return update_source_body(existing, body, now_iso())
    return ingest_text_body(source_type='web', title=title, body=body, imported_at=now_iso(), url=url)


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or '').strip()
        if text:
            pages.append(f'## Page {index}\n{text}')
    if not pages:
        raise RuntimeError(f'No extractable text found in PDF: {path}')
    return '\n\n'.join(pages)


def add_pdf(path_str: str, title: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    pdf_path = Path(path_str).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    body = extract_pdf_text(pdf_path)
    existing = find_source_by(original_path=str(pdf_path), source_type='pdf')
    if existing:
        return update_source_body(existing, body, now_iso())
    return ingest_text_body(source_type='pdf', title=title or pdf_path.stem, body=body, imported_at=now_iso(), original_path=str(pdf_path))


def safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors='ignore')
    except Exception:
        return None


def git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except Exception:
        return None


def collect_repo_files(path: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in sorted(path.rglob('*')):
        if any(part in SKIP_DIRS for part in candidate.parts):
            continue
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in TEXT_FILE_SUFFIXES:
            continue
        files.append(candidate)
        if len(files) >= MAX_REPO_FILES:
            break
    return files


def render_repo_snapshot(path: Path) -> str:
    files = collect_repo_files(path)
    lines = [f'# Repository snapshot: {path.name}', '', f'Path: {path}', f'Git HEAD: {git_head(path) or "not-a-git-repo"}', '']
    if not files:
        raise RuntimeError(f'No supported text files found in: {path}')
    lines.append('## Files included')
    for file_path in files:
        lines.append(f'- {file_path.relative_to(path)}')
    for file_path in files:
        content = safe_read_text(file_path) or ''
        snippet = content[:MAX_FILE_CHARS].strip()
        if not snippet:
            continue
        lines.extend(['', f'## File: {file_path.relative_to(path)}', '```', snippet, '```'])
    return '\n'.join(lines).strip() + '\n'


def add_repo(path_str: str, title: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    repo_path = Path(path_str).expanduser().resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        raise FileNotFoundError(repo_path)
    body = render_repo_snapshot(repo_path)
    existing = find_source_by(original_path=str(repo_path), source_type='repo')
    if existing:
        return update_source_body(existing, body, now_iso())
    return ingest_text_body(source_type='repo', title=title or repo_path.name, body=body, imported_at=now_iso(), original_path=str(repo_path))


INBOX_TEXT_SUFFIXES = {'.md', '.txt'}


def _nonclobbering_dest(dest_dir: Path, name: str) -> Path:
    """A path in dest_dir that won't overwrite an existing file. sync_inbox
    rglobs recursively but archives flat, so two same-named files in different
    inbox subdirs (e.g. clienta/*/README.md) would otherwise clobber on move."""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while (cand := dest_dir / f'{stem}-{i}{suffix}').exists():
        i += 1
    return cand


def ingest_inbox_file(path: Path) -> dict[str, Any]:
    ensure_dirs()
    processed_dir = RAW_INBOX_DIR / 'processed'
    processed_dir.mkdir(exist_ok=True)
    processed_path = _nonclobbering_dest(processed_dir, path.name)
    body = path.read_text()
    record = ingest_text_body(source_type='inbox', title=path.stem, body=body, imported_at=now_iso(), original_path=str(processed_path.resolve()), inbox_origin=path.name)
    shutil.move(str(path), str(processed_path))
    return record


def ingest_inbox_pdf(path: Path) -> dict[str, Any]:
    """Ingest a PDF dropped in the inbox. The nightly never called add_pdf, so
    PDFs silently rotted forever; route them through the existing PDF text path."""
    ensure_dirs()
    processed_dir = RAW_INBOX_DIR / 'processed'
    processed_dir.mkdir(exist_ok=True)
    processed_path = _nonclobbering_dest(processed_dir, path.name)
    body = extract_pdf_text(path)
    record = ingest_text_body(source_type='pdf', title=path.stem, body=body, imported_at=now_iso(), original_path=str(processed_path.resolve()), inbox_origin=path.name)
    shutil.move(str(path), str(processed_path))
    return record


def quarantine_inbox_file(path: Path, reason: str) -> dict[str, Any]:
    """Move an un-ingestable inbox file to raw/inbox/unsupported/ instead of
    silently leaving it to rot (the old filter just skipped it forever). It's
    then visible to the operator and not re-scanned every run."""
    ensure_dirs()
    unsupported_dir = RAW_INBOX_DIR / 'unsupported'
    unsupported_dir.mkdir(exist_ok=True)
    dest = _nonclobbering_dest(unsupported_dir, path.name)
    shutil.move(str(path), str(dest))
    return {'status': 'unsupported', 'reason': reason,
            'file': str(path.relative_to(RAW_INBOX_DIR)),
            'moved_to': str(dest.relative_to(BASE_DIR))}


def sync_inbox() -> list[dict[str, Any]]:
    ensure_dirs()
    results = []
    candidates = sorted(
        p for p in RAW_INBOX_DIR.rglob('*')
        if p.is_file()
        and 'processed' not in p.parts
        and 'unsupported' not in p.parts
    )
    for path in candidates:
        suffix = path.suffix.lower()
        if suffix in INBOX_TEXT_SUFFIXES:
            results.append(ingest_inbox_file(path))
        elif suffix == '.pdf':
            try:
                results.append(ingest_inbox_pdf(path))
            except Exception as e:
                results.append(quarantine_inbox_file(path, f'pdf extraction failed: {e}'))
        else:
            results.append(quarantine_inbox_file(
                path, f'unsupported type {suffix or "(no extension)"} — convert to .md/.txt/.pdf'))
    return results


def render_source_summary_prompt(source_meta: dict[str, Any], source_body: str) -> str:
    template = SOURCE_PROMPT_PATH.read_text().strip()
    return f"{template}\n\nSource metadata:\n- source_id: {source_meta['source_id']}\n- title: {source_meta['title']}\n- url: {source_meta.get('url', '')}\n\nSource content:\n\n{source_body}\n"


def ollama_base_url() -> str:
    """Base URL of the Ollama server, from KB_OLLAMA_URL (default localhost).

    On the home server this points at the PC's GPU over Tailscale; everywhere
    else it stays local, so main and manual PC runs are unaffected.
    """
    return os.environ.get('KB_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')


def ollama_generate(model: str, prompt: str, num_ctx: int = DEFAULT_NUM_CTX) -> str:
    response = requests.post(
        f'{ollama_base_url()}/api/generate',
        json={'model': model, 'prompt': prompt, 'stream': False, 'options': {'num_ctx': num_ctx}},
        timeout=600,
    )
    response.raise_for_status()
    return response.json()['response'].strip()


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Split a 'backend:model' spec into (backend, model).

    Only 'claude' and 'ollama' are recognized prefixes; anything else (including
    bare tags like 'qwen2.5-coder:7b' that themselves contain a colon) defaults
    to the ollama backend with the whole string as the model.
    """
    head, sep, rest = spec.partition(':')
    if sep and rest and head in ('claude', 'ollama'):
        return head, rest
    return 'ollama', spec


CLAUDE_CLI_TIMEOUT = 900  # seconds; Opus topic merges can be slow


class CloudLLMError(Exception):
    """Raised when a Claude Code headless call cannot complete.

    `reason` is one of: 'quota', 'auth', 'network', 'other' — used to decide
    fallback behavior and to summarize what happened in the run report.
    """

    def __init__(self, reason: str, detail: str = '') -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f'{reason}: {detail}' if detail else reason)


def classify_cloud_error(text: str) -> str:
    lowered = text.lower()
    if 'session limit' in lowered or 'weekly limit' in lowered or 'usage limit' in lowered:
        return 'quota'
    if 'login' in lowered or 'logged in' in lowered or 'authentication' in lowered \
       or 'revoked' in lowered or '401' in lowered or 'unauthorized' in lowered:
        return 'auth'
    if 'network' in lowered or 'timeout' in lowered or 'connection' in lowered or 'getaddrinfo' in lowered:
        return 'network'
    return 'other'


def claude_code_generate(model: str, prompt: str) -> str:
    """Run one headless Claude Code completion under the subscription login.

    Pipes the prompt via stdin, runs from a neutral cwd so the call does not
    auto-load this repo's CLAUDE.md/skills, and scrubs ANTHROPIC_API_KEY so the
    subscription OAuth token is used. Raises CloudLLMError on any failure.
    """
    env = dict(os.environ)
    env.pop('ANTHROPIC_API_KEY', None)
    # Mark this as a KB-internal headless call. A `claude -p` invocation is itself
    # a Claude Code session that fires SessionEnd / UserPromptSubmit hooks; without
    # this marker the correction-harvest SessionEnd hook would re-spawn the
    # harvester (which calls claude -p again) in an infinite loop, and the memory
    # hook would inject into the pipeline's own prompts. KB hooks check KB_HEADLESS
    # and exit early. The var propagates to the spawned process and its hooks.
    env['KB_HEADLESS'] = '1'
    try:
        proc = subprocess.run(
            ['claude', '-p', '--model', model, '--output-format', 'json'],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_CLI_TIMEOUT,
            cwd=tempfile.gettempdir(),
            env=env,
        )
    except FileNotFoundError as e:
        raise CloudLLMError('other', 'claude CLI not found on PATH') from e
    except subprocess.TimeoutExpired as e:
        raise CloudLLMError('network', f'claude timed out after {CLAUDE_CLI_TIMEOUT}s') from e
    if proc.returncode != 0:
        blob = (proc.stderr or '') + (proc.stdout or '')
        raise CloudLLMError(classify_cloud_error(blob), blob.strip()[:500])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise CloudLLMError('other', 'could not parse claude JSON output') from e
    result = (payload.get('result') or '').strip()
    if payload.get('is_error'):
        # Structured error payload: claude embeds the human-readable error text
        # in the payload fields, so classify from the serialized payload.
        raise CloudLLMError(classify_cloud_error(json.dumps(payload)), 'claude returned is_error=true')
    if not result:
        raise CloudLLMError('other', 'empty result from claude')
    return result


RUN_FALLBACKS: list[dict[str, str]] = []


def record_fallback(*, stage: str, item_id: str, primary: str, reason: str) -> None:
    RUN_FALLBACKS.append({'stage': stage, 'item_id': item_id, 'primary': primary, 'reason': reason})


def write_run_report(counts: dict[str, int]) -> Path:
    """Flush RUN_FALLBACKS + counts to state/last_run_report.json for the wrapper."""
    report = {'fallbacks': list(RUN_FALLBACKS), 'counts': {**counts, 'fallbacks': len(RUN_FALLBACKS)}}
    report_path = STATE_DIR / 'last_run_report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    return report_path


def llm_generate(primary: str, fallback: str | None, prompt: str, *, stage: str, item_id: str, num_ctx: int = DEFAULT_NUM_CTX) -> str:
    """Generate text from `primary` (a backend:model spec); on a CloudLLMError
    fall back to `fallback` (a local spec) and record the event. If no fallback
    is given, the cloud error propagates."""
    backend, model = parse_model_spec(primary)
    if backend == 'ollama':
        return ollama_generate(model=model, prompt=prompt, num_ctx=num_ctx)
    try:
        return claude_code_generate(model=model, prompt=prompt)
    except CloudLLMError as e:
        if not fallback:
            raise
        print(f"WARN: {stage} {item_id}: cloud model {primary} failed ({e.reason}); falling back to {fallback}")
        record_fallback(stage=stage, item_id=item_id, primary=primary, reason=e.reason)
        fb_backend, fb_model = parse_model_spec(fallback)
        if fb_backend == 'claude':
            return claude_code_generate(model=fb_model, prompt=prompt)
        return ollama_generate(model=fb_model, prompt=prompt, num_ctx=num_ctx)


def normalize_bullets(items: list[str]) -> list[str]:
    cleaned = []
    for item in items:
        value = item.strip()
        while value.startswith(('-', '*')):
            value = value[1:].strip()
        lowered = value.lower().rstrip('.:;!')
        if not value or lowered in {'none', 'none identified', 'none explicitly stated', 'none explicitly mentioned', 'n/a'}:
            continue
        cleaned.append(value)
    return cleaned


def extract_bullets(section_name: str, markdown: str) -> list[str]:
    header_pattern = rf'(?mi)^\s*(?:#{{2,4}}\s+|\*\*\s*){re.escape(section_name)}\s*\*?\*?\s*:?\s*$'
    match = re.search(header_pattern, markdown)
    if not match:
        return []
    rest = markdown[match.end():]
    next_header = re.search(r'(?m)^\s*(?:#{2,4}\s+|\*\*[A-Z])', rest)
    block = rest[:next_header.start()] if next_header else rest
    bullets = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith(('-', '*')):
            bullets.append(stripped[1:].strip())
    return normalize_bullets(bullets)


def summarize(source_id: str, model: str = DEFAULT_MODEL, fallback: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    config = load_topic_config()
    record = get_manifest_record('sources', 'source_id', source_id)
    source_meta, source_body = read_frontmatter_doc(BASE_DIR / record['raw_path'])
    summary = llm_generate(model, fallback, render_source_summary_prompt(source_meta, source_body), stage='summary', item_id=source_id)
    compiled_at = now_iso()
    compiled_path = COMPILED_SOURCES_DIR / f'{source_id}.md'
    source_location = source_meta.get('url') or source_meta.get('original_path') or 'unknown'
    source_line = f"Source: [{source_meta['url']}]({source_meta['url']})" if source_meta.get('url') else f"Source file: `{source_location}`"
    topics = normalize_topics(extract_bullets('Suggested topics', summary), source_id=source_id, config=config)
    open_questions = normalize_bullets(extract_bullets('Open questions', summary))
    if not topics:
        topics = normalize_topics(record.get('topics') or [source_meta['title']], source_id=source_id, config=config)
    compiled_doc = (
        f"---\n{yaml.safe_dump({'source_id': source_id, 'title': source_meta['title'], 'url': source_meta.get('url'), 'compiled_at': compiled_at, 'model': model, 'topics': topics}, sort_keys=False).strip()}\n---\n\n"
        f"# {source_meta['title']}\n\n{source_line}\n\n{summary}\n"
    )
    compiled_path.write_text(compiled_doc)
    record['compiled_path'] = str(compiled_path.relative_to(BASE_DIR))
    record['compiled_at'] = compiled_at
    record['model'] = model
    record['status'] = 'compiled'
    record['topics'] = topics
    record['open_questions'] = open_questions
    record['raw_updated_at'] = record.get('raw_updated_at') or source_meta.get('imported_at') or compiled_at
    record['last_seen_hash'] = record['content_hash']
    record['needs_summary'] = False
    record['needs_topic_rebuild'] = True
    record.pop('last_error', None)
    record.pop('last_error_at', None)
    update_source_record(record)
    return record


def load_compiled_source(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return read_frontmatter_doc(BASE_DIR / record['compiled_path'])


def gather_topic_inputs(topic_slug: str, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = config or load_topic_config()
    manifest = load_manifest()
    inputs = []
    for source in manifest['sources']:
        if source.get('status') != 'compiled' or not source.get('compiled_path'):
            continue
        source_topics = normalize_topics(source.get('topics') or [], source_id=source['source_id'], config=cfg)
        if topic_slug in [canonical_topic_slug(topic, cfg) for topic in source_topics]:
            compiled_meta, compiled_body = load_compiled_source(source)
            inputs.append({'source_id': source['source_id'], 'title': source['title'], 'compiled_at': source.get('compiled_at'), 'body': compiled_body, 'meta': compiled_meta})
    # Newest first so that on conflict the merge prompt treats recent sources as
    # authoritative (autonomous recency-supersede), and the cap drops the
    # OLDEST sources rather than silently truncating whichever fall off the
    # context window non-deterministically.
    inputs.sort(key=lambda item: item.get('compiled_at') or '', reverse=True)
    if len(inputs) > MAX_TOPIC_INPUTS:
        inputs = inputs[:MAX_TOPIC_INPUTS]
    return inputs


def render_topic_prompt(topic_name: str, topic_slug: str, topic_inputs: list[dict[str, Any]]) -> str:
    template = TOPIC_PROMPT_PATH.read_text().strip()
    parts = [template, '', f'Topic name: {topic_name}', f'Topic slug: {topic_slug}', '', 'Source summaries (newest first):']
    for item in topic_inputs:
        meta = item.get('meta') or {}
        doc_type = meta.get('Doc Type') or meta.get('doc_type') or ''
        when = item.get('compiled_at') or ''
        tag = f" [{doc_type}]" if doc_type else ''
        header = f"### Source {item['source_id']} — {item['title']}{tag}"
        if when:
            header += f" (compiled {when})"
        parts.extend(['', header, item['body']])
    return '\n'.join(parts).strip() + '\n'


def compile_topic(topic_name: str, model: str = DEFAULT_MODEL, config: dict[str, Any] | None = None, fallback: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    cfg = config or load_topic_config()
    topic_slug = canonical_topic_slug(topic_name, cfg)
    display_name = topic_display_name(topic_slug, raw_name=topic_name, config=cfg)
    topic_inputs = gather_topic_inputs(topic_slug, cfg)
    if not topic_inputs:
        raise RuntimeError(f'No compiled sources mention topic: {topic_name}')
    topic_body = llm_generate(model, fallback, render_topic_prompt(display_name, topic_slug, topic_inputs), stage='topic', item_id=topic_slug, num_ctx=TOPIC_NUM_CTX)
    compiled_at = now_iso()
    topic_path = COMPILED_TOPICS_DIR / f'{topic_slug}.md'
    topic_doc = (
        f"---\n{yaml.safe_dump({'topic': display_name, 'topic_slug': topic_slug, 'compiled_at': compiled_at, 'model': model, 'source_ids': [item['source_id'] for item in topic_inputs]}, sort_keys=False).strip()}\n---\n\n"
        f"# {display_name}\n\n{topic_body}\n\n## Source links\n" + '\n'.join(f"- [[{item['source_id']}]] {item['title']}" for item in topic_inputs) + '\n'
    )
    topic_path.write_text(topic_doc)
    record = {'topic': display_name, 'topic_slug': topic_slug, 'compiled_at': compiled_at, 'compiled_path': str(topic_path.relative_to(BASE_DIR)), 'model': model, 'source_ids': [item['source_id'] for item in topic_inputs], 'open_questions': normalize_bullets(extract_bullets('Open questions', topic_body)), 'contradictions': normalize_bullets(extract_bullets('Contradictions or ambiguities', topic_body))}
    update_topic_record(record)
    return record


def compile_all_topics(model: str = DEFAULT_MODEL, fallback: str | None = None) -> list[dict[str, Any]]:
    manifest = load_manifest()
    config = load_topic_config()
    topic_names: dict[str, str] = {}
    for source in manifest['sources']:
        if source.get('status') != 'compiled':
            continue
        normalized = normalize_topics(source.get('topics') or [], source_id=source['source_id'], config=config)
        for topic in normalized:
            slug = canonical_topic_slug(topic, config)
            topic_names.setdefault(slug, topic_display_name(slug, raw_name=topic, config=config))
    results = []
    for _, name in sorted(topic_names.items()):
        try:
            results.append(compile_topic(topic_name=name, model=model, config=config, fallback=fallback))
        except Exception as e:
            print(f"WARN: compile-topic failed for {name}: {e}")
    return results


def detect_changed_sources() -> list[dict[str, Any]]:
    manifest = load_manifest()
    changed = []
    for source in manifest['sources']:
        _, body = read_frontmatter_doc(BASE_DIR / source['raw_path'])
        current_hash = sha256_text(body)
        if current_hash != source.get('last_seen_hash'):
            source['content_hash'] = current_hash
            source['needs_summary'] = True
            source['needs_topic_rebuild'] = True
            source['status'] = 'changed'
            source['raw_updated_at'] = now_iso()
            update_source_record(source)
            changed.append(source)
    return changed


def summarize_changed_sources(model: str = DEFAULT_MODEL, fallback: str | None = None) -> list[dict[str, Any]]:
    detect_changed_sources()
    results = []
    for source in load_manifest()['sources']:
        if not source.get('needs_summary'):
            continue
        try:
            results.append(summarize(source['source_id'], model=model, fallback=fallback))
        except Exception as e:
            print(f"WARN: summarize failed for {source['source_id']}: {e}")
            try:
                rec = get_manifest_record('sources', 'source_id', source['source_id'])
                rec['last_error'] = f'summarize: {e}'
                rec['last_error_at'] = now_iso()
                update_source_record(rec)
            except Exception:
                pass
    return results


def affected_topic_names(source_records: list[dict[str, Any]], config: dict[str, Any] | None = None) -> dict[str, str]:
    cfg = config or load_topic_config()
    names: dict[str, str] = {}
    for source in source_records:
        for topic in normalize_topics(source.get('topics') or [], source_id=source['source_id'], config=cfg):
            slug = canonical_topic_slug(topic, cfg)
            names.setdefault(slug, topic_display_name(slug, raw_name=topic, config=cfg))
    return names


def rebuild_affected_topics(source_records: list[dict[str, Any]], model: str = DEFAULT_MODEL, fallback: str | None = None) -> list[dict[str, Any]]:
    config = load_topic_config()
    names = affected_topic_names(source_records, config)
    rebuilt = []
    failed_names: set[str] = set()
    for _, name in sorted(names.items()):
        try:
            rebuilt.append(compile_topic(topic_name=name, model=model, config=config, fallback=fallback))
        except Exception as e:
            print(f"WARN: compile-topic failed for {name}: {e}")
            failed_names.add(name)
    if source_records:
        with manifest_lock():
            manifest = load_manifest()
            touched_ids = {source['source_id'] for source in source_records}
            for source in manifest['sources']:
                if source['source_id'] not in touched_ids:
                    continue
                source_topic_names = set(source.get('topics') or [])
                if source_topic_names & failed_names:
                    # Keep needs_topic_rebuild=True so next run retries the failed topics
                    pass
                else:
                    source['needs_topic_rebuild'] = False
                    if source.get('status') == 'changed':
                        source['status'] = 'compiled'
            save_manifest(manifest)
    return rebuilt


def build_review_dashboard(model: str = FALLBACK_MODEL) -> Path:
    ensure_dirs()
    recent_sources = [{'source_id': s['source_id'], 'title': s['title'], 'compiled_at': s.get('compiled_at'), 'topics': s.get('topics') or [], 'open_questions': s.get('open_questions') or [], 'status': s.get('status'), 'source_type': s.get('source_type')} for s in load_manifest()['sources'] if s.get('compiled_path')]
    topic_records = load_manifest()['topics']
    collected_questions = []
    for source in recent_sources:
        for question in normalize_bullets(source.get('open_questions') or []):
            collected_questions.append(f"{source['source_id']}: {question}")
    for topic in topic_records:
        for question in normalize_bullets(topic.get('open_questions') or []):
            collected_questions.append(f"topic/{topic['topic_slug']}: {question}")
    newest_sources = sorted(recent_sources, key=lambda item: item.get('compiled_at') or '', reverse=True)[:8]
    topics_needing_attention = [topic for topic in sorted(topic_records, key=lambda item: item.get('compiled_at') or '', reverse=True) if topic.get('open_questions') or topic.get('contradictions')][:12]
    lines = ['# Review dashboard', '', '## Newly compiled sources']
    if newest_sources:
        lines.extend([f"- {s['source_id']} ({s['source_type']}): {s['title']}" for s in newest_sources])
    else:
        lines.append('- None yet')
    lines.extend(['', '## Topics needing attention'])
    if topics_needing_attention:
        for topic in topics_needing_attention:
            notes = []
            if topic.get('contradictions'):
                notes.append(f"{len(topic['contradictions'])} contradictions")
            if topic.get('open_questions'):
                notes.append(f"{len(topic['open_questions'])} open questions")
            suffix = f" — {', '.join(notes)}" if notes else ''
            lines.append(f"- {topic['topic']} ({topic['topic_slug']}){suffix}")
    else:
        lines.append('- None currently flagged')
    lines.extend(['', '## Open questions'])
    if collected_questions:
        lines.extend([f'- {q}' for q in collected_questions[:20]])
    else:
        lines.append('- None currently recorded')
    lines.extend(['', '## Suggested next actions'])
    action_items = []
    if any(s.get('status') == 'changed' for s in recent_sources):
        action_items.append('Run the pipeline to summarize changed sources and rebuild affected topics.')
    if topics_needing_attention:
        action_items.append('Review the flagged topic pages and normalize topic naming where needed.')
    if collected_questions:
        action_items.append('Triage recurring open questions and promote important ones into tracked notes.')
    if not action_items:
        action_items.append('Add more source material or schedule the next review run.')
    for idx, item in enumerate(action_items[:5], start=1):
        lines.append(f'{idx}. {item}')
    (REVIEW_DIR / 'dashboard.md').write_text('\n'.join(lines).rstrip() + '\n')
    (REVIEW_DIR / 'open_questions.md').write_text(('\n'.join(['# Open questions', ''] + [f'- {q}' for q in collected_questions])).rstrip() + '\n')
    return REVIEW_DIR / 'dashboard.md'


def curate_topics() -> dict[str, Any]:
    config = load_topic_config()
    with manifest_lock():
        manifest = load_manifest()
        changed_sources = []
        for source in manifest['sources']:
            normalized = normalize_topics(source.get('topics') or [], source_id=source['source_id'], config=config)
            if normalized != (source.get('topics') or []):
                source['topics'] = normalized
                source['needs_topic_rebuild'] = True
                changed_sources.append(source['source_id'])
        topic_files_removed = []
        valid_slugs = {canonical_topic_slug(topic, config) for source in manifest['sources'] for topic in source.get('topics') or []}
        for topic in manifest['topics']:
            if topic['topic_slug'] not in valid_slugs and (COMPILED_TOPICS_DIR / f"{topic['topic_slug']}.md").exists():
                (COMPILED_TOPICS_DIR / f"{topic['topic_slug']}.md").unlink()
                topic_files_removed.append(topic['topic_slug'])
        manifest['topics'] = [topic for topic in manifest['topics'] if topic['topic_slug'] in valid_slugs]
        save_manifest(manifest)
    return {'changed_sources': changed_sources, 'removed_topic_files': topic_files_removed, 'config_path': str(TOPIC_CONFIG_PATH.relative_to(BASE_DIR))}


def run_pipeline(summary_model: str = FALLBACK_MODEL, topic_model: str = FALLBACK_MODEL, review_model: str = FALLBACK_MODEL, summary_fallback: str | None = None, topic_fallback: str | None = None) -> dict[str, Any]:
    RUN_FALLBACKS.clear()
    inbox_records = sync_inbox()
    summarized = summarize_changed_sources(model=summary_model, fallback=summary_fallback)
    affected_inputs = inbox_records + summarized
    # Also drain any sources left pending from crashed/failed previous runs
    fresh_ids = {r['source_id'] for r in affected_inputs}
    backlog = [s for s in load_manifest()['sources'] if s.get('needs_topic_rebuild') and s['source_id'] not in fresh_ids]
    rebuild_inputs = affected_inputs + backlog
    rebuilt_topics = rebuild_affected_topics(rebuild_inputs, model=topic_model, fallback=topic_fallback) if rebuild_inputs else []
    dashboard_path = build_review_dashboard(model=review_model)
    # Sync source project tags from the committed config onto the (gitignored)
    # manifest so memory scoping survives a manifest rebuild and stays in step
    # with config/source_projects.yaml. Best-effort — never fail the run.
    try:
        apply_source_projects()
    except Exception:
        pass
    # Refresh the eager-memory index from the freshly compiled topics so retrieval
    # tracks the latest consolidated facts. Best-effort: check=False and a broad
    # guard mean an index failure can NEVER fail the nightly run (the hook fails
    # open when the index is stale or missing).
    memory_index_result = None
    try:
        proc = subprocess.run(
            [sys.executable, str(BASE_DIR / 'scripts' / 'memory_index.py'), 'build'],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            memory_index_result = json.loads(proc.stdout)
    except Exception:
        pass
    # Keep the embedding-retrieval cache in step with the freshly built index so
    # KB_RETRIEVAL=embedding (and shadow mode) score against current topics. Fully
    # INCREMENTAL — only changed topics re-embed, so a normal nightly embeds a
    # handful. Best-effort and gated: a missing embed model or Ollama outage leaves
    # the prior cache untouched, and KB_EMBED_NIGHTLY=0 disables it entirely.
    embedding_index_result = None
    if memory_index_result and os.environ.get('KB_EMBED_NIGHTLY', '1') != '0':
        try:
            proc = subprocess.run(
                [sys.executable, str(BASE_DIR / 'scripts' / 'embedding_rescorer.py'), 'build'],
                check=False, capture_output=True, text=True, timeout=900,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                embedding_index_result = proc.stdout.strip().splitlines()[-1]
        except Exception:
            pass
    report_path = write_run_report({'summarized': len(summarized), 'rebuilt_topics': len(rebuilt_topics)})
    return {'ingested_from_inbox': [r['source_id'] for r in inbox_records], 'summarized': [r['source_id'] for r in summarized], 'rebuilt_topics': [r['topic_slug'] for r in rebuilt_topics], 'dashboard_path': str(dashboard_path.relative_to(BASE_DIR)), 'memory_index': memory_index_result, 'embedding_index': embedding_index_result, 'fallbacks': len(RUN_FALLBACKS), 'run_report_path': str(report_path.relative_to(BASE_DIR))}


def list_sources() -> list[dict[str, Any]]:
    return load_manifest()['sources']


def list_topics() -> list[dict[str, Any]]:
    return load_manifest()['topics']


def main() -> None:
    parser = argparse.ArgumentParser(description='Local-first LLM knowledge-base CLI')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('sync-inbox', help='Import .md/.txt files from raw/inbox/')
    subparsers.add_parser('detect-changes', help='Mark raw sources whose content changed')
    subparsers.add_parser('list', help='List sources in the manifest')
    subparsers.add_parser('list-topics', help='List topics in the manifest')
    subparsers.add_parser('curate-topics', help='Apply alias/ignore/manual topic curation rules')
    subparsers.add_parser('apply-projects', help='Sync manifest source project tags from config/source_projects.yaml')

    add_url_parser = subparsers.add_parser('add-url', help='Fetch a web article into raw/web/')
    add_url_parser.add_argument('url')
    add_file_parser = subparsers.add_parser('add-file', help='Ingest a local text/markdown file into raw/web/')
    add_file_parser.add_argument('path')
    add_file_parser.add_argument('--title', default=None)
    add_pdf_parser = subparsers.add_parser('add-pdf', help='Extract text from a PDF into raw/web/')
    add_pdf_parser.add_argument('path')
    add_pdf_parser.add_argument('--title', default=None)
    add_repo_parser = subparsers.add_parser('add-repo', help='Snapshot a local repo/folder into raw/web/')
    add_repo_parser.add_argument('path')
    add_repo_parser.add_argument('--title', default=None)
    summarize_parser = subparsers.add_parser('summarize', help='Summarize one ingested source with Ollama')
    summarize_parser.add_argument('source_id')
    summarize_parser.add_argument('--model', default=DEFAULT_MODEL)
    summarize_parser.add_argument('--fallback', default=None)
    compile_topic_parser = subparsers.add_parser('compile-topic', help='Compile one topic page from summarized sources')
    compile_topic_parser.add_argument('topic_name')
    compile_topic_parser.add_argument('--model', default=DEFAULT_MODEL)
    compile_topic_parser.add_argument('--fallback', default=None)
    compile_all_topics_parser = subparsers.add_parser('compile-topics', help='Compile all discovered topics')
    compile_all_topics_parser.add_argument('--model', default=DEFAULT_MODEL)
    compile_all_topics_parser.add_argument('--fallback', default=None)
    summarize_changed_parser = subparsers.add_parser('summarize-changed', help='Summarize only changed/new sources')
    summarize_changed_parser.add_argument('--model', default=FALLBACK_MODEL)
    summarize_changed_parser.add_argument('--fallback', default=None)
    rebuild_affected_parser = subparsers.add_parser('rebuild-affected', help='Rebuild topics affected by changed/new sources')
    rebuild_affected_parser.add_argument('--model', default=FALLBACK_MODEL)
    rebuild_affected_parser.add_argument('--fallback', default=None)
    review_parser = subparsers.add_parser('review', help='Build a review dashboard and open-questions page')
    review_parser.add_argument('--model', default=FALLBACK_MODEL)
    run_parser = subparsers.add_parser('run', help='Run inbox sync, changed-source summary, affected topic rebuild, and review dashboard')
    run_parser.add_argument('--summary-model', default=FALLBACK_MODEL)
    run_parser.add_argument('--topic-model', default=FALLBACK_MODEL)
    run_parser.add_argument('--review-model', default=FALLBACK_MODEL)
    run_parser.add_argument('--summary-fallback', default=None)
    run_parser.add_argument('--topic-fallback', default=None)

    args = parser.parse_args()
    if args.command == 'add-url':
        print(json.dumps(add_url(args.url), indent=2))
    elif args.command == 'add-file':
        print(json.dumps(add_file(args.path, title=args.title), indent=2))
    elif args.command == 'add-pdf':
        print(json.dumps(add_pdf(args.path, title=args.title), indent=2))
    elif args.command == 'add-repo':
        print(json.dumps(add_repo(args.path, title=args.title), indent=2))
    elif args.command == 'summarize':
        print(json.dumps(summarize(args.source_id, model=args.model, fallback=args.fallback), indent=2))
    elif args.command == 'compile-topic':
        print(json.dumps(compile_topic(args.topic_name, model=args.model, fallback=args.fallback), indent=2))
    elif args.command == 'compile-topics':
        print(json.dumps(compile_all_topics(model=args.model, fallback=args.fallback), indent=2))
    elif args.command == 'sync-inbox':
        print(json.dumps(sync_inbox(), indent=2))
    elif args.command == 'detect-changes':
        print(json.dumps(detect_changed_sources(), indent=2))
    elif args.command == 'summarize-changed':
        print(json.dumps(summarize_changed_sources(model=args.model, fallback=args.fallback), indent=2))
    elif args.command == 'rebuild-affected':
        print(json.dumps(rebuild_affected_topics([s for s in load_manifest()['sources'] if s.get('needs_topic_rebuild')], model=args.model, fallback=args.fallback), indent=2))
    elif args.command == 'review':
        print(json.dumps({'dashboard_path': str(build_review_dashboard(model=args.model).relative_to(BASE_DIR))}, indent=2))
    elif args.command == 'run':
        print(json.dumps(run_pipeline(summary_model=args.summary_model, topic_model=args.topic_model, review_model=args.review_model, summary_fallback=args.summary_fallback, topic_fallback=args.topic_fallback), indent=2))
    elif args.command == 'curate-topics':
        print(json.dumps(curate_topics(), indent=2))
    elif args.command == 'apply-projects':
        print(json.dumps(apply_source_projects(), indent=2))
    elif args.command == 'list':
        print(json.dumps(list_sources(), indent=2))
    elif args.command == 'list-topics':
        print(json.dumps(list_topics(), indent=2))


if __name__ == '__main__':
    main()
