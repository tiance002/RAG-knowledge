import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.services import observability as obs
from app.services.performance_timing import timed_stage
from app.services.trace_schema import capture_identity


def test_timing_preserves_return_and_exception(monkeypatch):
    rows = []
    monkeypatch.setattr(obs, 'event', lambda name, **data: rows.append((name, data)))
    result = object()
    assert timed_stage('chroma')(lambda: result)() is result
    problem = ValueError('original')
    def fail():
        raise problem
    with pytest.raises(ValueError) as caught:
        timed_stage('chroma')(fail)()
    assert caught.value is problem
    assert [r[1]['execution_status'] for r in rows] == ['RETURNED', 'FAILED']
    assert all(r[1]['duration_ms'] >= 0 for r in rows)


def test_flush_latency_is_measured_not_hidden(monkeypatch):
    rows = []
    monkeypatch.setattr(obs, 'event', lambda name, **data: rows.append((name, data)))
    monkeypatch.setattr(obs, '_client', SimpleNamespace(flush=lambda: time.sleep(.01)))
    obs.flush_observations()
    assert rows[0][1]['stage_name'] == 'observability_flush'
    assert rows[0][1]['duration_ms'] >= 9
    monkeypatch.setattr(obs, '_client', None)
    obs.flush_observations()
    assert rows[-1][1]['execution_status'] == 'NOT_RUN'


@pytest.mark.parametrize('opt_in', [False, True])
def test_http_identity_is_opt_in_and_does_not_authorize_content(monkeypatch, tmp_path, opt_in):
    from app.api import chat
    from app.schemas import ChatRequest
    from app.services.trace_schema import capture_settings
    log = tmp_path / 'events.jsonl'
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    monkeypatch.setenv('RAG_PERFORMANCE_IDENTITY_HEADERS', str(opt_in).lower())
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    monkeypatch.setattr(obs, '_client', None)
    seen = []
    class Service:
        @obs.observe('qa.request')
        async def chat_stream(self, **kwargs):
            seen.append((capture_identity(), capture_settings()))
            yield 'answer_chunk', {'content': 'synthetic answer'}
            yield 'done', {'generation_status': 'COMPLETED'}
    monkeypatch.setattr(chat, 'ChatService', Service)
    request = Request({'type': 'http', 'headers': [(b'x-rag-run-id', b'run-test'),
                       (b'x-rag-attempt-id', b'attempt-test'), (b'x-rag-qid', b'q1')]})
    async def run():
        response = await chat.chat_completions(ChatRequest(messages=[{'role':'user','content':'q'}], kb_ids=[]),
                                               session=None, memory_scope=None, request=request)
        return ''.join([part async for part in response.body_iterator])
    output = asyncio.run(run())
    assert 'COMPLETED' in output
    assert seen[0][0]['run_id'] == ('run-test' if opt_in else None)
    assert seen[0][1]['capture_mode'] == 'BASIC'
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    perf = next(row for row in rows if row.get('stage') == 'qa.performance')
    assert perf['metadata']['server_pipeline_ms'] >= 0
    assert perf['metadata']['post_terminal_cleanup_ms'] >= 0
    assert 'excluding_outer_span_close' in perf['metadata']['timing_scope']
    assert capture_identity()['run_id'] is None


def test_cross_task_disconnect_closes_inner_and_identity(monkeypatch):
    from app.api import chat
    from app.schemas import ChatRequest
    monkeypatch.setenv('RAG_PERFORMANCE_IDENTITY_HEADERS', 'true')
    closed = []
    class Service:
        async def chat_stream(self, **kwargs):
            try:
                yield 'answer_chunk', {'content': 'partial'}
                yield 'done', {'generation_status': 'COMPLETED'}
            finally:
                closed.append(capture_identity()['run_id'])
    monkeypatch.setattr(chat, 'ChatService', Service)
    request = Request({'type': 'http', 'headers': [(b'x-rag-run-id', b'cancel-run')]})
    async def run():
        response = await chat.chat_completions(ChatRequest(messages=[{'role':'user','content':'q'}], kb_ids=[]),
                                               session=None, memory_scope=None, request=request)
        await anext(response.body_iterator)
        assert capture_identity()['run_id'] is None
        await asyncio.create_task(response.body_iterator.aclose())
        assert capture_identity()['run_id'] is None
    asyncio.run(run())
    assert closed == ['cancel-run']
