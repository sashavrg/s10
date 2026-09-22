#!/usr/bin/env python3
"""Read human/assistant turns from supported coding-agent JSONL transcripts.

The KB deliberately keeps transcript parsing separate from the correction and
outcome workflows. Agent transcript layouts are implementation details, so a
new layout should be added here rather than copied into each consumer.
"""
from __future__ import annotations

import json
from pathlib import Path


def flatten_content(content) -> str:
    """Return text-bearing content blocks, ignoring tool traffic."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get('type') in ('text', 'input_text', 'output_text'):
            parts.append(block.get('text', ''))
    return '\n'.join(parts)


def _message_from_record(record: dict) -> dict | None:
    """Normalize Claude Code and Codex records to a message-shaped object."""
    # Claude Code emits a top-level ``message`` object. Its older records also
    # have the message fields at top level.
    message = record.get('message')
    if isinstance(message, dict):
        return message
    if record.get('role') in ('user', 'assistant'):
        return record

    # Codex rollout JSONL wraps each persisted Responses item in ``payload``.
    # Only message items represent conversational turns; event_msg records
    # duplicate lifecycle information and are intentionally ignored.
    payload = record.get('payload')
    if isinstance(payload, dict) and payload.get('type') == 'message':
        return payload
    return None


def parse_turns(path: Path) -> list[dict[str, str]]:
    """Return ordered ``{'role', 'text'}`` turns from a transcript JSONL file."""
    turns: list[dict[str, str]] = []
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return turns

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        message = _message_from_record(record)
        if not message:
            continue
        role = message.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = flatten_content(message.get('content', '')).strip()
        if text:
            turns.append({'role': role, 'text': text})
    return turns
