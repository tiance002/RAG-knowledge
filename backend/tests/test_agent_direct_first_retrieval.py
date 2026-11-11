"""P4 workflow contracts with synthetic provider results, zero external calls."""
import asyncio
import copy
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
    monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', 'OFF')
    monkeypatch.delenv('RAG_AGENT_FIRST_RETRIEVAL', raising=False)


def state():
    return {'messages': [HumanMessage(content='依据资料说明收入是多少？')],
            'original_query': '依据资料说明收入是多少？', 'intent': 'qa',
            'has_prior_conversation': False,
            'kb_ids': ['allowed'], 'search_params': {'search_mode': 'hybrid',
                'top_k': 10, 'score_threshold': .1, 'vector_weight': .7,
                'bm25_weight': .3, 'rerank_enabled': True,
                'rerank_top_k': 5, 'rerank_score_threshold': .2, 'context_window': 1},
            'retry_count': 0, 'last_score': None}


def router():
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = [SimpleNamespace(name='search_knowledge_base')]
    return obj


@pytest.mark.parametrize('mode', [None, 'MODEL', 'invalid'])
def test_default_and_unknown_mode_keep_model_entry(monkeypatch, mode):
    if mode is not None:
        monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', mode)
    assert router().route_based_on_intent(state()) == 'qa'


@pytest.mark.parametrize('kind', ['chat', 'no_scope', 'image', 'unresolved_history', 'already_executed', 'other_tool'])
def test_unsupported_requests_keep_existing_path(monkeypatch, kind):
    monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', 'DIRECT_KB_V1')
    obj, current = router(), state()
    if kind == 'chat': current['intent'] = 'chat'
    if kind == 'no_scope': current['kb_ids'] = []
    if kind == 'image': current['messages'] = [HumanMessage(content=[{'type': 'text', 'text': '图片问题'}])]
    if kind == 'unresolved_history': current['messages'].insert(0, HumanMessage(content='上一问'))
    if kind == 'already_executed': current['executed_retrieval_requests'] = ['old']
    if kind == 'other_tool': obj.tools.append(SimpleNamespace(name='other'))
    assert obj.route_based_on_intent(current) == current['intent']


@pytest.mark.parametrize('rewrite', ['明确实体后的收入问题', '依据资料说明收入是多少？'])
def test_followup_keeps_model_entry_even_with_truthy_rewrite(monkeypatch, rewrite):
    monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', 'DIRECT_KB_V1')
    current = state()
    current['messages'].insert(0, HumanMessage(content='上一问'))
    current['retrieval_query'] = rewrite
    assert router().route_based_on_intent(current) == 'qa'


@pytest.mark.parametrize('history', [True, None])
def test_prior_or_unknown_raw_history_keeps_model_entry(monkeypatch, history):
    monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', 'DIRECT_KB_V1')
    current = state()
    current['has_prior_conversation'] = history
    assert router().route_based_on_intent(current) == 'qa'


def result():
    return json.dumps({'context': '收入：42。单位：万元。', 'retrieval_status': 'SUCCESS',
        'citations': [{'chunk_id': 'c1', 'fileId': 'file1', 'fileName': 'synthetic.pdf',
                       'location': '第 1 页', 'content': '收入：42。单位：万元。'}]})


def test_direct_dispatch_reuses_exact_tool_contract_and_is_not_model_call(monkeypatch):
    current = state()
    current['retrieval_query'] = '明确实体后的收入问题'
    before = copy.deepcopy(current)
    schema = aw.search_knowledge_base.args_schema
    invoke = AsyncMock(return_value=result())
    monkeypatch.setattr(aw, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=invoke))
    model = AsyncMock(side_effect=AssertionError('No model call for deterministic dispatch'))
    monkeypatch.setattr(aw, 'invoke_provider_model', model)
    events = []
    monkeypatch.setattr(aw, 'event', lambda name, **values: events.append((name, values)))
    out = asyncio.run(router().direct_first_retrieval(current, {}))
    assert current == before
    args = invoke.call_args.args[0]
    assert args == {**{k: v for k, v in current['search_params'].items() if k != 'rerank_enabled'},
                   'use_rerank': True, 'kb_ids': ['allowed'],
                   'query': current['retrieval_query'], 'original_query': current['original_query']}
    dispatch, tool = out['messages']
    assert dispatch.additional_kwargs['execution_origin'] == 'workflow_direct_retrieval'
    assert tool.tool_call_id == dispatch.tool_calls[0]['id']
    payload = json.loads(tool.content)
    assert '42' in payload['context'] and '万元' in payload['context']
    assert payload['citations'][0]['source_id'] == 1
    assert out['retrieval_attempt_count'] == 1
    assert any(name == 'agent.actual_query' and values['origin'] == 'workflow_direct_retrieval'
               for name, values in events)
    model.assert_not_called()


@pytest.mark.parametrize('low_first_grade', [False, True])
def test_actual_graph_saves_only_first_selection_and_preserves_grading_retry(monkeypatch, low_first_grade):
    schema = aw.search_knowledge_base.args_schema
    calls, tool_args = [], []
    class FakeModel:
        kwargs = {}
        def bind_tools(self, tools): return self
    model = FakeModel()
    class FakeService:
        model = 'synthetic'
        def get_chat_model(self, **kwargs): return model
    monkeypatch.setattr(aw, 'LLMService', FakeService)
    async def tool(args, **kwargs):
        tool_args.append(copy.deepcopy(args))
        return result()
    monkeypatch.setattr(aw, 'search_knowledge_base', SimpleNamespace(
        name='search_knowledge_base', args_schema=schema, ainvoke=tool))
    async def provider(model, messages, *, stage, **kwargs):
        calls.append(stage)
        if stage == 'quality':
            score = '.5' if low_first_grade and calls.count('quality') == 1 else '.9'
            return AIMessage(content=score)
        prior = [m for m in messages if isinstance(m, ToolMessage)]
        if not prior or (low_first_grade and len(prior) == 1):
            return AIMessage(content='', tool_calls=[{'id': f'call-{len(calls)}',
                'name': 'search_knowledge_base', 'args': {'query': '收入明细第二表达' if prior else state()['original_query']}}])
        return AIMessage(content='收入为42万元。[1]', response_metadata={'finish_reason': 'stop'})
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    runs = []
    for mode in ('MODEL', 'DIRECT_KB_V1'):
        monkeypatch.setenv('RAG_AGENT_FIRST_RETRIEVAL', mode)
        calls.clear(); tool_args.clear()
        out = asyncio.run(aw.AgentWorkflow().app.ainvoke(state()))
        runs.append((out, list(calls), copy.deepcopy(tool_args)))
    base, candidate = runs
    assert candidate[1] == base[1][1:]
    assert base[1][0] == 'agent'
    assert candidate[2] == base[2]
    assert candidate[0]['last_score'] == base[0]['last_score'] == .9
    assert candidate[0]['retry_count'] == base[0]['retry_count'] == int(low_first_grade)
    assert candidate[0]['retrieval_attempt_count'] == base[0]['retrieval_attempt_count'] == 1 + int(low_first_grade)
    assert candidate[0]['messages'][-1].content == base[0]['messages'][-1].content
    assert candidate[0]['citations'] == base[0]['citations']
    assert candidate[0]['generation_status'] == base[0]['generation_status'] == 'COMPLETED'
