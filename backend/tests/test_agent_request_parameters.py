import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from langchain_core.messages import AIMessage
from app.services import agent_workflow

def test_request_scope_settings_and_safe_telemetry(monkeypatch):
    invoke = AsyncMock(return_value='{}')
    schema = agent_workflow.search_knowledge_base.args_schema
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=invoke))
    events = []
    monkeypatch.setattr(agent_workflow, 'enabled', lambda: True)
    monkeypatch.setattr(agent_workflow, 'snapshot', lambda query: {'sha256': 'query-hash'})
    monkeypatch.setattr(agent_workflow, 'event', lambda name, **data: events.append((name, data)))
    params = {'search_mode':'fulltext', 'top_k':9, 'score_threshold':0.22, 'vector_weight':0.4, 'bm25_weight':0.6, 'rerank_enabled':True, 'rerank_top_k':4, 'rerank_score_threshold':0.25, 'rerank_model_id':'private-model', 'context_window':2}
    message = AIMessage(content='', tool_calls=[{'name':'search_knowledge_base', 'id':'1', 'args':{'query':'sensitive query', 'kb_ids':['other'], 'search_mode':'vector', 'top_k':1, 'score_threshold':0.9, 'use_rerank':False, 'rerank_model_id':'other', 'context_window':9}}])
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    state = {'messages':[message], 'kb_ids':['allowed'], 'search_params':params}
    asyncio.run(workflow.run_tools(state, {}))
    assert invoke.await_args.args[0] == {'query':'sensitive query', 'kb_ids':['allowed'], 'search_mode':'fulltext', 'top_k':9, 'score_threshold':0.22, 'vector_weight':0.4, 'bm25_weight':0.6, 'use_rerank':True, 'rerank_top_k':4, 'rerank_score_threshold':0.25, 'rerank_model_id':'private-model', 'context_window':2}
    data = next(data for name, data in events if name == 'agent.actual_query')
    assert data['requested_parameters'] == data['actual_parameters']
    assert data['query'] == {'sha256': 'query-hash'}
    assert len(data['kb_scope_sha256']) == 64
    assert all(secret not in str(data) for secret in ('sensitive query', 'allowed', 'private-model'))

def test_defaults_and_empty_scope(monkeypatch):
    invoke = AsyncMock(return_value='{}')
    schema = agent_workflow.search_knowledge_base.args_schema
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=invoke))
    events = []
    monkeypatch.setattr(agent_workflow, 'event', lambda name, **data: events.append((name, data)))
    message = AIMessage(content='', tool_calls=[{'name':'search_knowledge_base', 'id':'1', 'args':{'query':'q'}}])
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    state = {'messages':[message], 'kb_ids':['allowed'], 'search_params':{}}
    asyncio.run(workflow.run_tools(state, {}))
    assert invoke.await_args.args[0] == {'query':'q', 'kb_ids':['allowed']}
    actual = next(data['actual_parameters'] for name, data in events if name == 'agent.actual_query')
    assert actual['search_mode'] == 'hybrid' and actual['top_k'] == 5
    invoke.reset_mock()
    state['kb_ids'] = []
    result = asyncio.run(workflow.run_tools(state, {}))
    invoke.assert_not_awaited()
    assert json.loads(result['messages'][0].content)['retrieval_status'] == 'NO_RESULTS'

    state['kb_ids'] = ['allowed']
    invoke.side_effect = ValueError('sensitive exception detail')
    result = asyncio.run(workflow.run_tools(state, {}))
    response = json.loads(result['messages'][0].content)
    assert response['retrieval_status'] == 'RETRIEVAL_ERROR'
    assert 'sensitive exception detail' not in result['messages'][0].content
