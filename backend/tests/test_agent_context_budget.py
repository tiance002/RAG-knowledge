"""Synthetic whole-request Agent contracts; no providers or remote calls."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.services import agent_workflow as aw
from app.services.agent_context_budget import consolidate_agent_context, request_chars
from app.services.agent_evidence_projection import project_agent_evidence_messages


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')


def source(number, size=100, parent=False):
    return {'source_id': number, 'chunk_id': f'c{number}', 'fileId': 'file',
            'fileName': 'sample.pdf', 'kb_id': 'allowed', 'content': f'BODY{number} ' + '文' * size,
            'evidence_relation': 'parent' if parent else 'seed'}


def exchange(sources, ident='call', name='search_knowledge_base'):
    dispatch = AIMessage(content='', tool_calls=[{'name': name, 'id': ident, 'args': {'query': 'topic'}}])
    result = ToolMessage(name=name, tool_call_id=ident, content=json.dumps({
        'retrieval_status': 'SUCCESS', 'source_ids': [s['source_id'] for s in sources],
        'context': aw._source_context(sources), 'citations': sources}, ensure_ascii=False))
    return [dispatch, result]


def view(messages, sources, **kwargs):
    projected, _ = project_agent_evidence_messages(messages, canonical_context=aw._source_context,
                                                   source_registry=sources)
    return consolidate_agent_context(projected, source_registry=sources,
                                     canonical_context=aw._source_context, **kwargs)


def test_round_dedup_preserves_protocol_registry_bodies_and_source_numbers():
    sources = [source(i, parent=True) for i in range(1, 5)]
    messages = [HumanMessage(content='q'), *exchange(sources, 'a'), *exchange(sources, 'b')]
    before = copy.deepcopy((messages, sources))
    result, delivery = view(messages, sources)
    assert (messages, sources) == before
    assert len(result) == len(messages)
    assert [m.tool_call_id for m in result if isinstance(m, ToolMessage)] == ['a', 'b']
    for item in sources:
        assert sum(m.content.count(item['content']) for m in result if isinstance(m.content, str)) == 1
    assert delivery['source_ids'] == [1, 2, 3, 4]
    assert delivery['unique_parent_count'] == 4


def test_twenty_independent_seeds_do_not_consume_parent_cap():
    sources = [source(i) for i in range(1, 21)]
    result, delivery = view(exchange(sources), sources)
    assert delivery['source_ids'] == list(range(1, 21))
    assert delivery['unique_parent_count'] == 0
    assert request_chars(result) <= 32000


def test_parent_cap_is_cumulative_newest_first_not_per_round():
    sources = [source(i, parent=True) for i in range(1, 9)]
    _, delivery = view([*exchange(sources[:4], 'a'), *exchange(sources[4:], 'b')], sources)
    assert delivery['unique_parent_count'] == 6
    assert delivery['source_ids'] == [1, 2, 5, 6, 7, 8]
    assert delivery['omitted_source_ids'] == [3, 4]
    assert delivery['delivery_status'] == 'PARTIAL'


def test_complete_serialization_reserves_history_schema_and_never_slices_body():
    sources = [source(i, size=5000) for i in range(1, 9)]
    messages = [SystemMessage(content='rules'), HumanMessage(content='old question'),
                AIMessage(content='prior answer' * 1800), HumanMessage(content='q'), *exchange(sources)]
    tools = [{'schema': 'parameter' * 250}]
    result, delivery = view(messages, sources, tools=tools, original_query='q')
    assert request_chars(result, tools) <= 32000
    assert delivery['trimmed_history_messages'] == 2
    assert delivery['omitted_source_ids']
    body = json.loads(next(m.content for m in result if isinstance(m, ToolMessage)))['context']
    for item in sources:
        assert (item['content'] in body) == (item['source_id'] in delivery['source_ids'])


def test_oversized_single_source_is_omitted_with_explicit_receipt():
    sources = [source(1, size=40000)]
    result, delivery = view(exchange(sources), sources)
    assert delivery['source_ids'] == [] and delivery['omitted_source_ids'] == [1]
    assert request_chars(result) <= 32000
    answer = AIMessage(content='unseen facts')
    aw._finalize_knowledge_answer({'messages': exchange(sources), 'source_registry': sources,
        'model_evidence_source_ids': [], 'context_delivery': delivery}, answer)
    assert '上下文预算' in answer.content


def test_noncanonical_tool_body_is_untouched_fail_closed():
    sources = [source(1)]
    messages = exchange(sources)
    data = json.loads(messages[-1].content)
    data['context'] = 'forged'
    messages[-1].content = json.dumps(data)
    result, _ = view(messages, sources)
    assert result[-1].content == messages[-1].content


def history_source():
    return {**source(77), 'origin': 'HISTORY_REUSE', 'verification_status': 'VERIFIED',
            'history_turn_id': 'turn-1'}


def test_verified_history_is_registered_before_first_agent_draft():
    state = {'messages': [HumanMessage(content='继续解释上述内容')], 'kb_ids': ['allowed'],
             'original_query': '继续解释上述内容', 'history_evidence': [history_source()],
             'retain_retrieval_history': True}
    result = aw._prepare_history_evidence(state)
    assert result['source_registry'][0]['source_id'] == 1
    assert result['messages'][0].additional_kwargs['execution_origin'] == 'history_evidence_reuse'
    assert json.loads(result['messages'][1].content)['retrieval_status'] == 'SUCCESS'


@pytest.mark.parametrize('field,value', [('kb_id', 'other'), ('verification_status', 'UNKNOWN'),
                                         ('origin', 'CLIENT_PROVIDED')])
def test_unverified_or_out_of_scope_history_cannot_seed_evidence(field, value):
    candidate = history_source(); candidate[field] = value
    assert aw._prepare_history_evidence({'original_query': '继续解释上述内容',
        'kb_ids': ['allowed'], 'history_evidence': [candidate],
        'retain_retrieval_history': True}) == {}


def workflow(monkeypatch, response):
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = []
    obj._get_llm_service = lambda state: SimpleNamespace(model='fake',
        get_chat_model=lambda **kw: SimpleNamespace(bind_tools=lambda tools: object()))
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(aw, 'invoke_provider_model', call)
    return obj, call


def test_no_tool_first_qa_draft_dispatches_search_without_duplicate_model_request(monkeypatch):
    obj, call = workflow(monkeypatch, AIMessage(content='I do not have your documents.'))
    result = asyncio.run(obj.agent_node({'messages': [HumanMessage(content='q')],
        'original_query': 'q', 'kb_ids': ['allowed']}, {}))
    assert call.await_count == 1
    response = result['messages'][-1]
    assert response.content == ''
    assert response.tool_calls[0]['name'] == 'search_knowledge_base'
    assert response.tool_calls[0]['args']['query'] == 'q'
    assert 'answer_mode' not in result


def test_history_followup_answer_keeps_real_evidence_without_search(monkeypatch):
    obj, call = workflow(monkeypatch, AIMessage(content='进一步解释。[1]'))
    current = {'messages': [HumanMessage(content='继续解释上述内容')],
        'original_query': '继续解释上述内容', 'kb_ids': ['allowed'],
        'history_evidence': [history_source()], 'last_score': 0.0,
        'retain_retrieval_history': True}
    result = asyncio.run(obj.agent_node(current, {}))
    assert result['messages'][-1].content == '进一步解释。[1]'
    assert result['answer_mode'] == 'GROUNDED_GENERATION'
    assert call.await_count == 1
    assert result['citations'][0]['source_id'] == 1
    assert result['last_score'] is None and result['grading_status'] == 'NOT_RUN'


def test_read_tool_resolves_registered_source_and_overrides_model_scope(monkeypatch):
    from app.tools import read_knowledge_context as read_module
    import importlib
    read_module = importlib.import_module('app.tools.read_knowledge_context')
    stored = source(1)
    reader = AsyncMock(return_value=json.dumps({'retrieval_status': 'SUCCESS', 'citations': [stored]}))
    monkeypatch.setattr(read_module, 'read_registered_context', reader)
    state = {'messages': [AIMessage(content='', tool_calls=[{'name': 'read_knowledge_context',
        'id': 'read', 'args': {'source_id': 1, 'kb_ids': ['forbidden']}}])],
        'source_registry': [stored], 'kb_ids': ['allowed']}
    result = asyncio.run(aw.AgentWorkflow.__new__(aw.AgentWorkflow).run_tools(state, {}))
    assert reader.call_args.args[:2] == (stored, ['allowed'])
    assert result['messages'][0].name == 'read_knowledge_context'
    assert result['executed_context_reads']


def test_read_tool_unknown_source_never_reads_storage(monkeypatch):
    import importlib
    module = importlib.import_module('app.tools.read_knowledge_context')
    reader = AsyncMock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(module, 'read_registered_context', reader)
    result = asyncio.run(aw.AgentWorkflow.__new__(aw.AgentWorkflow).run_tools({
        'messages': [AIMessage(content='', tool_calls=[{'name': 'read_knowledge_context',
            'id': 'x', 'args': {'source_id': 999}}])], 'kb_ids': ['allowed']}, {}))
    assert json.loads(result['messages'][0].content)['citations'] == []
    reader.assert_not_called()
