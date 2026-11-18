"""Durable turn must not allocate model clients for a disconnected request."""
import asyncio
from types import SimpleNamespace

import pytest


def test_disconnect_before_first_model_step_allocates_no_chat_service(monkeypatch):
    from app.services import chat, conversation_context
    from app.services.conversation_runner import run_turn

    created = []
    monkeypatch.setattr(chat, 'ChatService', lambda: created.append(True))
    monkeypatch.setattr(conversation_context, 'prepare_history',
                        lambda *args, **kwargs: ([], [], {}))

    class Store:
        engine = object()

        def read_turn_control(self, *args):
            return {'cancel_requested': False, 'status': 'RUNNING'}

        def finish_turn(self, *args, **kwargs):
            return {'status': args[3]}

        def get_session(self, *args):
            return {'revision': 2}

    class Disconnected:
        async def is_disconnected(self):
            return True

    reservation = {
        'turn': {'id': 'turn', 'sequence_no': 1, 'user_content': 'question'},
        'session': {'id': 'session', 'revision': 1},
        'created': True,
        'lease_token': 'lease',
    }
    async def exercise():
        stream = run_turn(Store(), reservation, kb_ids=['kb'], params={},
                          runtime_snapshot=SimpleNamespace(max_chat_history_rounds=5),
                          request=Disconnected())
        assert (await anext(stream))[0] == 'turn_started'
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)
        assert created == []

    asyncio.run(exercise())


def test_construction_failure_still_closes_service_if_model_close_is_cancelled(monkeypatch):
    from app.services import chat, conversation_context, llm
    from app.services.conversation_runner import run_turn

    closed = []

    class FakeService:
        def chat_stream(self, **kwargs):
            raise ValueError('synthetic stream construction failure')

        async def aclose(self):
            closed.append('service')

    class FakeModel:
        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            closed.append('model')

    async def close_with_cancellation(close):
        await close()
        if getattr(close, '__self__', None).__class__ is FakeModel:
            raise asyncio.CancelledError()

    monkeypatch.setattr(chat, 'ChatService', FakeService)
    monkeypatch.setattr(llm, 'LLMService', FakeModel)
    monkeypatch.setattr(llm, 'close_quietly', close_with_cancellation)
    monkeypatch.setattr(conversation_context, 'prepare_history',
                        lambda *args, **kwargs: ([], [], {}))

    class Store:
        engine = object()

        def read_turn_control(self, *args):
            return {'cancel_requested': False, 'status': 'RUNNING'}

        def finish_turn(self, *args, **kwargs):
            return {'status': args[3]}

        def get_session(self, *args):
            return {'revision': 2}

    reservation = {'turn': {'id': 'turn', 'sequence_no': 1, 'user_content': 'question'},
                   'session': {'id': 'session', 'revision': 1}, 'created': True,
                   'lease_token': 'lease'}
    binding = SimpleNamespace(base_url='http://synthetic.invalid', api_key='synthetic', model_name='mock')

    async def exercise():
        stream = run_turn(Store(), reservation, kb_ids=['kb'], params={},
                          runtime_snapshot=SimpleNamespace(max_chat_history_rounds=5),
                          model_binding=binding)
        assert (await anext(stream))[0] == 'turn_started'
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)

    asyncio.run(exercise())
    assert closed == ['model', 'service']
