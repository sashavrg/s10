import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import codex_memory_mcp as mcp  # noqa: E402


def test_initialize_advertises_the_recall_tool():
    reply = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert reply['result']['serverInfo']['name'] == 's10'
    assert reply['result']['capabilities']['tools']['listChanged'] is False

    tools = mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert tools['result']['tools'] == [mcp.TOOL]


def test_recall_omits_project_scoped_facts_without_project(monkeypatch):
    monkeypatch.setattr(mcp.memory_retrieval, 'retrieve', lambda *args, **kwargs: {
        'query': 'deploy', 'mode': 'lexical',
        'matches': [
            {'slug': 'global', 'projects': [], 'key_points': ['global fact']},
            {'slug': 'private', 'projects': ['client-a'], 'key_points': ['private fact']},
        ],
    })

    result = mcp.recall_memory({'query': 'deploy'})
    assert [match['slug'] for match in result['matches']] == ['global']


def test_recall_passes_project_and_fact_cap(monkeypatch):
    captured = {}

    def retrieve(query, project, max_facts):
        captured.update(query=query, project=project, max_facts=max_facts)
        return {'query': query, 'mode': 'lexical', 'matches': []}

    monkeypatch.setattr(mcp.memory_retrieval, 'retrieve', retrieve)
    mcp.recall_memory({'query': 'deploy', 'project': 's10', 'max_facts': 3})
    assert captured == {'query': 'deploy', 'project': 's10', 'max_facts': 3}


@pytest.mark.parametrize('arguments', [
    {}, {'query': ''}, {'query': 'x', 'project': ''}, {'query': 'x', 'max_facts': 0},
])
def test_recall_rejects_invalid_arguments(arguments):
    with pytest.raises(ValueError):
        mcp.recall_memory(arguments)
