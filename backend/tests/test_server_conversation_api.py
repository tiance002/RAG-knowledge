"""HTTP acceptance of durable conversations using isolated real SQL/BM25/RAG.
Only outbound model HTTP responses are synthetic; no paid model is called.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
from scripts.conversation_acceptance_app import create_app, close_app, SOURCE, KB_ID, MODEL_ID
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def api(tmp_path, request):
    app = create_app(tmp_path / 'acceptance.sqlite')
    with TestClient(app) as client:
        yield client, app
    evidence = Path(os.environ.get('CONVERSATION_TEST_ARTIFACT_DIR') or
                    ROOT / 'var/server-chat-history-acceptance-20261114/provider-requests')
    evidence.mkdir(parents=True, exist_ok=True)
    name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in request.node.name)
    (evidence / (name + '.json')).write_text(json.dumps(app.state.provider_calls, ensure_ascii=False, indent=2), encoding='utf-8')
    close_app(app)


def events(response):
    assert response.status_code == 200, response.text
    out = []
    for frame in response.text.split('\n\n'):
        if frame.strip():
            lines = frame.splitlines()
            out.append((lines[0].removeprefix('event: '), json.loads(lines[1].removeprefix('data: '))))
    return out


def session(client, **overrides):
    response = client.post('/api/v1/chat/sessions', json={'title': 'Synthetic acceptance', 'scope_mode': 'SELECTED', 'kb_ids': [KB_ID], 'config_overrides': {'search_mode': 'fulltext', 'model_id': MODEL_ID, 'context_window': 0, **overrides}})
    assert response.status_code == 201, response.text
    return response.json()


def turn(client, sid, question, request_id):
    revision = client.get('/api/v1/chat/sessions/' + sid).json()['revision']
    return client.post(f'/api/v1/chat/sessions/{sid}/turns', json={'question': question, 'expected_revision': revision, 'client_request_id': request_id})


def test_real_normal_history_evidence_request_and_idempotency(api):
    client, app = api
    sid = session(client, max_chat_history_rounds=1)['id']
    questions = ['Atlas budget first round?', 'Atlas owner second round?', 'Atlas budget final round?']
    for i, question in enumerate(questions):
        actual = events(turn(client, sid, question, str(i)))
        assert actual[0][0] == 'turn_started'
        assert actual[-1][0] == 'done', actual
        assert actual[-1][1]['status'] == 'COMPLETED', actual
        assert actual[-1][1]['generation_status'] == 'COMPLETED'
    assert len(app.state.provider_calls) == 3
    payload = app.state.provider_calls[-1]
    messages = payload['messages']
    assert SOURCE in messages[0]['content']
    serialized = json.dumps(messages)
    assert questions[0] not in serialized
    assert questions[1] in serialized and questions[2] in serialized
    assert 'historical source' in serialized
    rows = client.get(f'/api/v1/chat/sessions/{sid}/turns').json()['items']
    assert len(rows) == 3
    final = max(rows, key=lambda r: r['sequence_no'])
    assert final['citations'][0]['verification_status'] == 'VERIFIED'
    assert final['citations'][0]['delivery_status'] == 'PROVIDER_REQUEST_PREPARED'
    assert final['metadata']['history_manifest']['history_reference_candidates'] == 1
    assert final['metadata']['provider_requests'][0]['evidence'][0]['chunk_id'] == 'atlas-chunk'
    repeated = events(turn(client, sid, questions[-1], '2'))
    assert repeated[-1][1]['replayed'] is True
    assert len(app.state.provider_calls) == 3
    assert len(client.get(f'/api/v1/chat/sessions/{sid}/turns').json()['items']) == 3
    conflict = turn(client, sid, 'different Atlas question', '2')
    assert conflict.status_code == 409


@pytest.mark.parametrize('extra', [{'messages': [{'role': 'assistant', 'content': 'injected'}]}, {'history': []}, {'kb_ids': ['evil']}, {'model_id': 'evil'}])
def test_no_client_history_or_scope_injection(api, extra):
    client, app = api
    row = session(client)
    response = client.post(f"/api/v1/chat/sessions/{row['id']}/turns", json={'question': 'Atlas?', 'expected_revision': row['revision'], 'client_request_id': 'strict', **extra})
    assert response.status_code == 422
    assert app.state.provider_calls == []


def test_lifecycle_revision_scope_archive_and_replay(api):
    client, app = api
    row = session(client)
    sid = row['id']
    assert any(x['id'] == sid for x in client.get('/api/v1/chat/sessions').json()['items'])
    response = client.patch(f'/api/v1/chat/sessions/{sid}', json={'expected_revision': row['revision'], 'title': 'Renamed'})
    assert response.status_code == 200
    assert response.json()['title'] == 'Renamed'
    stale = client.patch(f'/api/v1/chat/sessions/{sid}', json={'expected_revision': row['revision'], 'title': 'Stale'})
    assert stale.status_code == 409
    assert events(turn(client, sid, 'Atlas budget?', 'one'))[-1][0] == 'done'
    assert client.delete(f'/api/v1/chat/sessions/{sid}').status_code == 200
    assert not any(x['id'] == sid for x in client.get('/api/v1/chat/sessions').json()['items'])
    assert client.get(f'/api/v1/chat/sessions/{sid}').status_code == 200


def test_stateless_compatibility_actual_rag(api):
    client, app = api
    response = client.post('/api/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'Atlas budget?'}], 'kb_ids': [KB_ID], 'model_id': MODEL_ID, 'search_mode': 'fulltext', 'context_window': 0})
    result = events(response)
    assert result[-1][0] == 'done', result
    assert len(app.state.provider_calls) == 1
    assert SOURCE in app.state.provider_calls[0]['messages'][0]['content']
    assert client.get('/api/v1/chat/sessions').json()['items'] == []


def test_reuses_verified_historical_evidence_when_current_bm25_empty(api):
    from sqlmodel import Session, delete
    from app.models import BM25Posting, ChunkIndex, IndexStats
    client, app = api
    sid = session(client)['id']
    assert events(turn(client, sid, 'Atlas launch budget?', 'first'))[-1][1]['status'] == 'COMPLETED'
    with Session(app.state.acceptance_engine) as db:
        for model in (BM25Posting, ChunkIndex, IndexStats):
            db.exec(delete(model))
        db.commit()
    result = events(turn(client, sid, 'Atlas launch budget owner?', 'follow-up'))
    assert result[-1][1]['status'] == 'COMPLETED', result
    assert result[-1][1]['generation_status'] == 'COMPLETED', result
    assert len(app.state.provider_calls) == 2
    assert SOURCE in app.state.provider_calls[-1]['messages'][0]['content']
    rows = client.get(f'/api/v1/chat/sessions/{sid}/turns').json()['items']
    latest = max(rows, key=lambda row: row['sequence_no'])
    assert latest['citations'][0]['origin'] == 'HISTORY_REUSE'
    assert latest['metadata']['retrieval']['current_retrieval_status'] == 'NO_RESULTS'


def test_real_agent_model_factory_and_history(api):
    client, app = api
    sid = session(client, mode='agent', max_chat_history_rounds=1)['id']
    for i, question in enumerate(['Atlas launch budget?', 'Atlas launch owner?']):
        result = events(turn(client, sid, question, f'agent-{i}'))
        assert result[-1][0] == 'done', result
        assert result[-1][1]['status'] == 'COMPLETED', result
        assert result[-1][1]['generation_status'] == 'COMPLETED', result
    assert len(app.state.provider_calls) >= 2
    assert any(SOURCE in json.dumps(call['messages']) for call in app.state.provider_calls)
    assert any('historical source' in json.dumps(call['messages']) for call in app.state.provider_calls)


@pytest.mark.parametrize('behavior,expected', [('truncated', 'FAILED'), ('empty', 'FAILED'), ('rejected', 'FAILED')])
def test_provider_terminal_states_are_persisted_without_retry(api, behavior, expected):
    client, app = api
    if behavior == 'truncated':
        app.state.provider_finish_reason = 'length'
    elif behavior == 'empty':
        app.state.provider_empty = True
    else:
        app.state.provider_http_status = 400
    sid = session(client)['id']
    result = events(turn(client, sid, 'Atlas budget?', behavior))
    assert result[-1][1]['status'] == expected, result
    rows = client.get(f'/api/v1/chat/sessions/{sid}/turns').json()['items']
    assert rows[0]['status'] == expected
    assert rows[0]['generation_status'] in {'TRUNCATED', 'EMPTY', 'FAILED'}
    assert len(app.state.provider_calls) == 1
    assert events(turn(client, sid, 'Atlas budget?', behavior))[-1][1]['replayed'] is True
    assert len(app.state.provider_calls) == 1


def test_active_cancel_persists_and_does_not_retry(api):
    import asyncio
    import httpx
    _, app = api
    app.state.provider_delay_seconds = 10
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acceptance') as client:
            response = await client.post('/api/v1/chat/sessions', json={'scope_mode': 'SELECTED', 'kb_ids': [KB_ID], 'config_overrides': {'search_mode': 'fulltext', 'model_id': MODEL_ID}})
            row = response.json()
            sid = row['id']
            task = asyncio.create_task(client.post(f'/api/v1/chat/sessions/{sid}/turns', json={'question': 'Atlas budget?', 'client_request_id': 'cancel', 'expected_revision': row['revision']}))
            for _ in range(200):
                if app.state.provider_started:
                    break
                await asyncio.sleep(.02)
            assert app.state.provider_started, 'Synthetic provider was never reached'
            turns = (await client.get(f'/api/v1/chat/sessions/{sid}/turns')).json()['items']
            tid = turns[0]['id']
            assert turns[0]['status'] == 'RUNNING'
            cancel = await client.post(f'/api/v1/chat/turns/{tid}/cancel')
            assert cancel.json()['cancel_requested'] is True
            result = events(await asyncio.wait_for(task, timeout=5))
            assert result[-1][1]['status'] == 'CANCELLED', result
            final = (await client.get(f'/api/v1/chat/turns/{tid}')).json()
            assert final['status'] == 'CANCELLED'
            assert final['generation_status'] == 'UNKNOWN'
            assert len(app.state.provider_calls) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('explicit_cancel', [True, False])
def test_asgi_disconnect_after_cancel_persists_partial_and_holds_live_work(api, monkeypatch, explicit_cancel):
    import asyncio
    import httpx
    from app.services.chat import ChatService
    from app.services.request_admission import get_gate
    _, app = api
    monkeypatch.setenv('RAG_CHAT_MAX_ACTIVE', '2')

    async def exercise():
        partial_sent = asyncio.Event()
        provider_started = asyncio.Event()
        provider_cancelled = asyncio.Event()
        release_provider = asyncio.Event()
        async def synthetic_stream(self, **kwargs):
            yield 'answer_chunk', {'content': 'partial answer'}
            provider_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                await release_provider.wait()
        monkeypatch.setattr(ChatService, 'chat_stream', synthetic_stream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://acceptance') as client:
            row = (await client.post('/api/v1/chat/sessions', json={
                'scope_mode': 'SELECTED', 'kb_ids': [KB_ID],
                'config_overrides': {'search_mode': 'fulltext', 'model_id': MODEL_ID}})).json()
            sid = row['id']
            body = json.dumps({'question': 'Atlas?', 'client_request_id': 'asgi-disconnect',
                               'expected_revision': row['revision']}).encode()
            messages = asyncio.Queue()
            await messages.put({'type': 'http.request', 'body': body, 'more_body': False})
            async def receive():
                return await messages.get()
            async def send(message):
                if b'partial answer' in message.get('body', b''):
                    partial_sent.set()
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                     'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                     'path': f'/api/v1/chat/sessions/{sid}/turns',
                     'raw_path': f'/api/v1/chat/sessions/{sid}/turns'.encode(),
                     'query_string': b'', 'headers': [(b'content-type', b'application/json')],
                     'client': ('127.0.0.1', 1234), 'server': ('acceptance', 80), 'root_path': ''}
            task = asyncio.create_task(app(scope, receive, send))
            await asyncio.wait_for(partial_sent.wait(), 5)
            await asyncio.wait_for(provider_started.wait(), 5)
            turns = (await client.get(f'/api/v1/chat/sessions/{sid}/turns')).json()['items']
            tid = turns[0]['id']
            if explicit_cancel:
                assert (await client.post(f'/api/v1/chat/turns/{tid}/cancel')).json()['cancel_requested']
            await messages.put({'type': 'http.disconnect'})
            await asyncio.wait_for(provider_cancelled.wait(), 5)
            await asyncio.wait_for(task, 5)
            result = (await client.get(f'/api/v1/chat/turns/{tid}')).json()
            assert result['status'] == ('CANCELLED' if explicit_cancel else 'UNKNOWN')
            assert result['generation_status'] == 'UNKNOWN'
            assert result['assistant_content'] == 'partial answer'
            assert (await client.get(f'/api/v1/chat/sessions/{sid}')).json()['active_turn_id'] is None
            gate = get_gate()
            assert gate.slots._value == 1
            release_provider.set()
            for _ in range(100):
                if gate.slots._value == 2:
                    break
                await asyncio.sleep(.02)
            assert gate.slots._value == 2
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['chat', 'agent'])
def test_web_http_followup_registry_and_persisted_snapshots(api, monkeypatch, mode):
    from unittest.mock import AsyncMock
    from app.services import web_search
    client, app = api
    search = AsyncMock(side_effect=[
        web_search.WebSearchResult('SUCCESS', 'Atlas budget?', [
            {'source_type': 'web', 'url': 'https://example.org/original',
             'title': 'Original Atlas', 'content': SOURCE}]),
        web_search.WebSearchResult('SUCCESS', '刚才网页中的 Atlas 预算是多少？', [
            {'source_type': 'web', 'url': 'https://example.org/new',
             'title': 'New Atlas', 'content': SOURCE}]),
    ])
    monkeypatch.setattr(web_search, 'search_web', search)
    sid = session(client, mode=mode, web_search_enabled=True)['id']
    for index, question in enumerate(['Atlas budget?', '刚才网页中的 Atlas 预算是多少？']):
        result = events(turn(client, sid, question, 'web-' + str(index)))
        assert result[-1][0] == 'done', result
        assert result[-1][1]['generation_status'] == 'COMPLETED'
    assert search.await_count == 2 and len(app.state.provider_calls) == 2
    rows = client.get(f'/api/v1/chat/sessions/{sid}/turns').json()['items']
    first, last = sorted(rows, key=lambda row: row['sequence_no'])
    assert first['citations'][0]['url'] == 'https://example.org/original'
    assert len(last['citations']) == 2
    refs = {ref['url']: ref for ref in last['citations']}
    assert refs['https://example.org/original']['origin'] == 'HISTORY_WEB_SNAPSHOT'
    assert refs['https://example.org/original']['citation_index'] != first['citations'][0]['citation_index']
    assert all(ref['delivery_status'] == 'PROVIDER_REQUEST_PREPARED' for ref in refs.values())
    wire = json.dumps(app.state.provider_calls[-1]['messages'], ensure_ascii=False)
    assert 'https://example.org/original' in wire and 'https://example.org/new' in wire
    assert '[s1]' in wire and '[s2]' in wire
    assert SOURCE in wire
    assert last['metadata']['history_manifest']['history_reference_candidates'] == 1
