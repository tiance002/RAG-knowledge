import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import llm as llm_module


def test_only_nonstreamed_answer_uses_configured_limit(monkeypatch):
    monkeypatch.setattr(llm_module.settings, 'LLM_ANSWER_MAX_TOKENS', 4096)
    service = llm_module.LLMService(base_url='http://example.test/v1', api_key='unused', model='test')
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))]))
    monkeypatch.setattr(service, '_get_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    assert asyncio.run(service.generate([{'role': 'user', 'content': 'hi'}])) == 'ok'
    assert create.call_args.kwargs['max_tokens'] == 4096

    class Stream:
        def __aiter__(self):
            async def values():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='ok'), finish_reason='stop')])
            return values()

    create.return_value = Stream()
    async def collect():
        return [x async for x in service.generate_stream([{'role': 'user', 'content': 'hi'}])]
    assert asyncio.run(collect()) == ['ok']
    assert 'max_tokens' not in create.call_args.kwargs


def test_agent_chat_model_default_and_explicit_override(monkeypatch):
    monkeypatch.setattr(llm_module.settings, 'LLM_ANSWER_MAX_TOKENS', 4096)
    monkeypatch.setattr(llm_module, 'ChatOpenAI', lambda **kwargs: kwargs)
    service = llm_module.LLMService(base_url='http://example.test/v1', api_key='unused', model='test')
    assert service.get_chat_model()['max_tokens'] is None
    assert service.get_chat_model(max_tokens=512)['max_tokens'] == 512
    # Removing the accidental cap must not restore automatic UNKNOWN retries.
    assert service.get_chat_model()['max_retries'] == 0
