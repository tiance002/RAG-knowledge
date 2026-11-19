import asyncio
import httpx
import pytest

from app.services import llm


def unavailable(status):
    request = httpx.Request('POST', 'https://model.invalid/chat')
    return httpx.HTTPStatusError('provider refused request', request=request,
                                 response=httpx.Response(status, request=request))


@pytest.mark.parametrize('errors, count', [([503, 503, 200], 3), ([503, 503, 503], 3), ([400], 1)])
def test_answer_retries_only_transient_errors(monkeypatch, errors, count):
    monkeypatch.setattr(llm, 'ANSWER_RETRY_DELAYS', (0, 0))
    calls = []

    async def send():
        status = errors[len(calls)]
        calls.append(status)
        if status != 200:
            raise unavailable(status)
        return 'answer'

    if errors[-1] == 200:
        assert asyncio.run(llm.retry_answer_call(send, stage='agent')) == 'answer'
    else:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(llm.retry_answer_call(send, stage='agent'))
    assert len(calls) == count


@pytest.mark.parametrize('partial', [False, True])
def test_stream_only_restarts_before_first_token(monkeypatch, partial):
    monkeypatch.setattr(llm, 'ANSWER_RETRY_DELAYS', (0, 0))
    calls = 0

    @llm._retry_answer_stream
    async def stream():
        nonlocal calls
        calls += 1
        if partial:
            yield 'partial'
        if calls == 1:
            raise httpx.ReadTimeout('provider stalled')
        yield 'answer'

    async def collect(values):
        return [value async for value in values]

    if partial:
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(collect(stream()))
        assert calls == 1
    else:
        assert asyncio.run(collect(stream())) == ['answer']
        assert calls == 2
