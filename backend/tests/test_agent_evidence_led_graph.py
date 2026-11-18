"""Evidence-led production graph contracts, with synthetic model/tool replies."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services import agent_workflow as aw


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', 'MODEL')
    monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', 'AGENT_EVIDENCE_VIEW_V2')
    created = []
    initialize = aw.AgentWorkflow.__init__
    def tracked_initialize(self):
        initialize(self)
        created.append(self)
    monkeypatch.setattr(aw.AgentWorkflow, '__init__', tracked_initialize)
    yield
    for workflow in created:
        workflow.close()


def make_workflow(monkeypatch, replies):
    class Model:
        kwargs = {}
        def bind_tools(self, tools): return self
    monkeypatch.setattr(aw, 'LLMService', lambda: SimpleNamespace(
        model='synthetic', get_chat_model=lambda **kwargs: Model()))
    schema = aw.search_knowledge_base.args_schema
    tool = AsyncMock(side_effect=[json.dumps(reply) for reply in replies])
    monkeypatch.setattr(aw, 'search_knowledge_base', SimpleNamespace(
        name='search_knowledge_base', args_schema=schema, ainvoke=tool))
    return aw.AgentWorkflow(), tool


def citation(cid, body):
    return {'chunk_id': cid, 'fileId': 'file', 'fileName': 'facts.pdf',
            'kb_id': 'allowed', 'content': body}


def search_call(query, ident):
    return AIMessage(content='', tool_calls=[{
        'name': 'search_knowledge_base', 'id': ident, 'args': {'query': query}}])


def initial():
    return {'messages': [HumanMessage(content='依据资料说明这两个年份。')],
            'kb_ids': ['allowed'], 'search_params': {}, 'agent_steps': [],
            # Legacy callers may still send a low initialized/stale score.
            'last_score': .0, 'grading_status': 'VALID'}


def test_real_graph_reads_supported_source_after_irrelevant_prefix_without_quality_call(monkeypatch):
    monkeypatch.setattr(aw.get_settings(), 'AGENT_TOOL_RESULT_INLINE_CHARS', 2000)
    reply = {'retrieval_status': 'SUCCESS', 'citations': [
        citation('noise', 'Unrelated catalogue entries. ' * 150),
        citation('dates', 'The first event occurred in 1956, and the second in 1966.')],
        'source_mapping_status': 'VERIFIED'}
    obj, tool = make_workflow(monkeypatch, [reply])
    calls = []

    async def provider(model, messages, *, stage, **kwargs):
        assert stage == 'agent', 'No standalone grading model may execute'
        calls.append(messages)
        if len(calls) == 1:
            return search_call('the two events dates', 'search-1')
        data = json.loads([m.content for m in messages if isinstance(m, ToolMessage)][-1])
        if len(calls) == 2:
            assert data['tool_result_status'] == 'SAVED_INCOMPLETE'
            return AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
                'id': 'read-saved', 'args': {'result_handle': data['result_handle'],
                    'source_ids': ['s1', 's2'], 'limit': 20}}])
        context = data['context']
        assert context.find('1956') > 2000 and context.find('1966') > 2000
        assert 'Document s2' in context
        return AIMessage(content='两个年份分别为1956年和1966年。[s2]',
                         response_metadata={'finish_reason': 'stop'})

    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.app.ainvoke(initial()))
    assert len(calls) == 3 and tool.await_count == 1
    assert 'grading' not in obj.app.get_graph().nodes
    assert result['messages'][-1].content == '两个年份分别为1956年和1966年。[2]'
    assert result['answer_mode'] == 'GROUNDED_GENERATION'
    assert result['grading_status'] == 'NOT_RUN' and result['grading_scope'] == {}
    assert result['last_score'] is None
    assert result['model_evidence_source_ids'] == [1, 2]


def test_agent_itself_can_select_alternative_search_after_incomplete_evidence(monkeypatch):
    replies = [{'retrieval_status': 'SUCCESS', 'citations': [citation('partial', 'First year: 1956.')]},
               {'retrieval_status': 'SUCCESS', 'citations': [citation('complete', 'Second year: 1966.')]}]
    obj, tool = make_workflow(monkeypatch, replies)
    calls = []

    async def provider(model, messages, *, stage, **kwargs):
        assert stage == 'agent'
        calls.append(messages)
        if len(calls) == 1:
            return search_call('first year', 'a')
        if len(calls) == 2:
            return search_call('second year', 'b')
        return AIMessage(content='1956年[s1]，1966年[s2]。')

    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.app.ainvoke(initial()))
    assert len(calls) == 3 and tool.await_count == 2
    assert [call.args[0]['query'] for call in tool.await_args_list] == ['first year', 'second year']
    assert result['messages'][-1].content == '1956年[1]，1966年[2]。'
    assert result['retrieval_attempt_count'] == 2 and result['retry_count'] == 0
    assert result['grading_status'] == 'NOT_RUN' and result['last_score'] is None


def test_fresh_tool_output_clears_stale_quality_state_without_changing_retrieval_status():
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    message = ToolMessage(name='search_knowledge_base', tool_call_id='a', content=json.dumps({
        'retrieval_status': 'SUCCESS', 'citations': [citation('a', 'Evidence.')], 'context': 'Evidence.'}))
    result = asyncio.run(obj.process_tool_output({'messages': [message],
        'last_score': .1, 'retrieval_retry_scheduled': True, 'grading_status': 'VALID'}))
    assert result['last_score'] is None and result['grading_status'] == 'NOT_RUN'
    assert result['grading_scope'] == {} and not result['retrieval_retry_scheduled']
    assert result['retrieval_status'] == 'SUCCESS'


@pytest.mark.parametrize('strategy', ['fixed', 'model'])
def test_empty_search_preserves_configured_no_evidence_fallback_and_does_not_claim_quality_pass(monkeypatch, strategy):
    from app.services import grounded_generation as gg
    obj, tool = make_workflow(monkeypatch, [{'retrieval_status': 'NO_RESULTS', 'citations': []}])
    provider = AsyncMock(return_value=search_call('missing', 'a'))
    fallback = AsyncMock(return_value=('No supporting evidence.', strategy,
                                      None if strategy == 'fixed' else 'stop', None))
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    monkeypatch.setattr(gg, 'generate_no_results_fallback', fallback)
    result = asyncio.run(obj.app.ainvoke(initial()))
    assert provider.await_count == 1 and tool.await_count == 1
    assert fallback.await_count == 1
    assert result['messages'][-1].content == 'No supporting evidence.'
    assert result['answer_mode'] == 'NO_RESULTS_' + strategy.upper()
    assert result['citations'] == [] and result['grading_status'] == 'NOT_RUN'
    assert result['generation_status'] == ('NOT_RUN' if strategy == 'fixed' else 'COMPLETED')
