"""Provider-reported numeric usage only; no real Provider traffic."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import requests
from app.services import llm, embedding, rerank, grounded_generation


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_PROMETHEUS_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e11')


def events_capture(monkeypatch):
    events=[]
    monkeypatch.setattr(llm, 'event', lambda name, **data: events.append((name,data)))
    return events


def test_actual_embedding_async_response_retained_on_same_attempt(monkeypatch):
    events=events_capture(monkeypatch)
    payload={'data':[{'index':0,'embedding':[.2]}], 'usage':{'prompt_tokens':7,'total_tokens':7}}
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=payload)))
    @asynccontextmanager
    async def owned(**kwargs): yield client
    monkeypatch.setattr(embedding,'provider_request',owned)
    service=embedding.EmbeddingService(base_url='https://fixture.invalid',api_key='private-key',model='synthetic')
    async def run():
        try: return await service.embed_query('private input')
        finally: await client.aclose()
    assert asyncio.run(run()) == [.2]
    start=next(data for name,data in events if name=='provider.request_started')
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert start['local_request_id']==finish['local_request_id']
    assert finish['usage_status']=='PROVIDER_REPORTED'
    assert finish['usage']=={'input_tokens':7,'total_tokens':7}
    assert 'private input' not in str(events) and 'private-key' not in str(events)


def test_actual_rerank_response_retained_without_second_call(monkeypatch):
    events=events_capture(monkeypatch)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'results':[{'index':0,'relevance_score':.9}], 'usage':{'total_tokens':12}})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    @asynccontextmanager
    async def owned(**kwargs): yield client
    monkeypatch.setattr(rerank,'provider_request',owned)
    service=rerank.RerankService.__new__(rerank.RerankService)
    model=SimpleNamespace(base_url='https://fixture.invalid',api_key='fake',model_name='rerank')
    async def run():
        try:return await service._arerank_via_api('private query',['private passage'],model)
        finally:await client.aclose()
    assert asyncio.run(run())[0]['score']==.9 and len(calls)==1
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert finish['usage']=={'total_tokens':12} and finish['usage_status']=='PROVIDER_REPORTED'
    assert 'private query' not in str(events) and 'private passage' not in str(events)


@pytest.mark.parametrize('usage,expected,invalid', [
    ({'input_tokens':5,'prompt_tokens':5,'total_tokens':5}, {'input_tokens':5,'total_tokens':5}, []),
    ({'input_tokens':5,'prompt_tokens':6,'output_tokens':True,'total_tokens':9}, {'total_tokens':9}, ['input_tokens','output_tokens']),
    ({'prompt_tokens':False,'total_tokens':-1}, {}, ['input_tokens','total_tokens']),
    ({'prompt_tokens':0,'total_tokens':0}, {'input_tokens':0,'total_tokens':0}, []),
    (None, {}, []),
])
def test_sync_http_partial_conflicting_invalid_and_zero(monkeypatch, usage, expected, invalid):
    events=events_capture(monkeypatch)
    response=requests.Response(); response.status_code=200
    import json
    response._content=json.dumps({'usage':usage}).encode()
    assert llm.invoke_provider_http(lambda:response,stage='embedding',model_name='test') is response
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert finish.get('usage',{})==expected
    assert finish.get('usage_invalid_fields',[])==invalid
    assert finish['usage_status']==('PROVIDER_REPORTED' if expected else 'UNKNOWN')


def test_sdk_metadata_is_trusted_but_aggregate_only_is_unknown(monkeypatch):
    events=events_capture(monkeypatch)
    response=SimpleNamespace(response_metadata={'token_usage':{'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}},
                             usage_metadata={'input_tokens':999,'output_tokens':999})
    model=SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    assert asyncio.run(llm.invoke_provider_model(model,[],config={},stage='agent',model_name='synthetic')) is response
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert finish['usage']=={'input_tokens':4,'output_tokens':2,'total_tokens':6}
    events.clear()
    response2=SimpleNamespace(response_metadata={},usage_metadata={'input_tokens':999,'output_tokens':999})
    model.ainvoke=AsyncMock(return_value=response2)
    asyncio.run(llm.invoke_provider_model(model,[],config={},stage='agent',model_name='synthetic'))
    assert next(data for name,data in events if name=='provider.request_finished')['usage_status']=='UNKNOWN'


def test_unconsumed_stream_never_read_and_malformed_json_is_fail_open(monkeypatch):
    events=events_capture(monkeypatch)
    class Stream(httpx.SyncByteStream):
        def __iter__(self): raise AssertionError('must not consume')
    request=httpx.Request('POST','https://fixture.invalid')
    response=httpx.Response(200,stream=Stream(),request=request)
    assert llm.invoke_provider_http(lambda:response,stage='embedding',model_name='test') is response
    assert not response.is_stream_consumed
    bad=httpx.Response(200,content=b'not-json',request=request)
    assert llm.invoke_provider_http(lambda:bad,stage='embedding',model_name='test') is bad
    assert all(data['usage_status']=='UNKNOWN' for name,data in events if name=='provider.request_finished')


def test_timeout_is_one_unknown_attempt_no_replay(monkeypatch):
    events=events_capture(monkeypatch); sends=AsyncMock(side_effect=httpx.ReadTimeout('private'))
    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(llm.invoke_provider_http_async(sends,stage='embedding',model_name='test'))
    sends.assert_awaited_once()
    start=next(data for name,data in events if name=='provider.request_started')
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert caught.value.provider_status=='UNKNOWN' and finish['usage_status']=='UNKNOWN'
    assert start['local_request_id']==finish['local_request_id']


def test_buffered_grounded_response_attaches_actual_usage(monkeypatch):
    events=events_capture(monkeypatch)
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='answer'),finish_reason='stop')],
                             usage={'prompt_tokens':8,'completion_tokens':2,'total_tokens':10})
    create=AsyncMock(return_value=response)
    model=SimpleNamespace(model='test',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result=asyncio.run(grounded_generation.generate_grounded_candidate(model,[{'role':'user','content':'q'}],[{'content':'evidence'}]))
    assert result[0]=='answer'
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert finish['usage']=={'input_tokens':8,'output_tokens':2,'total_tokens':10}
    create.assert_awaited_once()


def test_local_trace_retains_numeric_usage_with_prometheus_disabled(monkeypatch, tmp_path):
    import json
    from app.services import observability
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_PROMETHEUS_ENABLED','false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events.jsonl'))
    monkeypatch.setattr(observability,'_sdk',lambda:None)
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={
        'data':[{'index':0,'embedding':[.2]}], 'usage':{'prompt_tokens':7,'total_tokens':7,'hidden':'secret'}})))
    @asynccontextmanager
    async def owned(**kwargs): yield client
    monkeypatch.setattr(embedding,'provider_request',owned)
    service=embedding.EmbeddingService(base_url='https://fixture.invalid',api_key='private-key',model='synthetic')
    async def run():
        try:
            with observability.span('qa.request'):
                assert await service.embed_query('private question')==[.2]
        finally: await client.aclose()
    asyncio.run(run())
    raw=(tmp_path/'events.jsonl').read_text(encoding='utf-8')
    rows=[json.loads(line) for line in raw.splitlines()]
    start=next(row for row in rows if row.get('stage')=='provider.request_started')
    finish=next(row for row in rows if row.get('stage')=='provider.request_finished')
    assert finish['metadata']['local_request_id']==start['metadata']['local_request_id']
    assert finish['metadata']['usage']=={'input_tokens':7,'total_tokens':7}
    assert finish['metadata']['usage_status']=='PROVIDER_REPORTED'
    assert 'private-key' not in raw and 'private question' not in raw and 'secret' not in raw


def test_cancelled_attempt_is_unknown_and_never_replayed(monkeypatch):
    events=events_capture(monkeypatch)
    async def run():
        entered=asyncio.Event()
        async def send():
            entered.set()
            await asyncio.Event().wait()
        task=asyncio.create_task(llm.invoke_provider_http_async(send,stage='embedding',model_name='test'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    starts=[data for name,data in events if name=='provider.request_started']
    finishes=[data for name,data in events if name=='provider.request_finished']
    assert len(starts)==len(finishes)==1
    assert finishes[0]['status']=='CANCELLED' and finishes[0]['usage_status']=='UNKNOWN'
    assert starts[0]['local_request_id']==finishes[0]['local_request_id']


def test_repeated_usage_counter_conflicts_remain_unknown():
    attempt={'usage_status':'UNKNOWN'}
    llm.attach_provider_usage(attempt,raw_usage={'prompt_tokens':5,'total_tokens':5})
    llm.attach_provider_usage(attempt,raw_usage={'prompt_tokens':5,'total_tokens':5})
    assert attempt['usage']=={'input_tokens':5,'total_tokens':5}
    llm.attach_provider_usage(attempt,raw_usage={'prompt_tokens':6})
    assert attempt['usage']=={'total_tokens':5} and attempt['usage_invalid_fields']==['input_tokens']
    llm.attach_provider_usage(attempt,raw_usage={'prompt_tokens':5})
    assert attempt['usage']=={'total_tokens':5}


def test_model_no_results_fallback_usage_in_existing_attempt(monkeypatch):
    events=events_capture(monkeypatch)
    monkeypatch.setattr(grounded_generation.get_settings(),'FALLBACK_STRATEGY','model')
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='answer'),finish_reason='stop')],
                             usage={'prompt_tokens':3,'completion_tokens':1,'total_tokens':4})
    create=AsyncMock(return_value=response)
    model=SimpleNamespace(model='test',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result=asyncio.run(grounded_generation.generate_no_results_fallback(model,'question'))
    assert result[1]=='model'
    finish=next(data for name,data in events if name=='provider.request_finished')
    assert finish['usage']=={'input_tokens':3,'output_tokens':1,'total_tokens':4}
    create.assert_awaited_once()
