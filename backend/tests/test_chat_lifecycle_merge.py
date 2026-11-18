import asyncio
from types import SimpleNamespace

from app.services import chat


def test_stream_closes_owned_models_and_inner_stream_on_disconnect(monkeypatch):
    closed = []
    class Model:
        async def aclose(self):
            closed.append('model')
    model = Model()
    service = chat.ChatService.__new__(chat.ChatService)
    service._owned_llms = []

    async def impl(*args, **kwargs):
        assert chat._owned_llm_services.get() is service._owned_llms
        try:
            yield ('answer_chunk', {'content': 'partial'})
            yield ('done', {})
        finally:
            closed.append('stream')

    monkeypatch.setattr(service, '_chat_stream_impl', impl)
    async def run():
        stream = service.chat_stream(resolved_llm=model)
        assert (await anext(stream))[0] == 'answer_chunk'
        assert chat._owned_llm_services.get() is None
        await stream.aclose()
        await service.aclose()
    asyncio.run(run())
    assert closed == ['stream', 'model']


def test_stream_closes_on_task_cancellation(monkeypatch):
    closed = []
    started = asyncio.Event()
    class Model:
        async def aclose(self): closed.append('model')
    service = chat.ChatService.__new__(chat.ChatService)
    service._owned_llms = []
    async def impl(*args, **kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield None
        finally:
            closed.append('stream')
    monkeypatch.setattr(service, '_chat_stream_impl', impl)
    async def run():
        stream = service.chat_stream(resolved_llm=Model())
        task = asyncio.create_task(anext(stream))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('cancellation did not propagate')
    asyncio.run(run())
    assert closed == ['stream', 'model']


def test_rewrite_closes_model_and_releases_admission_on_error(monkeypatch):
    from app.api import chat as api
    from app.services import model_bindings, runtime_settings
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    closed = []
    class Lease:
        def release(self): closed.append('lease')
    class Model:
        def __init__(self, **kwargs): pass
        async def understand_query(self, *args, **kwargs):
            raise ValueError('synthetic failure')
        async def aclose(self): closed.append('model')
    monkeypatch.setattr(api, 'acquire_request', AsyncMock(return_value=Lease()))
    monkeypatch.setattr(api, 'LLMService', Model)
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda session: SimpleNamespace(default_llm_model='id', max_chat_history_rounds=2))
    monkeypatch.setattr(model_bindings, 'resolve_model', lambda *args, **kwargs: SimpleNamespace(base_url='test', api_key='test', model_name='test'))
    data = SimpleNamespace(model_id=None, query='question', history=[], images=[])
    async def run():
        try:
            await api.rewrite_query(data, request=object(), session=object())
        except HTTPException as exc:
            assert exc.status_code == 502
        else:
            raise AssertionError('expected provider failure')
    asyncio.run(run())
    assert closed == ['model', 'lease']


def test_repeated_cancellation_waits_for_all_models_to_close():
    closed = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Model:
            def __init__(self, name): self.name = name
            async def aclose(self):
                if self.name == 'slow':
                    started.set()
                    await release.wait()
                closed.append(self.name)
        service = chat.ChatService.__new__(chat.ChatService)
        service._owned_llms = [Model('first'), Model('slow')]
        task = asyncio.create_task(service.aclose())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('cancellation did not propagate')
        await service.aclose()
    asyncio.run(run())
    assert closed == ['slow', 'first']
