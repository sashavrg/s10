#!/usr/bin/env python3
"""A small, local stdio MCP server exposing project-scoped KB recall to Codex.

It intentionally uses only the Python standard library. The KB is private and
local; running a separate HTTP service or adding a framework dependency would
provide no benefit for one read-only retrieval tool.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / 'scripts'))
import memory_retrieval  # noqa: E402

SERVER_INFO = {'name': 's10', 'version': '1.0.0'}
PROTOCOL_VERSION = '2025-03-26'

TOOL = {
    'name': 'recall_memory',
    'description': (
        'Search the local s10 for durable, possibly relevant project context. '
        'Use before relying on remembered infrastructure, conventions, or prior decisions. '
        'Pass the current project directory name when it is known; omit it to return '
        'global notes only, never notes scoped to another project.'
    ),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'The question or topic to look up.'},
            'project': {
                'type': 'string',
                'description': 'Optional current project directory name, such as "s10".',
            },
            'max_facts': {
                'type': 'integer', 'minimum': 1, 'maximum': 8, 'default': 5,
                'description': 'Maximum facts per matching topic.',
            },
        },
        'required': ['query'],
        'additionalProperties': False,
    },
}


def recall_memory(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get('query')
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a non-empty string')

    project = arguments.get('project')
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise ValueError('project must be a non-empty string when provided')
    max_facts = arguments.get('max_facts', 5)
    if not isinstance(max_facts, int) or isinstance(max_facts, bool) or not 1 <= max_facts <= 8:
        raise ValueError('max_facts must be an integer from 1 through 8')

    result = memory_retrieval.retrieve(query.strip(), project=project, max_facts=max_facts)
    matches = result['matches']
    # ``retrieve(..., project=None)`` is intentionally broad for its legacy CLI.
    # An MCP caller that omitted project must not receive another workspace's facts.
    if project is None:
        matches = [match for match in matches if not match.get('projects')]

    return {
        'query': result['query'],
        'project': project,
        'retrieval_mode': result['mode'],
        'matches': matches,
        'note': 'KB content is auto-summarized; verify it against the live task and code before relying on it.',
    }


def response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get('method')
    request_id = request.get('id')
    if method == 'notifications/initialized':
        return None
    if method == 'initialize':
        return response(request_id, {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': SERVER_INFO,
        })
    if method == 'tools/list':
        return response(request_id, {'tools': [TOOL]})
    if method == 'tools/call':
        params = request.get('params') or {}
        if not isinstance(params, dict) or params.get('name') != 'recall_memory':
            return error(request_id, -32602, 'unknown tool')
        arguments = params.get('arguments') or {}
        if not isinstance(arguments, dict):
            return error(request_id, -32602, 'tool arguments must be an object')
        try:
            result = recall_memory(arguments)
        except Exception as exc:
            return error(request_id, -32602, str(exc))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        return response(request_id, {
            'content': [{'type': 'text', 'text': text}],
            'structuredContent': result,
        })
    return error(request_id, -32601, f'method not found: {method}')


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('request must be an object')
            reply = handle(request)
        except Exception as exc:
            reply = error(None, -32700, f'parse error: {exc}')
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
