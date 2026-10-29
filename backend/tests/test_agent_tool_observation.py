import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.tools import retrieval_tool


class Session:
    def __init__(self, engine): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def test_tool_attempt_rerank_and_original_delivery_do_not_change_output(monkeypatch):
    monkeypatch.setattr(retrieval_tool, 'Session', Session)
    rows = [{'id': 'a', 'content': 'expanded neighbour', 'original_content': 'original',
             'is_expanded': True, 'score': .2},
            {'id': 'b', 'content': 'other', 'score': .1}]
    service = SimpleNamespace(search=AsyncMock())
    async def search(**kwargs):
        return ([dict(r) for r in rows], {'retrieval_status': 'SUCCESS'})
    service.search.side_effect = search
    monkeypatch.setattr(retrieval_tool, 'get_retrieval_service', lambda: service)
    monkeypatch.setattr(retrieval_tool, 'RerankService', lambda _: SimpleNamespace(
        rerank=lambda *a, **k: [{'index': 1, 'score': .4}, {'index': 0, 'score': .9}]))
    events = []
    monkeypatch.setattr(retrieval_tool, 'event', lambda name, **data: events.append((name, data)))
    token = retrieval_tool.retrieval_attempt.set('attempt-1')
    try:
        output = asyncio.run(retrieval_tool.search_knowledge_base.coroutine(
            query='private query', kb_ids=['kb'], use_rerank=True, rerank_score_threshold=.5,
            rerank_top_k=1))
    finally:
        retrieval_tool.retrieval_attempt.reset(token)
    data = json.loads(output)
    assert [c['chunk_id'] for c in data['citations']] == ['a']
    assert data['citations'][0]['content'] == 'original'
    events = dict(events)
    assert all(e['retrieval_attempt_id'] == 'attempt-1' for e in events.values())
    assert events['rerank.batch_output']['results'][0]['id'] == 'b'
    assert events['rerank.filtered']['output_ids'] == ['a']
    assert events['rerank.selected']['output_ids'] == ['a']
    delivered = events['context.delivery']['candidates'][0]
    assert delivered['content_role'] == 'context_sent' and delivered['content_field'] == 'original_content' and delivered['expanded']
    assert delivered['content_sha256'] == hashlib.sha256(b'original').hexdigest()
    assert 'private query' not in json.dumps(events)


def test_two_attempts_quality_boundary_and_retry_are_linked(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.services import agent_workflow
    from unittest.mock import Mock
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    outputs = [json.dumps({'context': '', 'citations': [], 'retrieval_status': 'NO_RESULTS'}),
               json.dumps({'context': 'x' * 2100, 'citations': [{'id': 'a'}], 'retrieval_status': 'SUCCESS'})]
    seen = []
    async def invoke(args, config):
        seen.append((retrieval_tool.retrieval_attempt.get(), args['query']))
        return outputs[len(seen)-1]
    schema = agent_workflow.search_knowledge_base.args_schema
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=invoke))
    events = []
    monkeypatch.setattr(agent_workflow, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(agent_workflow, 'model_response', lambda *args, **kwargs: None)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='0.9')))
    workflow._get_llm_service = Mock(return_value=SimpleNamespace(model='fake', get_chat_model=Mock(return_value=model)))
    state = {'kb_ids': ['kb'], 'search_params': {}, 'original_query': 'question', 'retry_count': 0}
    for index in range(2):
        state['messages'] = [AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base',
                            'id': str(index), 'args': {'query': f'query {index}'}}])]
        tool_result = asyncio.run(workflow.run_tools(state, {}))
        state.update(tool_result)
        state.update(asyncio.run(workflow.process_tool_output(state)))
        state.update(asyncio.run(workflow.grade_documents(state, {})))
    assert seen[0][0] and seen[1][0] and seen[0][0] != seen[1][0]
    queries = [d for n, d in events if n == 'agent.actual_query']
    quality = [d for n, d in events if n == 'agent.quality']
    decisions = [d for n, d in events if n == 'agent.retry_decision']
    assert [q['retrieval_attempt_id'] for q in queries] == [s[0] for s in seen]
    assert [q['retrieval_attempt_id'] for q in quality] == [s[0] for s in seen]
    assert quality[0]['score_valid'] is None and quality[0]['reason'] == 'NO_CITATIONS'
    assert quality[1]['score_valid'] is True and quality[1]['score_parsed'] == .9
    assert quality[1]['context_chars_examined'] == 2000 and quality[1]['context_clipped']
    assert quality[1]['context_sent_sha256'] == hashlib.sha256(('x' * 2000).encode()).hexdigest()
    assert [d['action'] for d in decisions] == ['SCHEDULED', 'NOT_SCHEDULED']
    assert decisions[1]['retrieval_attempt_count'] == 2


def test_provider_messages_observe_all_agent_calls_and_hide_reasoning(monkeypatch):
    from app.services import agent_workflow
    from langchain_core.messages import HumanMessage, AIMessage
    from unittest.mock import Mock
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow.tools = []
    model = SimpleNamespace(bind_tools=lambda tools: model)
    workflow._get_llm_service = Mock(return_value=SimpleNamespace(model='fake', get_chat_model=Mock(return_value=model)))
    sent, events, captured = [], [], []
    async def invoke(model, messages, config, stage, model_name):
        sent.append((stage, messages))
        if stage == 'intent': return AIMessage(content='qa')
        if stage == 'quality': return AIMessage(content='0.9')
        if stage == 'agent': return AIMessage(content='Thinking: hidden model reasoning\n\nvisible',
            tool_calls=[{'name':'search_knowledge_base', 'args': {'query': 'q'}, 'id':'tool-1'}])
        return AIMessage(content='hello')
    def fake_capture(value, *, content_role):
        captured.append((content_role, value))
        return {'role': content_role, 'value': value}
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    monkeypatch.setattr(agent_workflow, 'enabled', lambda: True)
    monkeypatch.setattr(agent_workflow, 'capture', fake_capture)
    monkeypatch.setattr(agent_workflow, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(agent_workflow, 'model_response', lambda *args, **kwargs: None)
    asyncio.run(workflow.analyze_intent({'messages': [HumanMessage(content='explain the structure of atoms')]}, {}))
    asyncio.run(workflow.agent_node({'messages': [HumanMessage(content='question')], 'citations': []}, {}))
    asyncio.run(workflow.grade_documents({'citations': [{'id':'a'}], 'original_query':'question',
                                         'rag_context':'evidence', 'retry_count':0}, {}))
    asyncio.run(workflow.generate_chat({'messages': [HumanMessage(content='hi')]}, {}))
    requests = [data for name, data in events if name == 'llm.request']
    responses = [data for name, data in events if name == 'llm.visible_response']
    assert [d['role'] for d in requests] == ['intent', 'agent', 'quality', 'agent_chat']
    assert [d['role'] for d in responses] == [d['role'] for d in requests]
    for request, (_, messages) in zip(requests, sent):
        assert [row['role'] for row in request['input']['value']] == [m.type for m in messages]
        assert [row['content'] for row in request['input']['value']] == [m.content for m in messages]
    assert 'hidden model reasoning' not in str(captured)
    assert responses[1]['output']['value'] == '[REDACTED]visible'
    assert all(role in ('provider_request', 'provider_response') for role, _ in captured)


def test_agent_request_projection_discloses_omissions_and_never_captures_reasoning(monkeypatch):
    from app.services import agent_workflow
    from langchain_core.messages import AIMessage, HumanMessage
    events = []
    monkeypatch.setattr(agent_workflow, 'enabled', lambda: True)
    monkeypatch.setattr(agent_workflow, 'capture', lambda value, **kwargs: {'value': value})
    monkeypatch.setattr(agent_workflow, 'event', lambda name, **data: events.append((name, data)))
    messages = [HumanMessage(content='question', name='user-name'), AIMessage(
        content=[{'type':'text', 'text':'answer'}, {'type':'reasoning', 'text':'private reasoning'}],
        additional_kwargs={'reasoning_content':'private metadata'})]
    agent_workflow._observe_model_request('agent', messages, 'fake')
    _, record = events[0]
    assert record['reason_code'] == 'provider_message_projection'
    assert record['is_exact_provider_payload'] is False
    assert set(record['projection_excludes']) == {'reasoning', 'provider_specific_fields', 'unsupported_content_blocks'}
    assert record['input']['value'][0]['name'] == 'user-name'
    assert record['input']['value'][1]['content'] == [{'type':'text', 'text':'answer'}]
    assert 'private reasoning' not in str(record) and 'private metadata' not in str(record)
