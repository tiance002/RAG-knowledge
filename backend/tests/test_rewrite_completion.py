import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.llm import LLMService, is_unknown_provider_error, should_abort_provider_error


@pytest.mark.parametrize('reason,text,expected', [
    ('stop', ' refined question ', 'refined question'),
    ('stop', '', ''), ('length', 'partial altered question', ''),
])
def test_only_complete_nonempty_rewrite_is_used(reason, text, expected):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
        return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,
            message=SimpleNamespace(content=text))], usage=None)))))
    service = LLMService(api_key='test', model='test')
    service._client = client
    assert asyncio.run(service.rewrite_query('original question')) == expected
    assert client.chat.completions.create.await_count == 1


@pytest.mark.parametrize('reason', [None, 'unrecognized'])
def test_unknown_rewrite_finish_is_terminal_without_retry(reason):
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=reason, message=SimpleNamespace(content='partial'))], usage=None))
    service = LLMService(api_key='test', model='test')
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(Exception) as caught:
        asyncio.run(service.rewrite_query('original question'))
    assert is_unknown_provider_error(caught.value)
    assert should_abort_provider_error(caught.value)
    assert create.await_count == 1
