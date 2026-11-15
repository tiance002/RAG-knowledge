"""Capture the actual invocation view; providers are scripted offline stubs."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services import agent_workflow as aw
from app.services.agent_context_budget import history_as_sent, request_chars
from app.services.agent_tool_results import AgentToolResultStore


def source(number=1, size=1200, parent=False):
    return {'source_id': number, 'chunk_id': f'chunk-{number}', 'fileId': 'file',
            'fileName': 'manual.pdf', 'kb_id': 'allowed',
            'content': f'CANONICAL-BODY-{number} ' + '文' * size,
            'evidence_relation': 'parent' if parent else 'seed'}


def exchange(sources, ident='search'):
    return [AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base',
        'id': ident, 'args': {'query': 'topic'}}]), ToolMessage(
        name='search_knowledge_base', tool_call_id=ident, content=json.dumps({
            'retrieval_status': 'SUCCESS', 'source_mapping_status': 'VERIFIED',
            'source_ids': [c['source_id'] for c in sources], 'citations': sources,
            'context': aw._source_context(sources)}, ensure_ascii=False))]


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', 'AGENT_EVIDENCE_VIEW_V2')
    settings = SimpleNamespace(GROUNDED_REQUEST_MAX_CHARS=32000,
        AGENT_TOOL_RESULT_INLINE_CHARS=512, AGENT_TOOL_RESULT_STORE_DIR=str(tmp_path),
        AGENT_TOOL_RESULT_MAX_BYTES=8 * 1024 * 1024, AGENT_TOOL_RESULT_MAX_ENTRIES=32)
    monkeypatch.setattr(aw, 'get_settings', lambda: settings)
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = [aw.search_knowledge_base, aw.read_knowledge_context, aw.read_tool_result]
    obj._tool_result_store = None
    obj._tool_result_store_enabled = True
    model = SimpleNamespace(kwargs={'tools': []})
    obj._get_llm_service = lambda state: SimpleNamespace(model='scripted-offline',
        get_chat_model=lambda **kwargs: SimpleNamespace(bind_tools=lambda tools: model))
    yield obj, settings, tmp_path
    obj.close()
    assert list(tmp_path.iterdir()) == []


def initial(sources):
    return {'messages': [HumanMessage(content='topic'), *exchange(sources)],
        'original_query': 'topic', 'kb_ids': ['allowed'], 'source_registry': sources,
        'citations': sources, 'retrieval_status': 'SUCCESS', 'retrieval_attempt_count': 1}


def test_saved_index_read_and_citations_use_actual_request_body(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [source()]
    state = initial(sources)
    original = copy.deepcopy(state)
    requests = []

    async def provider(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        tools = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        if len(requests) == 1:
            index = tools[0]
            assert index['tool_result_status'] == 'SAVED_INCOMPLETE'
            assert index['sources'][0]['source_id'] == 's1'
            assert 'INCOMPLETE' in index['sources'][0]['preview_status']
            assert index['delivered_source_ids'] == []
            assert sources[0]['content'] not in json.dumps(tools, ensure_ascii=False)
            return AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
                'id': 'read', 'args': {'result_handle': index['result_handle'], 'source_ids': ['s1']}}])
        assert tools[-1]['tool_result_read'] is True
        assert tools[-1]['source_ids'] == ['s1']
        assert tools[-1]['delivered_source_ids'] == ['s1']
        assert f"Document s1 (Source: manual.pdf):\n{sources[0]['content']}" == tools[-1]['context']
        return AIMessage(content='事实。[s1]', response_metadata={'finish_reason': 'stop'})

    monkeypatch.setattr(aw, 'invoke_provider_model', provider)

    async def run():
        first = await obj.agent_node(state, {})
        assert first['model_evidence_source_ids'] == []
        assert first['context_delivery']['deferred_source_ids'] == [1]
        assert state == original
        next_state = {**state, **first, 'messages': [*state['messages'], *first['messages']]}
        read = await obj.run_tools(next_state, {})
        next_state = {**next_state, **read, 'messages': [*next_state['messages'], *read['messages']]}
        observation = await obj.process_tool_output(next_state)
        next_state.update(observation)
        return await obj.agent_node(next_state, {})

    result = asyncio.run(run())
    assert len(requests) == 2
    assert result['model_evidence_source_ids'] == [1]
    assert result['messages'][-1].content == '事实。[1]'
    assert result['citations'] == sources
    assert result['answer_mode'] == 'GROUNDED_GENERATION'
    assert all(request_chars(rows) <= 32000 for rows in requests)


def test_small_result_keeps_existing_inline_body(workflow, monkeypatch):
    obj, settings, _ = workflow
    settings.AGENT_TOOL_RESULT_INLINE_CHARS = 6000
    sources = [source(size=30)]
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='事实。[s1]')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(initial(sources), {}))
    data = json.loads(next(m.content for m in captured if isinstance(m, ToolMessage)))
    assert 'result_handle' not in data
    assert sources[0]['content'] in data['context']
    assert result['model_evidence_source_ids'] == [1]


def test_raw_duplicate_body_above_threshold_stays_inline_when_projected_fits(workflow, monkeypatch):
    obj, settings, _ = workflow
    settings.AGENT_TOOL_RESULT_INLINE_CHARS = 6000
    sources = [source(size=4400)]
    state = initial(sources)
    assert len(state['messages'][-1].content) > 6000
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='事实。[s1]')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    data = json.loads(next(m.content for m in captured if isinstance(m, ToolMessage)))
    assert 'result_handle' not in data
    assert len(json.dumps(data, ensure_ascii=False)) < 6000
    assert sources[0]['content'] in data['context']
    assert result['model_evidence_source_ids'] == [1]


@pytest.mark.parametrize('mode', ['save-failed', 'OFF'])
def test_store_failure_and_off_use_whole_source_budget(workflow, monkeypatch, mode):
    obj, _, _ = workflow
    if mode == 'OFF':
        monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', 'OFF')
    else:
        failing = SimpleNamespace(save=lambda _: (_ for _ in ()).throw(OSError('disk failure')))
        monkeypatch.setattr(obj, '_result_store', lambda registry: failing)
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='事实。[s1]')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    sources = [source()]
    result = asyncio.run(obj.agent_node(initial(sources), {}))
    assert result['model_evidence_source_ids'] == [1]
    assert sources[0]['content'] in next(m.content for m in captured if isinstance(m, ToolMessage))


def test_parent_cap_counts_only_whole_bodies_actually_sent(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [source(n, size=50, parent=True) for n in range(1, 9)]
    state = initial(sources)
    store = obj._result_store(sources)
    index = store.save(state['messages'][-1].content)
    data = store.read(index['result_handle'], limit=20)
    state['messages'].extend([AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
        'id': 'read', 'args': {'result_handle': index['result_handle'], 'limit': 20}}]),
        ToolMessage(name='read_tool_result', tool_call_id='read', content=json.dumps(data, ensure_ascii=False))])
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='事实。[s1]')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    assert result['context_delivery']['unique_parent_count'] == 6
    assert result['model_evidence_source_ids'] == [1, 2, 3, 4, 5, 6]
    data = json.loads([m for m in captured if isinstance(m, ToolMessage)][-1].content)
    assert data['delivered_source_ids'] == ['s1', 's2', 's3', 's4', 's5', 's6']
    assert sources[6]['content'] not in data['context']
    assert request_chars(captured) <= 32000


def test_default_agent_history_requires_fresh_search(workflow, monkeypatch):
    obj, _, _ = workflow
    history = {**source(), 'origin': 'HISTORY_REUSE', 'verification_status': 'VERIFIED'}
    state = {'messages': [HumanMessage(content='继续')], 'original_query': '继续',
        'kb_ids': ['allowed'], 'history_evidence': [history]}
    assert aw._prepare_history_evidence(state) == {}
    monkeypatch.setattr(aw, 'invoke_provider_model', AsyncMock(return_value=AIMessage(content='first draft')))
    result = asyncio.run(obj.agent_node(state, {}))
    assert result['messages'][-1].tool_calls[0]['name'] == 'search_knowledge_base'
    assert result['model_evidence_source_ids'] == []


def test_retained_history_registers_all_verified_sources_as_readable_index(workflow, monkeypatch):
    obj, _, _ = workflow
    history = [{**source(n, size=40), 'origin': 'HISTORY_REUSE',
        'verification_status': 'VERIFIED', 'history_turn_id': str(n)} for n in range(1, 8)]
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base',
            'id': 'fresh', 'args': {'query': '继续'}}])
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    state = {'messages': [HumanMessage(content='继续')], 'original_query': '继续',
        'kb_ids': ['allowed'], 'history_evidence': history, 'retain_retrieval_history': True}
    result = asyncio.run(obj.agent_node(state, {}))
    assert len(result['source_registry']) == 7
    data = json.loads(next(m.content for m in captured if isinstance(m, ToolMessage)))
    assert len(data['sources']) == 7
    assert result['model_evidence_source_ids'] == []
    assert result['messages'][1].additional_kwargs['historical_retrieval']


def test_history_redaction_keeps_tool_protocol_pairs_and_assistant_dialogue():
    sources = [source()]
    messages = [HumanMessage(content='before'), *exchange(sources),
        AIMessage(content='prior answer'), HumanMessage(content='now')]
    result = history_as_sent(messages, original_query='now')
    assert result[-2].content == 'prior answer'
    assert result[1].tool_calls == messages[1].tool_calls
    assert result[2].tool_call_id == messages[2].tool_call_id
    assert 'fresh search' in result[2].content
    assert sources[0]['content'] in messages[2].content
    assert history_as_sent(messages, original_query='now', retain_retrieval_history=True) == messages


def test_tool_result_reads_have_finite_server_budget(workflow):
    obj, _, _ = workflow
    sources = [source()]
    store = obj._result_store(sources)
    index = store.save(exchange(sources)[-1].content)
    state = {'source_registry': sources, 'messages': [AIMessage(content='', tool_calls=[{
        'name': 'read_tool_result', 'id': 'read', 'args': {'result_handle': index['result_handle']}}])],
        'executed_tool_result_reads': [str(n) for n in range(6)]}
    result = asyncio.run(obj.run_tools(state, {}))
    assert result['retrieval_blocked'] is True
    assert json.loads(result['messages'][0].content)['execution_status'] == 'TOOL_RESULT_READ_LIMIT'


def test_saved_source_can_be_reread_after_delivery_budget_omitted_it(workflow):
    obj, _, _ = workflow
    sources = [source()]
    store = obj._result_store(sources)
    index = store.save(exchange(sources)[-1].content)
    args = {'result_handle': index['result_handle'], 'source_ids': ['s1']}
    key = json.dumps([args['result_handle'], ['s1'], 0, 3], sort_keys=True)
    state = {'source_registry': sources, 'model_evidence_source_ids': [],
        'messages': [AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
            'id': 'read-again', 'args': args}])], 'executed_tool_result_reads': [key]}
    result = asyncio.run(obj.run_tools(state, {}))
    data = json.loads(result['messages'][0].content)
    assert data['source_ids'] == [1]
    assert sources[0]['content'] in data['context']
    assert len(result['executed_tool_result_reads']) == 2
    assert result['retrieval_blocked'] is False


def test_multiple_large_results_keep_distinct_handles_and_raw_metadata(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [source(1), source(2)]
    state = initial(sources)
    state['messages'] = [HumanMessage(content='topic'), *exchange(sources[:1], 'one'),
        *exchange(sources[1:], 'two')]
    payload = json.loads(state['messages'][-1].content)
    payload['route_diagnostics'] = {'status': 'PARTIAL', 'unavailable_route': 'bm25'}
    state['messages'][-1].content = json.dumps(payload, ensure_ascii=False)
    original = copy.deepcopy(state['messages'])
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base',
            'id': 'next', 'args': {'query': 'new topic'}}])
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    indices = [json.loads(m.content) for m in captured if isinstance(m, ToolMessage)]
    assert len({index['result_handle'] for index in indices}) == 2
    assert [index['sources'][0]['source_id'] for index in indices] == ['s1', 's2']
    assert state['messages'] == original
    assert obj._tool_result_store.resolve(indices[1]['result_handle']) == original[-1].content
    assert result['model_evidence_source_ids'] == []


def test_four_rounds_of_fifty_sources_bound_indices_and_report_all_unread(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [{**source(n, size=80), 'source_metadata': {'semantic_note': 'm' * 600}}
        for n in range(1, 201)]
    state = initial(sources)
    state['messages'] = [HumanMessage(content='topic')]
    for round_no in range(4):
        state['messages'].extend(exchange(sources[round_no * 50:(round_no + 1) * 50], str(round_no)))
    original = copy.deepcopy(state['messages'])
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base',
            'id': 'next', 'args': {'query': 'next'}}])
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    indices = [json.loads(m.content) for m in captured if isinstance(m, ToolMessage)]
    assert len(indices) == 4
    assert all(len(index['sources']) <= 10 and index['total_sources'] == 50 for index in indices)
    assert result['context_delivery']['compacted_result_indices'] > 0
    assert result['context_delivery']['delivery_status'] == 'PARTIAL'
    assert result['model_evidence_source_ids'] == []
    assert set(result['context_delivery']['omitted_source_ids']) == set(range(1, 201))
    assert request_chars(captured) <= 32000
    assert [m.tool_call_id for m in captured if isinstance(m, ToolMessage)] == ['0', '1', '2', '3']
    assert state['messages'] == original


def test_capped_index_read_does_not_mark_unread_sources_delivered(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [source(n, size=30) for n in range(1, 51)]
    state = initial(sources)
    store = obj._result_store(sources)
    index = store.save(state['messages'][-1].content)
    data = store.read(index['result_handle'], offset=49)
    state['messages'].extend([AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
        'id': 'read-last', 'args': {'result_handle': index['result_handle'], 'offset': 49}}]),
        ToolMessage(name='read_tool_result', tool_call_id='read-last', content=json.dumps(data, ensure_ascii=False))])
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='事实。[s50]')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    assert result['model_evidence_source_ids'] == [50]
    assert set(result['context_delivery']['omitted_source_ids']) == set(range(1, 50))
    assert result['context_delivery']['delivery_status'] == 'PARTIAL'
    assert result['messages'][-1].content == '事实。[50]'
    assert [c['source_id'] for c in result['citations']] == [50]


def test_corrupted_saved_result_falls_back_without_false_delivery(workflow, monkeypatch):
    obj, _, _ = workflow
    sources = [source()]
    state = initial(sources)
    store = obj._result_store(sources)
    index = store.save(state['messages'][-1].content)
    store._entries[index['result_handle']]['path'].write_text('corrupted', encoding='utf-8')
    state['messages'].append(AIMessage(content='', tool_calls=[{'name': 'read_tool_result',
        'id': 'read', 'args': {'result_handle': index['result_handle']}}]))
    read = asyncio.run(obj.run_tools(state, {}))
    failure = json.loads(read['messages'][0].content)
    assert failure['source_ids'] == []
    assert failure['execution_status'] == 'SAVED_RESULT_UNAVAILABLE'
    assert failure['retrieval_status'] == 'UNKNOWN'
    state = {**state, **read, 'messages': [*state['messages'], *read['messages']]}
    captured = []
    async def provider(model, messages, **kwargs):
        captured.extend(messages)
        return AIMessage(content='candidate answer')
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    result = asyncio.run(obj.agent_node(state, {}))
    # The original canonical search is still available and uses whole-source
    # fallback; the failed read is not promoted into delivered evidence.
    assert result['model_evidence_source_ids'] == [1]
    assert sources[0]['content'] in next(m.content for m in captured if isinstance(m, ToolMessage))
    assert result['answer_mode'] == 'EXTRACTIVE_FALLBACK'


@pytest.mark.parametrize('outcome', ['complete', 'error', 'cancel'])
def test_chat_always_closes_request_store(monkeypatch, tmp_path, outcome):
    from app.schemas import Message, MessageRole
    from app.services import chat
    store = AgentToolResultStore([source()], root=tmp_path)
    store.save(exchange([source()])[-1].content)
    started = asyncio.Event()
    class Graph:
        async def astream_events(self, state, version):
            started.set()
            if outcome == 'error':
                raise RuntimeError('scripted graph failure')
            if outcome == 'cancel':
                await asyncio.Future()
            if False:
                yield None
    obj = SimpleNamespace(app=Graph(), close=store.close)
    monkeypatch.setattr(aw, 'AgentWorkflow', lambda: obj)
    service = chat.ChatService.__new__(chat.ChatService)
    service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=20)
    service._resolved_llm = lambda _: SimpleNamespace(model='fake', base_url='http://fixture', api_key='fake')
    async def run():
        async def consume():
            return [row async for row in service._chat_stream_langgraph(
                [Message(role=MessageRole.USER, content='q')], ['allowed'], 5, 0, None)]
        pending = asyncio.create_task(consume())
        await started.wait()
        if outcome == 'cancel':
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            rows = await pending
            assert rows[-1][0] == ('done' if outcome == 'complete' else 'error')
    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []


def test_cancellation_waits_for_pending_disk_write_before_cleanup(tmp_path):
    started, release = threading.Event(), threading.Event()
    sources = [source()]
    store = AgentToolResultStore(sources, root=tmp_path)
    def save():
        started.set()
        release.wait(timeout=5)
        return store.save(exchange(sources)[-1].content)
    async def run():
        pending = asyncio.create_task(aw._store_io(save))
        await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        store.close()
    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
