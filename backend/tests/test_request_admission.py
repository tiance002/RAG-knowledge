import asyncio
import pytest
from fastapi import HTTPException
from app.services.request_admission import AdmissionGate, AdmittedStreamingResponse


def test_gate_queue_limit_timeout_cancel_and_recovery():
    async def check():
        gate = AdmissionGate(1, 1, .05)
        first = await gate.acquire()
        waiting = asyncio.create_task(gate.acquire())
        await asyncio.sleep(.01)
        assert gate.waiters == 1
        with pytest.raises(HTTPException) as overflow:
            await gate.acquire()
        assert overflow.value.status_code == 503
        assert overflow.value.headers['Retry-After'] == '5'
        with pytest.raises(HTTPException) as timed:
            await waiting
        assert timed.value.status_code == 503
        waiting = asyncio.create_task(gate.acquire())
        await asyncio.sleep(.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert gate.waiters == 0
        first.release()
        second = await gate.acquire()
        second.release()
    asyncio.run(check())


def test_detached_work_keeps_capacity_and_response_header_failure_releases():
    async def check():
        gate = AdmissionGate(1, 0, .05)
        lease = await gate.acquire()
        unblock = asyncio.Event()
        task = asyncio.create_task(unblock.wait())
        lease.hold_until(task)
        async def body():
            yield b'x'
        cleaned = []
        response = AdmittedStreamingResponse(body(), lease=lease, on_unstarted=lambda: cleaned.append(True))
        async def send(message):
            raise RuntimeError('header failed')
        async def receive():
            await asyncio.Future()
        with pytest.raises(BaseException):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert cleaned == [True]
        with pytest.raises(HTTPException):
            await gate.acquire()
        unblock.set()
        await task
        await asyncio.sleep(0)
        next_lease = await gate.acquire()
        next_lease.release()
    asyncio.run(check())


def test_actual_turn_and_stateless_api_return_busy_before_stream_and_receipt(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.conversation_acceptance_app import create_app, close_app, KB_ID, MODEL_ID
    from app.api import conversations, chat
    async def unavailable(request):
        raise HTTPException(503, detail={'code': 'CHAT_BUSY'}, headers={'Retry-After': '5'})
    monkeypatch.setattr(conversations, 'acquire_request', unavailable)
    monkeypatch.setattr(chat, 'acquire_request', unavailable)
    app = create_app(tmp_path / 'admission.sqlite')
    try:
        with TestClient(app) as client:
            session = client.post('/api/v1/chat/sessions', json={
                'title': 'busy', 'scope_mode': 'SELECTED', 'kb_ids': [KB_ID],
                'config_overrides': {'model_id': MODEL_ID}}).json()
            response = client.post(f"/api/v1/chat/sessions/{session['id']}/turns", json={
                'question': 'example', 'client_request_id': 'busy-1', 'expected_revision': session['revision']})
            assert response.status_code == 503 and response.headers['retry-after'] == '5'
            assert response.json()['detail']['code'] == 'CHAT_BUSY'
            assert client.get(f"/api/v1/chat/sessions/{session['id']}/turns").json()['items'] == []
            response = client.post('/api/v1/chat/completions', json={
                'messages': [{'role': 'user', 'content': 'example'}], 'kb_ids': [KB_ID]})
            assert response.status_code == 503 and response.headers['retry-after'] == '5'
            assert app.state.provider_calls == []
    finally:
        close_app(app)


def test_disconnected_waiter_does_not_consume_capacity(monkeypatch):
    from app.services import request_admission as admission
    class Disconnected:
        async def is_disconnected(self):
            return True
    async def check():
        gate = AdmissionGate(1, 1, 2)
        first = await gate.acquire()
        monkeypatch.setattr(admission, 'get_gate', lambda: gate)
        with pytest.raises(HTTPException) as disconnected:
            await admission.acquire_request(Disconnected())
        assert disconnected.value.status_code == 499
        assert gate.waiters == 0
        first.release()
        after = await gate.acquire()
        after.release()
    asyncio.run(check())


def test_mid_body_send_failure_closes_inner_before_releasing_permit():
    async def check():
        gate = AdmissionGate(1, 0, .05)
        lease = await gate.acquire()
        closed = []
        async def inner():
            try:
                yield b'event'
            finally:
                closed.append('inner')
        async def outer():
            stream = inner()
            try:
                async for value in stream:
                    yield value
            finally:
                await stream.aclose()
                closed.append('outer')
        response = AdmittedStreamingResponse(outer(), lease=lease)
        async def send(message):
            if message['type'] == 'http.response.body':
                raise RuntimeError('mid-body send failure')
        async def receive():
            await asyncio.Event().wait()
        with pytest.raises(BaseException):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert closed == ['inner', 'outer']
        assert not gate.slots.locked()
    asyncio.run(check())


def test_running_generator_holds_permit_until_actual_closure():
    async def check():
        gate = AdmissionGate(1, 0, .05)
        lease = await gate.acquire()
        entered, unblock = asyncio.Event(), asyncio.Event()
        closed = []
        async def body():
            try:
                entered.set()
                await unblock.wait()
                yield b'late'
            finally:
                closed.append(True)
        iterator = body()
        owner = asyncio.create_task(anext(iterator))
        await entered.wait()
        response = AdmittedStreamingResponse(iterator, lease=lease)
        async def send(message):
            raise RuntimeError('header send failure')
        async def receive():
            await asyncio.Event().wait()
        with pytest.raises(BaseException):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert gate.slots.locked()
        unblock.set()
        assert await owner == b'late'
        for _ in range(50):
            if closed and not gate.slots.locked():
                break
            await asyncio.sleep(.02)
        assert closed and not gate.slots.locked()
    asyncio.run(check())


def test_slow_close_quarantines_permit_until_cleanup_finishes():
    async def check():
        gate = AdmissionGate(1, 0, .05)
        lease = await gate.acquire()
        closing, unblock = asyncio.Event(), asyncio.Event()
        async def body():
            try:
                yield b'first'
            finally:
                closing.set()
                await unblock.wait()
        async def send(message):
            if message['type'] == 'http.response.body':
                raise RuntimeError('broken body')
        async def receive():
            await asyncio.Event().wait()
        response = AdmittedStreamingResponse(body(), lease=lease)
        with pytest.raises(BaseException):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert closing.is_set() and gate.slots.locked()
        unblock.set()
        for _ in range(50):
            if not gate.slots.locked():
                break
            await asyncio.sleep(.02)
        assert not gate.slots.locked()
    asyncio.run(check())
