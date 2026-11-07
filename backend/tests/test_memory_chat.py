"""Offline boundary captures: actual chat/SSE and Agent message construction."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import memory, chat, agent_workflow, grounded_generation, observability
from app.services.memory_commands import parse_preference_commands, resolve_display_preferences, normalize_preference_history
from app.schemas import Message, MessageRole
from langchain_core.messages import AIMessage, HumanMessage


def run(awaitable):
    return asyncio.run(awaitable)


def snapshot(**values):
    return memory.PreferencesSnapshot(values=memory.PreferenceValues(**values),
        sources={key: 'explicit' if key in values else 'default' for key in memory.FIELDS})


@pytest.mark.parametrize('text,key,value', [
    ('以后回答都简洁一点。', 'answer_style', 'concise'),
    ('以后请详细解释。', 'answer_style', 'detailed'),
    ('记住我是初学者。', 'knowledge_level', 'beginner'),
    ('以后解释技术问题先讲原理。', 'add', ['principle_first']),
    ('之后比较方案都使用表格。', 'add', ['table']),
    ('From now on answer briefly.', 'answer_style', 'concise'),
    ('Remember I am a beginner.', 'knowledge_level', 'beginner'),
])
def test_explicit_rules(text, key, value):
    commands, question = parse_preference_commands(text)
    assert len(commands) == 1 and not question
    assert commands[0].changes[key] == value


@pytest.mark.parametrize('text', [
    '这次详细解释一下。', '这道题使用表格。', '这次结合通用知识扩展。', '当前问题要求写代码。',
    '我是初学者。', '不要记住我是初学者。', '以后不要详细解释。', '如果以后回答都简洁一点。',
    '“以后回答都简洁一点。”是什么意思？', '他说以后请详细解释。', '以后回答都简洁一点还是详细一点？',
    'Do not remember I am a beginner.', 'This time explain in detail.',
    '请解释这段话：“例子。以后请详细解释。请继续。”',
    '```\n例子。以后请详细解释。\n```', '以后请详细解释？',
    '不要记录这些话。以后请详细解释。',
])
def test_non_commands(text):
    assert parse_preference_commands(text) == ([], text)


@pytest.mark.parametrize('text,operation,field', [
    ('忘记我的回答风格偏好。', 'delete', 'answer_style'),
    ('删除我之前设置的输出偏好。', 'delete', 'output_preferences'),
    ('恢复默认设置。', 'reset', None),
])
def test_delete_rules(text, operation, field):
    command = parse_preference_commands(text)[0][0]
    assert command.operation == operation and command.field == field


def test_resolver_defaults_current_turn_conditions_and_safe_trace():
    assert resolve_display_preferences(snapshot(), '你好')[0] == ''
    saved = snapshot(answer_style='concise', knowledge_level='beginner', output_preferences=['table', 'principle_first', 'code_example'])
    policy, values, sources = resolve_display_preferences(saved, '这次详细解释Redis原理，不要表格')
    assert values['answer_style'] == 'detailed'
    assert sources['answer_style'] == 'current_turn'
    assert 'table' not in values['output_preferences']
    assert '回答简洁' not in policy
    assert 'table' not in resolve_display_preferences(saved, '你好')[1]['output_preferences']
    assert 'table' not in resolve_display_preferences(saved, '方案上线了吗')[1]['output_preferences']
    assert 'table' in resolve_display_preferences(saved, '比较两种方案')[1]['output_preferences']
    data = observability.sanitize({'preference_values': values, 'preference_sources': {k + '_origin': v for k,v in sources.items()}, 'preferences_read': True, 'redis_fallback': True})
    assert data['preference_values'] == values
    assert data['preference_sources']['answer_style_origin'] == 'current_turn'
    assert 'private text' not in str(observability.sanitize({'answer_style': 'private text', 'answer_style_origin': 'private text'}))
    assert resolve_display_preferences(snapshot(answer_style='concise'), '简洁架构是什么')[2]['answer_style'] == 'explicit'
    assert resolve_display_preferences(snapshot(answer_style='detailed'), '请简洁回答并详细解释')[1]['answer_style'] is None
    policy, values, sources = resolve_display_preferences(snapshot(), '这道题使用表格')
    assert values['output_preferences'] == ['table'] and sources['output_preferences'] == 'current_turn'


def setup_service(monkeypatch, saved=None):
    saved = saved or snapshot()
    store = SimpleNamespace(get_preferences=AsyncMock(return_value=saved),
        update_preference=AsyncMock(return_value=saved), delete_preference=AsyncMock(return_value=saved),
        reset_preferences=AsyncMock(return_value=saved), change_output_preferences=AsyncMock(return_value=saved))
    monkeypatch.setattr(memory, 'get_memory_service', lambda: store)
    captured = []
    async def stream(**kwargs):
        captured.append(kwargs['messages'])
        yield 'offline answer'
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = SimpleNamespace(generate_stream=stream)
    service._retrieve_with_new_service = AsyncMock()
    return service, store, captured


async def collect(service, text, **kwargs):
    return [event async for event in service.chat_stream([Message(role=MessageRole.USER, content=text)], kb_ids=[], **kwargs)]


def test_actual_normal_once_read_and_current_turn_override(monkeypatch):
    service, store, captured = setup_service(monkeypatch, snapshot(answer_style='concise', output_preferences=['table']))
    events = run(collect(service, '这次详细解释Redis原理', memory_scope=memory.PreferenceScope(user_id='trusted')))
    store.get_preferences.assert_awaited_once_with(scope=memory.PreferenceScope(user_id='trusted'))
    assert captured[0][0]['role'] == 'system' and '详细解释' in captured[0][0]['content']
    assert '回答简洁' not in captured[0][0]['content']
    store.update_preference.assert_not_awaited()
    assert events[-1][0] == 'done'


def test_current_output_request_trace_and_rewrite_boundaries(monkeypatch):
    events = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **data: events.append((name, data)))
    service, store, captured = setup_service(monkeypatch)
    run(collect(service, '这道题使用表格'))
    metadata = next(data for name,data in events if name == 'memory.preferences')
    assert metadata['preference_values']['output_preferences'] == ['table']
    assert metadata['preference_sources']['output_preferences_origin'] == 'current_turn'
    store.update_preference.assert_not_awaited()
    store.change_output_preferences.assert_not_awaited()
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(chat, 'Session', Session)
    rewrite = AsyncMock(return_value='rewritten Redis question')
    service.llm_service.rewrite_query = rewrite
    service._retrieve_with_new_service.return_value = ([], {'retrieval_status':'NO_RESULTS'})
    for saved in (snapshot(answer_style='concise'), snapshot()):
        store.get_preferences.return_value = saved
        async def request():
            return [item async for item in service.chat_stream([Message(role='user',content='Redis细节是什么？')], kb_ids=['kb'], use_rewrite=True)]
        result = run(request())
        assert result[-1][0] == 'done'
    assert [call.args for call in rewrite.await_args_list] == [('Redis细节是什么？',), ('Redis细节是什么？',)]
    retrieval = service._retrieve_with_new_service.await_args_list
    assert len(retrieval) == 2
    assert all(call.kwargs['query'] == 'rewritten Redis question' for call in retrieval)
    assert all('preference' not in str(call.kwargs) for call in retrieval)


def test_standalone_and_combined_commands_use_actual_stream(monkeypatch):
    service, store, captured = setup_service(monkeypatch, snapshot(answer_style='concise'))
    events = run(collect(service, '以后回答都简洁一点。'))
    assert '已保存' in str(events) and events[-1][0] == 'done' and captured == []
    store.update_preference.assert_awaited_once_with({'answer_style': 'concise'}, scope=None)
    store.get_preferences.assert_not_awaited()
    events = run(collect(service, '以后回答都简洁一点。你好'))
    assert captured[-1][-1]['content'] == '你好'
    assert 'offline answer' in str(events) and '已保存' in str(events)


def test_redis_write_failure_truthful_read_fallback_answers(monkeypatch):
    fallback = snapshot()
    fallback.redis_fallback = True
    service, store, captured = setup_service(monkeypatch, fallback)
    store.update_preference.side_effect = memory.MemoryUnavailable('offline')
    events = run(collect(service, '以后回答都简洁一点。'))
    assert '保存失败' in str(events) and '已保存' not in str(events) and captured == []
    assert events[-1][0] == 'error' and not any(kind == 'done' for kind,_ in events)
    events = run(collect(service, '以后回答都简洁一点。你好'))
    assert 'offline answer' in str(events) and captured[-1] == [{'role': 'user', 'content': '你好'}]
    error = memory.MemoryUnavailable('post-ack readback failed')
    error.write_acknowledged = True
    store.update_preference.side_effect = error
    events = run(collect(service, '以后回答都简洁一点。'))
    assert events[-1][0] == 'error' and '已提交' in events[-1][1]['message'] and '未能确认' in events[-1][1]['message']
    assert '保存失败' not in events[-1][1]['message']


def test_actual_http_sse_and_trusted_scope(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.chat import router
    from app.core.database import get_session
    service, store, captured = setup_service(monkeypatch, snapshot(answer_style='concise'))
    monkeypatch.setattr(chat.ChatService, '__init__', lambda self: self.__dict__.update(service.__dict__))
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_session] = lambda: None
    app.dependency_overrides[memory.get_preference_scope] = lambda: memory.PreferenceScope(user_id='server-user')
    with TestClient(app) as client:
        response = client.post('/api/chat/completions', json={'messages': [{'role':'user', 'content':'以后回答都简洁一点。'}], 'kb_ids': []})
    assert response.status_code == 200 and 'event: done' in response.text and '已保存' in response.text
    assert captured == []
    store.update_preference.assert_awaited_once_with({'answer_style': 'concise'}, scope=memory.PreferenceScope(user_id='server-user'))


def test_agent_generation_and_auxiliary_inputs_unchanged(monkeypatch):
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    llm = SimpleNamespace(model='offline', get_chat_model=Mock(return_value=object()))
    workflow._get_llm_service = Mock(return_value=llm)
    workflow.tools = []
    routed = []
    def route(state, service, messages, **kwargs):
        routed.append(list(messages))
        return llm, None, None
    workflow._route_post_retrieval_model = route
    monkeypatch.setattr(grounded_generation, '_routed_chat_model', lambda *args: object())
    calls = []
    async def invoke(model, messages, **kwargs):
        if kwargs['stage'] in ('agent', 'agent_chat'):
            assert sum(m.type == 'system' for m in messages) == 1
            assert messages[0].type == 'system'
        calls.append((kwargs['stage'], list(messages)))
        return AIMessage(content='0.9' if kwargs['stage'] == 'quality' else 'chat' if kwargs['stage'] == 'intent' else 'offline answer')
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    policy = resolve_display_preferences(snapshot(answer_style='concise'), '解释Redis')[0]
    state = {'messages': [HumanMessage(content='解释Redis')], 'original_query':'解释Redis', 'citations': [], 'rag_context': '', 'preference_policy':policy}
    run(workflow.analyze_intent(state, {}))
    run(workflow.generate_chat(state, {}))
    run(workflow.agent_node(state, {}))
    state.update(citations=[{'chunk_id':'one'}], rag_context='raw evidence', retrieval_status='SUCCESS')
    run(workflow.grade_documents(state, {}))
    run(workflow.agent_node(state, {}))
    assert [stage for stage,_ in calls] == ['intent', 'agent_chat', 'agent', 'quality', 'agent']
    for index, (stage, messages) in enumerate(calls):
        assert any(policy in str(m.content) for m in messages) == (index in (1, 4))
    assert all(policy not in str(m) for messages in routed for m in messages)
    assert state['rag_context'] == 'raw evidence'
    assert state['preference_policy'] == policy


def test_grounded_actual_provider_prompt_and_router_separation(monkeypatch):
    requests, routed = [], []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'), finish_reason='stop')], usage=None)
    async def create(**kwargs):
        assert [row['role'] for row in kwargs['messages']] == ['system', 'user']
        requests.append(kwargs['messages'])
        return response
    llm = SimpleNamespace(model='offline', _get_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    def route(service, model_id, messages, citations, **kwargs):
        routed.append(list(messages))
        return llm, 'offline', None, None
    monkeypatch.setattr(grounded_generation, '_route_generation', route)
    policy = resolve_display_preferences(snapshot(answer_style='concise'), 'question')[0]
    citations = [{'chunk_id':'one', 'content':'raw evidence'}]
    run(grounded_generation.generate_grounded_candidate(llm, [{'role':'user', 'content':'question'}], citations, preference_policy=policy))
    assert len(requests) == 1
    assert [row['role'] for row in requests[0]] == ['system', 'user']
    assert 'raw evidence' in requests[0][0]['content'] and policy in requests[0][0]['content']
    assert all(policy not in str(row) for row in routed[0])
    assert citations == [{'chunk_id':'one', 'content':'raw evidence'}]


def test_direct_chat_merges_preference_into_existing_system(monkeypatch):
    service, _, captured = setup_service(monkeypatch, snapshot(answer_style='concise'))
    async def provider(**kwargs):
        messages = kwargs['messages']
        assert [message['role'] for message in messages] == ['system', 'user']
        assert messages[0]['content'].startswith('Existing higher-priority instruction')
        assert '回答表达偏好' in messages[0]['content']
        captured.append(messages)
        yield 'offline answer'
    service.llm_service.generate_stream = provider
    async def request():
        return [event async for event in service.chat_stream([
            Message(role='system', content='Existing higher-priority instruction'),
            Message(role='user', content='你好')], kb_ids=[])]
    events = run(request())
    assert events[-1][0] == 'done'
    assert len(captured) == 1


def test_real_agent_graph_reuses_snapshot_and_keeps_tool_grade_inputs(monkeypatch):
    """Run two retrieval rounds through the actual graph and SSE adapter."""
    schema = agent_workflow.search_knowledge_base.args_schema
    saved = snapshot(answer_style='concise')
    service, store, _ = setup_service(monkeypatch, saved)
    llm = SimpleNamespace(model='offline', get_chat_model=lambda **kwargs: object())
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_get_llm_service', lambda *args: llm)
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_route_post_retrieval_model', lambda self, state, llm, messages, **kwargs: (llm, None, None))
    monkeypatch.setattr(grounded_generation, '_routed_chat_model', lambda *args: object())
    all_runs = []
    for preferences in (saved, snapshot()):
        store.get_preferences.return_value = preferences
        calls, tool_inputs, counters = [], [], {'agent': 0, 'quality': 0}
        async def invoke(model, messages, **kwargs):
            stage = kwargs['stage']
            calls.append((stage, [message.model_dump() for message in messages]))
            if stage == 'intent':
                return AIMessage(content='qa')
            if stage == 'quality':
                counters['quality'] += 1
                return AIMessage(content='0.5' if counters['quality'] == 1 else '0.9')
            counters['agent'] += 1
            if counters['agent'] < 3:
                return AIMessage(content='', tool_calls=[{'name':'search_knowledge_base', 'id':str(counters['agent']), 'args':{'query':'Redis detail ' + str(counters['agent'])}}])
            return AIMessage(content='answer [1]')
        async def search(args, **kwargs):
            tool_inputs.append(dict(args))
            return json.dumps({'retrieval_status':'SUCCESS', 'context':'raw evidence', 'citations':[{'chunk_id':'one', 'content':'raw evidence', 'fileId':'file'}]})
        monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
        monkeypatch.setattr(agent_workflow, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=search))
        async def request():
            return [event async for event in service.chat_stream([Message(role=MessageRole.USER, content='Redis实现细节是什么？')], kb_ids=['kb'], mode='agent')]
        events = run(request())
        assert events[-1][0] == 'done' and not any(kind == 'error' for kind,_ in events)
        assert len(tool_inputs) == 2 and counters == {'agent':3, 'quality':2}
        all_runs.append((calls, tool_inputs))
    assert store.get_preferences.await_count == 2  # one read per entire graph request
    assert all_runs[0][1] == all_runs[1][1]
    for stage in ('intent', 'quality'):
        assert [rows for name,rows in all_runs[0][0] if name == stage] == [rows for name,rows in all_runs[1][0] if name == stage]
    preferred_agent_calls = [rows for name,rows in all_runs[0][0] if name == 'agent']
    assert len(preferred_agent_calls) == 3
    # Forced initial retrieval and mandatory retry receive no display segment;
    # the same request-level policy is retained for final composition.
    assert [any(row['type'] == 'system' and '回答表达偏好' in row['content'] for row in rows) for rows in preferred_agent_calls] == [False, False, True]


@pytest.mark.parametrize('mode', ['chat', 'agent'])
def test_real_redis_read_failure_does_not_block_actual_qa(monkeypatch, mode):
    service, _, captured = setup_service(monkeypatch)
    real_store = memory.MemoryService(client=SimpleNamespace(hgetall=AsyncMock(side_effect=ConnectionError('offline'))))
    monkeypatch.setattr(memory, 'get_memory_service', lambda: real_store)
    llm = SimpleNamespace(model='offline', get_chat_model=lambda **kwargs: object())
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_get_llm_service', lambda *args: llm)
    invoke = AsyncMock(return_value=AIMessage(content='offline answer'))
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    events = run(collect(service, '你好', mode=mode))
    assert 'offline answer' in str(events) and events[-1][0] == 'done'
    real_store.client.hgetall.assert_awaited_once()
    if mode == 'agent':
        invoke.assert_awaited_once()
        assert all('回答表达偏好' not in str(m.content) for m in invoke.await_args.args[1])
    else:
        assert captured == [[{'role':'user', 'content':'你好'}]]


def test_native_trace_export_retains_only_bounded_preference_metadata(monkeypatch):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    sdk = Langfuse(public_key='pk-memory-local-sdk', secret_key='synthetic',
        tracer_provider=TracerProvider(), span_exporter=exporter,
        mask=lambda **kw: observability.sanitize(kw.get('data'), final_export=True),
        mask_otel_spans=observability._native_mask_spans,
        should_export_span=observability._native_should_export)
    monkeypatch.setattr(observability, 'enabled', lambda: True)
    monkeypatch.setattr(observability, 'native_enabled', lambda: True)
    monkeypatch.setattr(observability, '_sdk', lambda: sdk)
    monkeypatch.setattr(observability, '_record', lambda *args: None)
    try:
        with observability.span('qa.request'):
            observability.event('memory.preferences', preferences_read=True, redis_fallback=True,
                preference_values={'answer_style':'concise', 'knowledge_level':'beginner', 'output_preferences':['table']},
                preference_sources={'answer_style_origin':'explicit', 'knowledge_level_origin':'explicit', 'output_preferences_origin':'current_turn'},
                arbitrary='private identity and question')
            observability.event('memory.delivery', stage='agent_tool_and_answer', preference_policy_applied=True)
        sdk.flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = dict(spans[0].attributes)
        event = next(stage for stage in json.loads(attrs['langfuse.observation.metadata.stages']) if stage['stage'] == 'memory.preferences')
        assert event['metadata']['preference_values']['answer_style'] == 'concise'
        assert event['metadata']['preference_values']['output_preferences'] == ['table']
        assert event['metadata']['preference_sources']['output_preferences_origin'] == 'current_turn'
        assert event['metadata']['preferences_read'] is True and event['metadata']['redis_fallback'] is True
        delivery = next(s for s in json.loads(attrs['langfuse.observation.metadata.stages']) if s['stage'] == 'memory.delivery')
        assert delivery['metadata']['preference_policy_applied'] is True
        assert 'private identity and question' not in str(attrs)
    finally:
        sdk.shutdown()


@pytest.mark.parametrize('saved', [snapshot(), snapshot(answer_style='concise')])
def test_historic_memory_commands_cannot_override_current_snapshot(monkeypatch, saved):
    service, store, captured = setup_service(monkeypatch, saved)
    history = [Message(role='user', content='以后请详细解释。'),
        Message(role='assistant', content='已保存长期偏好：回答风格为详细。\n\n'),
        Message(role='user', content='之后比较方案都使用表格。之前的问题'),
        Message(role='assistant', content='已保存长期输出偏好：比较方案时使用表格。\n\n之前的回答'),
        Message(role='user', content='恢复默认设置。'),
        Message(role='assistant', content='已删除全部长期偏好，恢复系统默认设置。'),
        Message(role='user', content='你好')]
    async def request():
        return [event async for event in service.chat_stream(history, kb_ids=[])]
    run(request())
    assert [row['content'] for row in captured[0] if row['role'] != 'system'] == ['之前的问题', '之前的回答', '你好']
    assert '详细' not in str(captured[0]) and '表格' not in str(captured[0])
    store.update_preference.assert_not_awaited()
    store.reset_preferences.assert_not_awaited()
    store.change_output_preferences.assert_not_awaited()


@pytest.mark.parametrize('command', ['恢复默认设置。', '以后请详细解释。', '之后比较方案都使用表格。'])
def test_history_ending_in_assistant_never_replays_command(monkeypatch, command):
    service, store, _ = setup_service(monkeypatch)
    async def request():
        return [event async for event in service.chat_stream([
            Message(role='user', content=command), Message(role='assistant', content='之前的回复')], kb_ids=[])]
    run(request())
    store.update_preference.assert_not_awaited()
    store.reset_preferences.assert_not_awaited()
    store.change_output_preferences.assert_not_awaited()


def test_unpaired_assistant_content_is_never_filtered():
    messages = [Message(role='user', content='说明资料中的删除流程'),
                Message(role='assistant', content='已删除输出偏好，恢复系统默认值。')]
    assert normalize_preference_history(messages) == messages


def test_scheduled_third_retry_uses_actual_grading_decision(monkeypatch):
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    llm = SimpleNamespace(model='offline', get_chat_model=lambda **kwargs: object())
    workflow._get_llm_service = lambda state: llm
    workflow._route_post_retrieval_model = lambda *args, **kwargs: (llm, None, None)
    workflow.tools = []
    monkeypatch.setattr(grounded_generation, '_routed_chat_model', lambda *args: object())
    invocations = []
    async def invoke(model, messages, **kwargs):
        invocations.append((kwargs['stage'], list(messages)))
        return AIMessage(content='0.5' if kwargs['stage'] == 'quality' else 'answer')
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    policy = resolve_display_preferences(snapshot(answer_style='concise'), 'question')[0]
    state = {'messages':[HumanMessage(content='question')], 'original_query':'question', 'citations':[{'chunk_id':'one'}],
        'rag_context':'raw evidence', 'retrieval_status':'SUCCESS', 'retry_count':2, 'preference_policy':policy}
    decision = run(workflow.grade_documents(state, {}))
    assert decision['retry_count'] == 3 and decision['retrieval_retry_scheduled'] is True
    state.update({k:v for k,v in decision.items() if k != 'messages'})
    run(workflow.agent_node(state, {}))
    assert all(policy not in message.content for message in invocations[-1][1])
    exhausted = run(workflow.grade_documents(state, {}))
    assert exhausted['retrieval_retry_scheduled'] is False
    state.update({k:v for k,v in exhausted.items() if k != 'messages'})
    run(workflow.agent_node(state, {}))
    assert any(policy in message.content for message in invocations[-1][1])
