"""Request alias identity tests; synthetic sources and providers only."""
import asyncio
import copy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services import agent_workflow as aw
from app.services.agent_source_registry import (
    RequestSourceRegistry, resolve_source_alias, restore_known_source_aliases, source_alias)


def source(sid=1, chunk='original-chunk'):
    return {'source_id': sid, 'chunk_id': chunk, 'fileId': 'original-file',
            'kb_id': 'allowed', 'fileName': 'book.pdf', 'content': 'Value 42.'}


def evidence(sources):
    return ToolMessage(name='search_knowledge_base', tool_call_id='call', content=json.dumps({
        'retrieval_status': 'SUCCESS', 'context': aw._source_context(sources),
        'source_ids': [s['source_id'] for s in sources], 'citations': sources}))


@pytest.mark.parametrize('first_version,second_version', [
    ({'index_build_id': 'build-A'}, {'index_build_id': 'build-B'}),
    ({'build_id': 'build-A'}, {'build_id': 'build-B'}),
    ({'source_sha256': 'a' * 64}, {'source_sha256': 'b' * 64}),
    ({'source_metadata': {'source_sha256': 'a' * 64}},
     {'source_metadata': {'source_sha256': 'b' * 64}}),
])
def test_known_publication_versions_keep_distinct_sources_even_when_text_and_chunk_match(first_version, second_version):
    entries = []
    first = {**source(), **first_version}
    second = {**source(), **second_version}
    aw._register_sources({'citations': [first, second]}, entries)
    assert [entry['source_id'] for entry in entries] == [1, 2]
    assert entries[0]['content'] == entries[1]['content']
    assert RequestSourceRegistry(entries).alias_for(1) == 's1'
    assert RequestSourceRegistry(entries).alias_for(2) == 's2'


def test_same_known_publication_repeated_across_rounds_reuses_existing_source_number():
    entries = []
    citation = {**source(), 'index_build_id': 'build-A', 'source_sha256': 'a' * 64}
    first = aw._register_sources({'citations': [citation]}, entries)
    second = aw._register_sources({'citations': [dict(citation)]}, entries)
    assert len(entries) == 1
    assert first['source_ids'] == second['source_ids'] == [1]
    # Equivalent explicit field names do not create another identity.
    alternate = {**source(), 'build_id': 'build-A', 'source_metadata': {'source_sha256': 'a' * 64}}
    assert aw._register_sources({'citations': [alternate]}, entries)['source_ids'] == [1]


def test_cross_file_identical_bodies_and_chunk_handles_keep_independent_sources():
    entries = []
    citation = {**source(), 'index_build_id': 'build-A', 'source_sha256': 'a' * 64}
    other_file = {**citation, 'fileId': 'another-file'}
    aw._register_sources({'citations': [citation, other_file]}, entries)
    assert [entry['source_id'] for entry in entries] == [1, 2]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')


def test_request_view_uses_existing_ids_has_no_second_allocation_and_observes_additions():
    entries = [source(2)]
    registry = RequestSourceRegistry(entries)
    assert registry.alias_for(2) == 's2'
    assert registry.resolve('s2') is entries[0]
    assert registry.resolve(2) is entries[0]
    entries.append(source(9, 'next'))
    assert registry.alias_for(9) == 's9'
    assert registry.resolve('s9') is entries[1]
    assert registry.alias_for(1) is None


@pytest.mark.parametrize('handle', [True, False, 0, -1, 1.0, None, '1', 's01', 'S1',
                                    's0', 's-1', 's1 ', ' s1', 's1\n', 'source://1',
                                    'original-chunk', 's' + '9' * 5000])
def test_unknown_or_noncanonical_handles_do_not_resolve(handle):
    assert resolve_source_alias(handle, [source()]) is None


@pytest.mark.parametrize('sid', [True, False, 0, -1, 1.0, None, '1'])
def test_alias_generation_requires_positive_integer_id(sid):
    with pytest.raises(ValueError):
        source_alias(sid)


def test_same_alias_is_request_local_and_ambiguous_identity_fails_closed():
    first, second = [source(1, 'chunk-A')], [source(1, 'chunk-B')]
    assert resolve_source_alias('s1', first)['chunk_id'] == 'chunk-A'
    assert resolve_source_alias('s1', second)['chunk_id'] == 'chunk-B'
    assert resolve_source_alias('s1', first + second) is None


def test_only_known_delivered_exact_alias_citations_restore_without_fabricating_proof():
    entries = [source(1), source(7, 'other')]
    before = copy.deepcopy(entries)
    text = '42 [s1][s1], unavailable [s7], unknown [s99], numeric [1], raw s1, [S1], [s01].'
    restored, stats = restore_known_source_aliases(text, entries, [1])
    assert restored == '42 [1][1], unavailable [s7], unknown [s99], numeric [1], raw s1, [S1], [s01].'
    assert stats == {'restored_alias_count': 2, 'unknown_alias_count': 1, 'undelivered_alias_count': 1}
    assert entries == before


def test_finalizer_restores_known_alias_without_running_answer_validator(monkeypatch):
    entries = [source()]
    response = AIMessage(content='Model interpretation [s1], unsupported source [s99].')
    current = {'messages': [evidence(entries)], 'source_registry': entries,
               'model_evidence_source_ids': [1], 'last_score': .9}
    citations, mode = aw._finalize_knowledge_answer(current, response, include_mode=True)
    assert mode == 'GROUNDED_GENERATION'
    assert response.content == 'Model interpretation [1], unsupported source [s99].'
    assert citations == entries


def test_read_tool_alias_reuses_same_registered_source_scope_and_duplicate_limit(monkeypatch):
    reader_module = importlib.import_module('app.tools.read_knowledge_context')
    entry = source()
    reader = AsyncMock(return_value=json.dumps({'retrieval_status': 'SUCCESS', 'citations': [entry]}))
    monkeypatch.setattr(reader_module, 'read_registered_context', reader)
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    current = {'messages': [AIMessage(content='', tool_calls=[{'name': 'read_knowledge_context',
        'id': 'read1', 'args': {'source_id': 's1', 'kb_ids': ['forbidden']}}])],
        'source_registry': [entry], 'kb_ids': ['allowed']}
    result = asyncio.run(obj.run_tools(current, {}))
    assert reader.call_args.args == (entry, ['allowed'])
    current.update({k: v for k, v in result.items() if k != 'messages'})
    current['messages'] = [AIMessage(content='', tool_calls=[{'name': 'read_knowledge_context',
        'id': 'read2', 'args': {'source_id': 1}}])]
    again = asyncio.run(obj.run_tools(current, {}))
    assert reader.await_count == 1
    assert again['retrieval_blocked']


def test_agent_invocation_encodes_aliases_final_answer_restores_numbers(monkeypatch):
    entries = [source()]
    seen = []
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = []
    obj._get_llm_service = lambda state: SimpleNamespace(model='fake',
        get_chat_model=lambda **kw: SimpleNamespace(bind_tools=lambda tools: object()))

    async def provider(model, messages, **kwargs):
        seen.extend(messages)
        return AIMessage(content='Value 42 [s1].')

    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    raw = evidence(entries)
    result = asyncio.run(obj.agent_node({'messages': [HumanMessage(content='q'), raw],
        'source_registry': entries, 'citations': entries, 'last_score': .9,
        'original_query': 'q', 'kb_ids': ['allowed'], 'retrieval_status': 'SUCCESS'}, {}))
    model_tool = next(m for m in seen if isinstance(m, ToolMessage))
    assert 'Document s1 (' in json.loads(model_tool.content)['context']
    assert json.loads(model_tool.content)['source_ids'] == ['s1']
    assert 'original-chunk' not in model_tool.content
    assert result['messages'][-1].content == 'Value 42 [1].'
    assert json.loads(raw.content)['source_ids'] == [1]
    assert entries == [source()]


@pytest.mark.parametrize('first_answer,expected_calls', [('Value 42 [s1].', 1), ('Invalid [9].', 2)])
def test_optional_router_quality_uses_alias_identity_and_escalation_preserves_delivery(monkeypatch, first_answer, expected_calls):
    from app.services import grounded_generation as gg
    from app.services.model_router import RouterConfig
    from app.services.agent_context_budget import request_chars
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    entries = [source()]
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = []

    def service(name, schema_size):
        class Model:
            kwargs = {'tools': [{'schema': 'p' * schema_size}]}
            def bind_tools(self, tools): return self
        return SimpleNamespace(model=name, get_chat_model=lambda **kw: Model())

    cheap, expensive = service('cheap', 10), service('expensive', 2500)
    obj._get_llm_service = lambda state: cheap
    obj._route_post_retrieval_model = lambda *args, **kwargs: (
        cheap, SimpleNamespace(mode='DYNAMIC', selected_role='CHEAP'), RouterConfig(mode='DYNAMIC'))
    monkeypatch.setattr(gg, '_role_service', lambda *args: expensive)
    calls = []

    async def provider(model, messages, **kwargs):
        calls.append((model, messages))
        return AIMessage(content=first_answer if len(calls) == 1 else 'Value 42 [s1].',
            response_metadata={'finish_reason': 'stop', 'model_name': 'cheap' if len(calls) == 1 else 'expensive'})

    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node({'messages': [HumanMessage(content='q'), evidence(entries)],
        'source_registry': entries, 'citations': entries, 'last_score': .9,
        'original_query': 'q', 'kb_ids': ['allowed'], 'retrieval_status': 'SUCCESS'}, {}))
    assert len(calls) == expected_calls
    assert result['model_evidence_source_ids'] == [1]
    assert result['messages'][-1].content == 'Value 42 [1].'
    for model, messages in calls:
        assert request_chars(messages, model.kwargs['tools']) <= 32000
        tool = next(m for m in messages if isinstance(m, ToolMessage))
        assert json.loads(tool.content)['source_ids'] == ['s1']
        assert 'Document s1 (' in json.loads(tool.content)['context']
