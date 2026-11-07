"""Synthetic Agent contract regressions; no providers, storage or paid calls."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.services import agent_workflow as aw
from test_e20_context_delivery import db, seed_row


@pytest.fixture(autouse=True)
def isolate_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'agent-synthetic-events.jsonl'))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')


def workflow():
    return aw.AgentWorkflow.__new__(aw.AgentWorkflow)


def call(query='q', threshold=0.1, ident='call'):
    return AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base', 'id': ident,
        'args': {'query': query, 'score_threshold': threshold}}])


def payload(chunk, text=None, status='SUCCESS'):
    citation = {'chunk_id': chunk, 'fileId': 'f', 'content': text or chunk}
    return json.dumps({'context': f'Document 1: {text or chunk}',
                       'citations': [citation], 'retrieval_status': status})


def tool(content, ident='call'):
    return ToolMessage(content=content, name='search_knowledge_base', tool_call_id=ident)


def fake_tool(monkeypatch, results):
    schema = aw.search_knowledge_base.args_schema
    invoke = AsyncMock(side_effect=results)
    monkeypatch.setattr(aw, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=invoke))
    return invoke


def test_stable_source_numbers_across_rounds_duplicates_and_failure(monkeypatch):
    fake_tool(monkeypatch, [payload('A'), payload('B'), payload('A'), RuntimeError('tool failed')])
    state = {'messages': [call()], 'kb_ids': ['allowed'], 'search_params': {}}
    for i in range(4):
        state['messages'] = [call(query=f'q{i}', ident=f'call{i}')]
        result = asyncio.run(workflow().run_tools(state, {}))
        state.update({k: v for k, v in result.items() if k != 'messages'})
        data = json.loads(result['messages'][0].content)
        if i < 3:
            number = (1, 2, 1)[i]
            assert data['citations'][0]['source_id'] == number
            assert f'Document {number}' in data['context']
    assert [c['chunk_id'] for c in state['source_registry']] == ['A', 'B']


def test_current_round_grading_aggregates_all_tools_without_old_history():
    state = {'messages': [tool(payload('old'), 'old'), call(), tool(payload('A'), 'a'), tool(payload('B'), 'b')]}
    result = asyncio.run(workflow().process_tool_output(state))
    assert [c['chunk_id'] for c in result['citations']] == ['A', 'B']
    assert 'old' not in result['rag_context']
    assert 'A' in result['rag_context'] and 'B' in result['rag_context']


@pytest.mark.parametrize('response', ['not a score', '-0.5', 'timeout code 0.5'])
def test_invalid_grade_is_unknown_and_never_schedules_low_retry(monkeypatch, response):
    monkeypatch.setattr(aw, 'invoke_provider_model', AsyncMock(return_value=SimpleNamespace(content=response)))
    obj = workflow()
    obj._get_llm_service = lambda state: SimpleNamespace(model='fake', get_chat_model=lambda **kw: object())
    result = asyncio.run(obj.grade_documents({'citations': [{'chunk_id': 'A'}],
        'rag_context': 'A', 'original_query': 'q', 'retry_count': 0}, {}))
    assert result['last_score'] is None
    assert result['grading_status'] == 'UNKNOWN'
    assert result.get('retry_count', 0) == 0
    assert not result.get('messages')


@pytest.mark.parametrize('response,expected', [
    ('.85', .85), ('**0.85**', .85), ('评分：0.85', .85), ('score:0.85', .85),
    ('1e-1', .1), ('0.5 because related', .5), ('评分约为 .85，资料高度相关。', .85),
    ('1.1', None), ('-0.5', None), ('0.85 or 0.65', None),
    ('Error code 0', None), ('HTTP 500', None), ('评分失败，重试 1 次', None),
])
def test_grade_accepts_equivalent_scores_without_turning_errors_into_scores(monkeypatch, response, expected):
    monkeypatch.setattr(aw, 'invoke_provider_model', AsyncMock(return_value=SimpleNamespace(content=response)))
    obj = workflow()
    obj._get_llm_service = lambda state: SimpleNamespace(model='fake', get_chat_model=lambda **kw: object())
    result = asyncio.run(obj.grade_documents({'citations': [{'chunk_id': 'A'}],
        'rag_context': 'A', 'original_query': 'q', 'retry_count': 0}, {}))
    assert result['last_score'] == expected
    assert result['grading_status'] == ('UNKNOWN' if expected is None else 'VALID')
    assert result.get('retry_count', 0) == (1 if expected is not None and expected < .8 else 0)


def test_locked_threshold_duplicate_does_not_execute(monkeypatch):
    invoke = fake_tool(monkeypatch, [payload('A'), payload('A')])
    state = {'messages': [call(threshold=0.5)], 'kb_ids': ['allowed'],
             'search_params': {'score_threshold': 0.5}}
    result = asyncio.run(workflow().run_tools(state, {}))
    state.update({k: v for k, v in result.items() if k != 'messages'})
    state['messages'] = [call(threshold=0.1, ident='retry')]
    result = asyncio.run(workflow().run_tools(state, {}))
    assert invoke.await_count == 1
    data = json.loads(result['messages'][0].content)
    assert data['execution_status'] == 'DUPLICATE_EFFECTIVE_REQUEST'
    assert data['actual_parameters']['score_threshold'] == 0.5
    assert data['requested_parameters']['score_threshold'] == 0.1


def test_server_stops_tool_requests_after_retry_budget(monkeypatch):
    invoke = fake_tool(monkeypatch, [payload('A')])
    state = {'messages': [call()], 'kb_ids': ['allowed'], 'retry_count': 3,
             'retrieval_attempt_count': 4}
    result = asyncio.run(workflow().run_tools(state, {}))
    invoke.assert_not_awaited()
    assert json.loads(result['messages'][0].content)['execution_status'] == 'RETRY_LIMIT'


def test_final_mapping_preserves_global_source_numbers():
    state = {'messages': [tool(payload('A'), 'a'), tool(json.dumps({
        'retrieval_status': 'SUCCESS', 'citations': [{'chunk_id': 'B', 'content': 'B', 'source_id': 2}]}), 'b')],
        'last_score': 0.9, 'source_registry': [
            {'chunk_id': 'A', 'content': 'A', 'source_id': 1},
            {'chunk_id': 'B', 'content': 'B', 'source_id': 2}]}
    response = AIMessage(content='B [2]', response_metadata={'finish_reason': 'stop'})
    citations = aw._finalize_knowledge_answer(state, response)
    assert [c['source_id'] for c in citations] == [1, 2]


def test_unknown_tool_outcome_cannot_be_replayed(monkeypatch):
    invoke = fake_tool(monkeypatch, [json.dumps({'retrieval_status': 'UNKNOWN', 'citations': []})])
    state = {'messages': [call()], 'kb_ids': ['allowed']}
    state.update(asyncio.run(workflow().run_tools(state, {})))
    state['messages'] = [call(ident='second')]
    result = asyncio.run(workflow().run_tools(state, {}))
    assert invoke.await_count == 1
    assert json.loads(result['messages'][0].content)['execution_status'] == 'DUPLICATE_EFFECTIVE_REQUEST'


def test_budget_block_finishes_without_invoking_model(monkeypatch):
    obj = workflow()
    obj._get_llm_service = lambda state: pytest.fail('blocked requests must finish locally')
    result = asyncio.run(obj.agent_node({'retrieval_blocked': True,
        'messages': [tool(payload('A'))], 'last_score': None}, {}))
    assert not result['messages'][0].tool_calls
    assert result['answer_mode'] == 'EXTRACTIVE_FALLBACK'
    assert result['validation_status'] == 'NOT_RUN'


def test_router_input_estimate_includes_actual_tool_arguments():
    from app.services.model_router import extract_features
    content = 'x' * 1200
    _, baseline = extract_features('q', [{'content': 'evidence'}], messages=[{'role': 'assistant', 'content': ''}])
    _, measured = extract_features('q', [{'content': 'evidence'}], messages=[{
        'role': 'assistant', 'content': '', 'tool_calls': [{'name': 'search_knowledge_base', 'args': {'query': content}}]}])
    assert measured['input_text_chars'] > baseline['input_text_chars'] + 1200
    assert measured['token_count']['status'] == 'ESTIMATED'


@pytest.mark.parametrize('missing', [True, False])
def test_router_role_binding_failure_does_not_call_or_fallback(monkeypatch, missing):
    from app.services import grounded_generation as gg
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'CHEAP_ONLY')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    calls = []
    def bind(role, model):
        calls.append((role, model))
        raise gg.RoutingBlocked('missing' if missing else 'invalid')
    monkeypatch.setattr(gg, '_role_service', bind)
    with pytest.raises(gg.RoutingBlocked):
        asyncio.run(gg.generate_grounded_candidate(SimpleNamespace(model='baseline'),
            [{'role': 'user', 'content': 'q'}], [{'chunk_id': 'A', 'content': 'A'}]))
    assert len(calls) == 1


def test_real_graph_multiple_tools_delivers_stable_ids_to_answer_and_grader(monkeypatch):
    from langchain_core.messages import HumanMessage
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setattr(aw, 'LLMService', lambda: SimpleNamespace(model='fake',
        get_chat_model=lambda **kw: SimpleNamespace(bind_tools=lambda tools: object())))
    obj = aw.AgentWorkflow()
    fake_tool(monkeypatch, [payload('A'), payload('B')])
    seen = []
    async def invoke(model, messages, config, stage, model_name, **kwargs):
        seen.append((stage, messages))
        if stage == 'quality':
            return AIMessage(content='0.9')
        if len([s for s, _ in seen if s == 'agent']) == 1:
            return AIMessage(content='', tool_calls=[
                {'name': 'search_knowledge_base', 'id': 'a', 'args': {'query': 'qA'}},
                {'name': 'search_knowledge_base', 'id': 'b', 'args': {'query': 'qB'}}])
        return AIMessage(content='B [2]', response_metadata={'finish_reason': 'stop'})
    monkeypatch.setattr(aw, 'invoke_provider_model', invoke)
    result = asyncio.run(obj.app.ainvoke({'messages': [HumanMessage(content='Use my documents to answer.')],
        'kb_ids': ['allowed'], 'search_params': {}, 'agent_steps': []}))
    assert result['messages'][-1].content == 'B [2]'
    assert [c['source_id'] for c in result['citations']] == [1, 2]
    assert result['grading_scope']['source_ids'] == [1, 2]
    grader = next(messages[0].content for stage, messages in seen if stage == 'quality')
    assert 'Document 1' in grader and 'Document 2' in grader
    assert result['validation_status'] == 'NOT_RUN'


@pytest.mark.parametrize('missing_usage', [False, True])
def test_agent_sse_uses_global_mapping_and_discloses_usage_scope(monkeypatch, missing_usage):
    from app.services import chat
    from app.schemas import Message, MessageRole
    registry = [{'chunk_id': 'A', 'content': 'A', 'source_id': 1},
                {'chunk_id': 'B', 'content': 'B', 'source_id': 2}]
    class App:
        async def astream_events(self, initial_state, version):
            for node, output in [
                ('tools', {'source_registry': registry}),
                ('process_tool_output', {'citations': [registry[1]], 'retrieval_status': 'SUCCESS',
                    'source_mapping_status': 'VERIFIED', 'expansion_delivery_status': 'DELIVERED'}),
                ('grading', {'grading_status': 'VALID', 'grading_scope': {'source_ids': [2]}}),
                ('agent', {'citations': registry, 'answer_mode': 'GROUNDED_GENERATION',
                    'validation_status': 'NOT_RUN', 'messages': [AIMessage(content='B [2]')],
                    'model_usage_records': [{'input': 10, 'output': 2, 'total': 12},
                                           None if missing_usage else {'input': 20, 'output': 3, 'total': 23}]})]:
                yield {'event': 'on_chain_end', 'name': node, 'metadata': {'langgraph_node': node},
                       'data': {'output': output}}
    monkeypatch.setattr(aw, 'AgentWorkflow', lambda: SimpleNamespace(app=App()))
    obj = chat.ChatService.__new__(chat.ChatService)
    async def collect():
        return [item async for item in obj._chat_stream_langgraph(
            [Message(role=MessageRole.USER, content='q')], ['allowed'], 5, 0.1, None)]
    events = asyncio.run(collect())
    assert [data['citations'] for kind, data in events if kind == 'rag_result'] == [registry]
    done = events[-1][1]
    assert done['validation_status'] == 'NOT_RUN'
    assert done['grading_scope'] == {'source_ids': [2]}
    assert done['usage_scope'] == 'AGENT_NODE_CALLS'
    assert done['usage_status'] == ('UNKNOWN' if missing_usage else 'OBSERVED')
    assert done['usage'] == ({} if missing_usage else {'input': 30, 'output': 5, 'total': 35})


def test_tool_expansion_exception_reports_verified_seed_fallback(monkeypatch):
    from app.tools import retrieval_tool as rt
    class Session:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(rt, 'Session', Session)
    monkeypatch.setattr(rt, 'get_retrieval_service', lambda: SimpleNamespace(search=AsyncMock(
        return_value=([{'id': 'A', 'file_id': 'f', 'file_name': 'book.xlsx',
                        'content': '[工作表: Budget]\nValue 42', 'score': 0.9}],
                      {'retrieval_status': 'SUCCESS'}))))
    def fail(**kwargs):
        assert kwargs['allowed_kb_ids'] == ['allowed']
        assert kwargs['include_source_members'] is True
        raise RuntimeError('sensitive expansion detail')
    monkeypatch.setattr(rt, 'expand_context', fail)
    result = json.loads(asyncio.run(rt.search_knowledge_base.coroutine(
        query='q', kb_ids=['allowed'], context_window=1)))
    assert result['retrieval_status'] == 'UNKNOWN'
    assert result['source_mapping_status'] == 'VERIFIED'
    assert result['expansion_delivery_status'] == 'SEED_ONLY'
    assert result['citations'][0]['content'] == '[工作表: Budget]\nValue 42'
    assert result['citations'][0]['location'] == '工作表: Budget'
    assert 'sensitive expansion detail' not in str(result)


def test_actual_tool_expansion_delivers_neighbor_with_own_pdf_page(db, monkeypatch):
    from app.tools import retrieval_tool as rt
    class Session:
        def __init__(self, engine): pass
        def __enter__(self): return db
        def __exit__(self, *args): pass
    monkeypatch.setattr(rt, 'Session', Session)
    sibling = {**seed_row(), 'id': 'sibling', 'content': 'SIBLING_GOLD', 'page_number': 8}
    monkeypatch.setattr(rt, 'get_retrieval_service', lambda: SimpleNamespace(search=AsyncMock(
        return_value=([seed_row(), sibling], {'retrieval_status': 'SUCCESS'}))))
    result = json.loads(asyncio.run(rt.search_knowledge_base.coroutine(query='q', kb_ids=['kb'], context_window=1)))
    citations = {c['chunk_id']: c for c in result['citations']}
    assert citations['seed']['content'] == 'SEED_PART'
    assert citations['sibling']['content'] == 'SIBLING_GOLD'
    assert citations['sibling']['location'] == '第 8 页'
    assert 'SEED_PART' in result['context'] and 'SIBLING_GOLD' in result['context']
    assert result['source_mapping_status'] == 'VERIFIED'


def test_tool_partial_rerank_failure_preserves_unknown_evidence(monkeypatch):
    from app.tools import retrieval_tool as rt
    from app.services import rerank
    class Session:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(rt, 'Session', Session)
    rows = [{'id': 'A', 'content': '甲' * 20, 'score': 0.8},
            {'id': 'B', 'content': '乙' * 20, 'score': 0.7}]
    monkeypatch.setattr(rt, 'get_retrieval_service', lambda: SimpleNamespace(search=AsyncMock(
        return_value=(rows, {'retrieval_status': 'SUCCESS'}))))
    class Rerank:
        def __init__(self, session): self.calls = 0
        def rerank(self, *args, **kwargs):
            self.calls += 1
            return [{'index': 0, 'score': 0.9}] if self.calls == 1 else []
    monkeypatch.setattr(rt, 'RerankService', Rerank)
    monkeypatch.setattr(rt, 'batch_rerank', lambda **kw: rerank.batch_rerank(**kw, max_context_tokens=5))
    result = json.loads(asyncio.run(rt.search_knowledge_base.coroutine(
        query='q', kb_ids=['kb'], use_rerank=True, rerank_top_k=2, rerank_score_threshold=0.5)))
    citations = {c['chunk_id']: c for c in result['citations']}
    assert citations['A']['rerank_score'] == 0.9
    assert citations['B']['content'] == '乙' * 20
    # 单行缺分只标记该行 UNKNOWN，不再否决整批证据。
    assert citations['B']['rerank_score'] is None and citations['B']['rerank_status'] == 'UNKNOWN'
    assert result['retrieval_status'] == 'SUCCESS'


@pytest.mark.parametrize('with_text', [False, True])
def test_actual_tool_and_registry_preserve_unverified_image_disclosure(monkeypatch, with_text):
    from app.tools import retrieval_tool as rt
    class Session:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(rt, 'Session', Session)
    rows = [{'id': 'image', 'file_id': 'f', 'file_name': 'one.pdf', 'page_number': 7,
             'content_type': 'image', 'content': 'UNVERIFIED_GENERATED_CAPTION', 'score': .8}]
    if with_text:
        rows.append({'id': 'text', 'file_id': 'f', 'content': 'Verified textual source', 'score': .9})
    monkeypatch.setattr(rt, 'get_retrieval_service', lambda: SimpleNamespace(search=AsyncMock(
        return_value=(rows, {'retrieval_status': 'SUCCESS'}))))
    actual = asyncio.run(rt.search_knowledge_base.coroutine(query='q', kb_ids=['kb']))
    assert json.loads(actual)['citations'][-1]['content_type'] == 'image'
    schema = aw.search_knowledge_base.args_schema
    monkeypatch.setattr(aw, 'search_knowledge_base', SimpleNamespace(args_schema=schema,
        ainvoke=AsyncMock(return_value=actual)))
    state = {'messages': [call()], 'kb_ids': ['kb']}
    output = asyncio.run(workflow().run_tools(state, {}))
    assert 'UNVERIFIED_GENERATED_CAPTION' not in output['messages'][0].content
    state.update(output)
    state['last_score'] = .9
    response = AIMessage(content='Answer [1]', response_metadata={'finish_reason': 'stop'})
    citations, mode = aw._finalize_knowledge_answer(state, response, include_mode=True)
    assert 'UNVERIFIED_GENERATED_CAPTION' not in response.content
    assert len(citations) == (1 if with_text else 0)
    if with_text:
        assert mode == 'GROUNDED_GENERATION' and '另有图片资料，其内容需要进一步核验' in response.content
        assert citations[0]['chunk_id'] == 'text' and citations[0]['source_id'] == 1
    else:
        assert mode == 'INSUFFICIENT_EVIDENCE' and '图片内容需要进一步核验' in response.content
