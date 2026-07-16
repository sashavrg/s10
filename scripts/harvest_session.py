#!/usr/bin/env python3
"""
Harvest explicit human corrections from a Claude Code session transcript and
drop them into raw/inbox/ as a dated correction note.

Design constraints (set by the operator):
- Fully autonomous. No human gate, no review step the pipeline waits on.
- HIGH PRECISION, LOW RECALL. Catch only unambiguous human overrides.
  Collaborative / emergent conclusions are deliberately IGNORED here because
  they already reach the KB via hand-authored inbox notes; harvesting them
  too would double-ingest and manufacture contradictions.
- Key on the HUMAN turn (a correction speech act), never on the assistant's
  unchallenged assertions, to avoid laundering model output into "knowledge".
- Silence is a valid output. A session with no hard corrections emits nothing.

This module does NOT touch the consolidation pipeline. Its output is an
ordinary inbox .md file with `doc-type: correction`, which flows through the
existing sync-inbox -> summarize -> rebuild path unchanged.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
# kb.py lives alongside this script; reuse its cloud backend (claude_code_generate),
# spec parser, and CloudLLMError so the harvester shares one LLM path with the
# nightly pipeline instead of maintaining a second, divergent one.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb  # noqa: E402

RAW_INBOX_DIR = BASE_DIR / 'raw' / 'inbox'
STATE_DIR = BASE_DIR / 'state'
HARVEST_STATE_PATH = STATE_DIR / 'harvested_sessions.json'

# Extraction is precision-critical and low-volume (one call per session), so it
# defaults to a CLOUD model. Validation on real transcripts showed local 3B/7B
# models fabricate corrections and copy prompt examples verbatim; only the cloud
# model was both precise (zero false positives) and had recall. On cloud failure
# the harvester SKIPS the session — there is deliberately NO local fallback,
# because a missed session costs nothing while a fabricated correction poisons
# the KB. Override with --model (e.g. ollama:qwen2.5-coder:3b) if ever needed.
DEFAULT_SPEC = 'claude:sonnet'
NUM_CTX = 16384
OLLAMA_URL = 'http://localhost:11434'

# Only the last N human/assistant exchanges are scanned. Corrections are local;
# scanning the whole transcript wastes context and raises false positives.
MAX_TURNS_SCANNED = 60
# A harvested correction is dropped if it is >= this ratio similar to a recent
# inbox note, to keep the transcript channel from fighting the hand-note channel.
DEDUP_SIMILARITY = 0.82
DEDUP_LOOKBACK_FILES = 40

EXTRACTION_PROMPT = """You read a coding-session transcript of alternating USER: and ASSISTANT: turns. Extract ONLY durable facts that a USER explicitly asserted while CORRECTING or OVERRIDING the assistant.

A qualifying correction must satisfy ALL of these:
1. SPEAKER: The fact is asserted in a USER turn (a block beginning "USER:"). NEVER extract anything that appears only in an ASSISTANT turn — not even if it is correct, important, or a conclusion.
2. SPEECH ACT: That USER turn explicitly overrides/contradicts/fixes the assistant. Tells: "no", "actually", "that's wrong", "not X, it's Y", "we don't use X anymore", "stop — it's Y".
3. DISTINCT PRIOR: There is a specific prior belief the assistant stated or assumed, and it is DIFFERENT from the corrected fact.

Examples that QUALIFY:
- USER: "No, the token endpoint is POST not GET"  ->  prior: "token endpoint is GET", corrected: "token endpoint is POST"
- USER: "That's wrong, auth is mTLS now, not Bearer"  ->  prior: "auth uses Bearer", corrected: "auth is mTLS"

Examples that DO NOT qualify — output NOTHING for these:
- Anything stated in an ASSISTANT turn: the assistant's plans, hypotheses, diagnoses, or conclusions, however confidently phrased. These are model output, not user knowledge — extracting them poisons the KB.
- Collaborative or jointly-reached conclusions. Those are captured elsewhere.
- A USER turn that merely agrees, asks a question, proposes a plan, or pastes notes/specs/code without correcting anything.
- Preferences about tone, style, or formatting, or one-off task edits such as renaming a variable.

Do not copy any wording from these instructions into your output. Every "prior" and "corrected" value must be drawn from the transcript itself.

STRICT RULES:
- If "prior" and "corrected" would be the same text, you are copying a statement, not capturing a correction — DROP it.
- Every "corrected" value must be the USER's own words from a USER turn. If you cannot point to the USER turn that asserts it, DROP it.
- Prefer missing a borderline correction over inventing one. A miss costs nothing; a false correction is harmful.
- If there are NO explicit USER corrections, output exactly: {"corrections": []}

Output VALID JSON ONLY, no prose, no markdown fences:
{"corrections": [{"prior": "<the wrong belief the assistant stated/assumed>", "corrected": "<the fact the USER explicitly asserted instead>", "topic_hint": "<2-5 word subject label>"}]}
"""


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec='seconds')


def load_harvest_state() -> dict:
    if HARVEST_STATE_PATH.exists():
        return json.loads(HARVEST_STATE_PATH.read_text())
    return {'sessions': {}}


def save_harvest_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HARVEST_STATE_PATH.write_text(json.dumps(state, indent=2))


def parse_transcript(path: Path) -> list[dict]:
    """Return [{'role': 'user'|'assistant', 'text': str}, ...] from a Claude Code .jsonl transcript.

    Claude Code stores one JSON object per line. We defensively pull role + text
    regardless of minor schema drift by checking the common shapes.
    """
    turns: list[dict] = []
    for line in path.read_text(errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
        role = msg.get('role') or obj.get('type')
        if role not in ('user', 'assistant'):
            continue
        content = msg.get('content', '')
        text = _flatten_content(content)
        if text.strip():
            turns.append({'role': role, 'text': text.strip()})
    return turns


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(block.get('text', ''))
                # tool_use / tool_result blocks are intentionally skipped:
                # corrections live in natural-language user turns.
            elif isinstance(block, str):
                parts.append(block)
        return '\n'.join(parts)
    return ''


def render_scan_window(turns: list[dict]) -> str:
    window = turns[-MAX_TURNS_SCANNED:]
    lines = []
    for t in window:
        speaker = 'USER' if t['role'] == 'user' else 'ASSISTANT'
        text = t['text']
        if len(text) > 2000:
            text = text[:2000] + ' …[truncated]'
        lines.append(f'{speaker}: {text}')
    return '\n\n'.join(lines)


def ollama_generate(model: str, prompt: str) -> str:
    resp = requests.post(
        f'{OLLAMA_URL}/api/generate',
        json={'model': model, 'prompt': prompt, 'stream': False,
              'options': {'num_ctx': NUM_CTX, 'temperature': 0.0}},
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()['response'].strip()


def generate(spec: str, prompt: str) -> str:
    """Route one generation through the cloud (claude:*) or local ollama per spec.

    Reuses kb's spec parser and cloud backend so the harvester shares the
    pipeline's auth/error handling. Cloud failures raise kb.CloudLLMError; the
    caller (harvest) skips the session rather than falling back locally.
    """
    backend, model = kb.parse_model_spec(spec)
    if backend == 'claude':
        return kb.claude_code_generate(model=model, prompt=prompt)
    return ollama_generate(model, prompt)


def extract_corrections(transcript_text: str, spec: str) -> list[dict]:
    prompt = f'{EXTRACTION_PROMPT}\n\nTRANSCRIPT:\n{transcript_text}\n'
    raw = generate(spec, prompt)
    raw = re.sub(r'^```(?:json)?|```$', '', raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch: pull the first {...} blob.
        m = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    items = data.get('corrections') or []
    cleaned = []
    for it in items:
        prior = (it.get('prior') or '').strip()
        corrected = (it.get('corrected') or '').strip()
        if not corrected:
            continue
        # Precision guard (deterministic, prompt-independent). A genuine override
        # has a DISTINCT prior belief. When the extractor launders an assistant
        # statement into a "correction" it emits prior empty or ~identical to
        # corrected — validation on real transcripts showed this is the dominant
        # false-positive class. Drop those: a miss costs nothing, a false
        # correction poisons the KB.
        if not prior or _near_identical(prior, corrected):
            continue
        cleaned.append({
            'prior': prior,
            'corrected': corrected,
            'topic_hint': (it.get('topic_hint') or '').strip(),
        })
    return cleaned


def _near_identical(a: str, b: str) -> bool:
    """True if a and b are effectively the same fact (so prior!=corrected fails).

    Catches exact match, containment either way, and high fuzzy overlap — the
    shapes the model produces when it copies one statement into both fields.
    """
    a2, b2 = a.lower().strip(), b.lower().strip()
    if not a2 or not b2:
        return False
    if a2 == b2 or a2 in b2 or b2 in a2:
        return True
    return difflib.SequenceMatcher(None, a2, b2).ratio() >= 0.85


def recent_inbox_texts() -> list[str]:
    if not RAW_INBOX_DIR.exists():
        return []
    files = sorted(
        (p for p in RAW_INBOX_DIR.glob('*.md')),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:DEDUP_LOOKBACK_FILES]
    return [p.read_text(errors='replace') for p in files]


def is_duplicate(correction: dict, corpus: list[str]) -> bool:
    needle = f"{correction['prior']} {correction['corrected']}".lower().strip()
    if not needle:
        return False
    # Also test the corrected fact alone — the "prior" wrong belief often won't
    # appear in a hand-note that only states the right answer.
    corrected_only = correction['corrected'].lower().strip()
    for text in corpus:
        hay = text.lower()
        for probe in (needle, corrected_only):
            if not probe:
                continue
            # Direct containment is the strongest signal and cheap.
            if probe in hay:
                return True
            # Otherwise slide a probe-sized window across the note and take the
            # best LOCAL ratio, so a short fact embedded in a long note isn't
            # penalized for the length mismatch the way a whole-string ratio is.
            w = len(probe)
            if w < 12 or len(hay) < w:
                continue
            step = max(1, w // 4)
            best = 0.0
            for i in range(0, len(hay) - w + 1, step):
                r = difflib.SequenceMatcher(None, probe, hay[i:i + w]).ratio()
                if r > best:
                    best = r
                    if best >= DEDUP_SIMILARITY:
                        return True
    return False


def write_correction_note(corrections: list[dict], project: str, session_id: str) -> Path | None:
    if not corrections:
        return None
    RAW_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    date = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    slug = re.sub(r'[^a-z0-9]+', '-', (project or 'session').lower()).strip('-')[:36]
    fname = f'correction-{date}-{slug}.md'
    path = RAW_INBOX_DIR / fname

    # Identity block per docs/classification-conventions.md so the classifier
    # routes these correctly and treats them as a distinct, low-trust source.
    lines = [
        f'# Corrections harvested from {project or "session"} session',
        '',
        'Environment: cross-environment',
        f'Project: {project or "none"}',
        'Status: active',
        'Doc Type: correction',
        'Source: claude-code-session-harvest',
        f'Session: {session_id}',
        f'Harvested: {now_iso()}',
        '',
        '## Corrections',
        '',
        '> Auto-harvested explicit user overrides. Newer corrections supersede '
        'older ones on the same fact.',
        '',
    ]
    for c in corrections:
        if c['prior']:
            lines.append(f"- Corrected: **{c['prior']}** → **{c['corrected']}** "
                         f"({c['topic_hint']})".rstrip())
        else:
            lines.append(f"- Asserted: **{c['corrected']}** ({c['topic_hint']})".rstrip())
    lines.append('')
    if any(c['topic_hint'] for c in corrections):
        hints = sorted({c['topic_hint'] for c in corrections if c['topic_hint']})
        lines.append('## Suggested topics')
        lines.append('')
        lines.extend(f'- {h}' for h in hints)
        lines.append('')
    path.write_text('\n'.join(lines))
    return path


def harvest(transcript_path: Path, project: str, spec: str, force: bool = False) -> dict:
    if not transcript_path.exists():
        return {'status': 'error', 'reason': f'transcript not found: {transcript_path}'}

    content_hash = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    session_id = transcript_path.stem
    state = load_harvest_state()
    prev = state['sessions'].get(session_id)
    if prev and prev.get('hash') == content_hash and not force:
        return {'status': 'skipped', 'reason': 'already harvested', 'session': session_id}

    turns = parse_transcript(transcript_path)
    if not turns:
        return {'status': 'empty', 'reason': 'no parseable turns', 'session': session_id}

    transcript_text = render_scan_window(turns)
    try:
        corrections = extract_corrections(transcript_text, spec)
    except kb.CloudLLMError as e:
        # Skip-on-failure: emit nothing and DO NOT record state, so a later
        # --force run can retry once the cloud backend is healthy again. No local
        # fallback by design — a fabricated correction would poison the KB.
        return {'status': 'skipped', 'reason': f'cloud LLM unavailable: {e}', 'session': session_id}

    corpus = recent_inbox_texts()
    kept = [c for c in corrections if not is_duplicate(c, corpus)]
    dropped = len(corrections) - len(kept)

    note_path = write_correction_note(kept, project, session_id)

    state['sessions'][session_id] = {
        'hash': content_hash,
        'harvested_at': now_iso(),
        'project': project,
        'corrections_found': len(corrections),
        'corrections_written': len(kept),
        'duplicates_dropped': dropped,
        'note': str(note_path.relative_to(BASE_DIR)) if note_path else None,
    }
    save_harvest_state(state)

    return {
        'status': 'ok',
        'session': session_id,
        'project': project,
        'corrections_found': len(corrections),
        'corrections_written': len(kept),
        'duplicates_dropped': dropped,
        'note': str(note_path.relative_to(BASE_DIR)) if note_path else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Harvest explicit user corrections from a Claude Code transcript into raw/inbox/.')
    parser.add_argument('transcript', help='Path to the session .jsonl transcript')
    parser.add_argument('--project', default='none', help='Project/client tag (per-project transcript dir name works well)')
    parser.add_argument('--model', default=DEFAULT_SPEC,
                        help='backend:model spec (default claude:sonnet; e.g. ollama:qwen2.5-coder:3b to force local)')
    parser.add_argument('--force', action='store_true', help='Re-harvest even if the transcript hash is unchanged')
    args = parser.parse_args()
    result = harvest(Path(args.transcript).expanduser(), args.project, args.model, force=args.force)
    print(json.dumps(result, indent=2))
    if result['status'] == 'error':
        sys.exit(1)


if __name__ == '__main__':
    main()
