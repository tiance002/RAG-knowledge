"""Offline, scripted provider and retrieval responses through the actual Agent graph."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.schemas import Message, MessageRole
from app.services import agent_workflow, grounded_generation
from test_memory_chat import setup_service, snapshot


@pytest.mark.parametrize('ordering', ['success', 'success_then_empty', 'empty_then_success'])
def test_agent_graph_preference_retry_and_source_mapping(monkeypatch, tmp_path, ordering):
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    saved = snapshot(answer_style='concise')
    service, store, _ = setup_service(monkeypatch, saved)
    llm = SimpleNamespace(model='offline', get_chat_model=lambda **kwargs: object())
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_get_llm_service', lambda *args: llm)
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_route_post_retrieval_model',
                        lambda self, state, llm, messages, **kwargs: (llm, None, None))
    monkeypatch.setattr(grounded_generation, '_routed_chat_model', lambda *args: object())
    schema = agent_workflow.search_knowledge_base.args_schema
    sources = {
        'A': {'chunk_id': 'A', 'fileId': 'file-A', 'fileName': 'A', 'content': 'Evidence A'},
        'B': {'chunk_id': 'B', 'fileId': 'file-B', 'fileName': 'B', 'content': 'Evidence B'},
    }
    runs = []
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
            if counters['agent'] <= 2:
                round_number = counters['agent']
                tool_calls = [{'name': 'search_knowledge_base', 'id': f'{round_number}-1',
                               'args': {'query': f'query {round_number}'}}]
                if round_number == 2 and ordering != 'success':
                    empty_call = {'name': 'search_knowledge_base', 'id': '2-2',
                                  'args': {'query': 'empty query'}}
                    if ordering == 'empty_then_success':
                        tool_calls.insert(0, empty_call)
                    else:
                        tool_calls.append(empty_call)
                return AIMessage(content='', tool_calls=tool_calls)
            return AIMessage(content='Evidence B [2], then A [1].',
                             response_metadata={'finish_reason': 'stop'})

        async def search(args, **kwargs):
            tool_inputs.append(dict(args))
            query = args['query']
            ordered = {'query 1': ['A', 'B'], 'query 2': ['B', 'A', 'B'], 'empty query': []}[query]
            return json.dumps({'retrieval_status': 'SUCCESS' if ordered else 'NO_RESULTS',
                               'context': 'ignored', 'citations': [sources[key] for key in ordered]})

        monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
        monkeypatch.setattr(agent_workflow, 'search_knowledge_base',
                            SimpleNamespace(args_schema=schema, ainvoke=search))

        async def request():
            return [event async for event in service.chat_stream(
                [Message(role=MessageRole.USER, content='根据知识库解释Redis实现细节')],
                kb_ids=['allowed'], mode='agent')]

        events = asyncio.run(request())
        assert events[-1][0] == 'done' and not any(kind == 'error' for kind, _ in events)
        rag = [data['citations'] for kind, data in events if kind == 'rag_result']
        assert rag
        expected = [(1, 'file-A', 'A', 'Evidence A'), (2, 'file-B', 'B', 'Evidence B')]
        def mapping(citations):
            return [(c['source_id'], c['fileId'], c['chunk_id'], c['content']) for c in citations]
        assert all(mapping(citations) == expected for citations in rag)
        agent_calls = [rows for stage, rows in calls if stage == 'agent']
        for rows in agent_calls:
            instruction = next(row['content'] for row in rows if row['type'] == 'system')
            assert '每个依据资料作出的事实陈述旁' in instruction
            assert 'source_id' in instruction and '`[1]`' in instruction
            assert '不要补充证据未明确支持的细节、单位、时间或推断' in instruction
            assert '不要据此断言整份文档没有该信息' in instruction
        delivered = [json.loads(row['content']) for rows in agent_calls for row in rows
                     if row['type'] == 'tool' and row.get('name') == 'search_knowledge_base']
        assert any([c['source_id'] for c in item['citations']] == [2, 1] for item in delivered)
        assert any('Document 2' in item['context'] and 'Evidence B' in item['context']
                   and 'Document 1' in item['context'] and 'Evidence A' in item['context']
                   for item in delivered)
        answer = ''.join(data['content'] for kind, data in events if kind == 'answer_chunk')
        # 早前一轮检索为空不应否决后续成功轮次的证据：三种顺序都应生成答案。
        assert events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
        assert answer == 'Evidence B [2], then A [1].'
        runs.append((calls, tool_inputs, rag))

    assert store.get_preferences.await_count == 2
    assert runs[0][1:] == runs[1][1:]
    for stage in ('intent', 'quality'):
        assert [rows for name, rows in runs[0][0] if name == stage] == [
            rows for name, rows in runs[1][0] if name == stage]
    preferred = [rows for stage, rows in runs[0][0] if stage == 'agent']
    assert [any(row['type'] == 'system' and '回答表达偏好' in row['content'] for row in rows)
            for rows in preferred] == [False, False, True]
