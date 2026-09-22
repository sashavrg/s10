import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import transcript_turns  # noqa: E402


def test_parse_turns_reads_claude_message_records(tmp_path):
    transcript = tmp_path / 'claude.jsonl'
    transcript.write_text('\n'.join([
        json.dumps({'message': {'role': 'user', 'content': 'Please fix it.'}}),
        json.dumps({'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'On it.'}]}}),
    ]))

    assert transcript_turns.parse_turns(transcript) == [
        {'role': 'user', 'text': 'Please fix it.'},
        {'role': 'assistant', 'text': 'On it.'},
    ]


def test_parse_turns_reads_codex_response_item_records(tmp_path):
    transcript = tmp_path / 'codex.jsonl'
    transcript.write_text('\n'.join([
        json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}),
        json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Use the staging endpoint.'}],
        }}),
        json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'I will use staging.'}],
        }}),
        json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'developer',
            'content': [{'type': 'input_text', 'text': 'Ignore me.'}],
        }}),
    ]))

    assert transcript_turns.parse_turns(transcript) == [
        {'role': 'user', 'text': 'Use the staging endpoint.'},
        {'role': 'assistant', 'text': 'I will use staging.'},
    ]
