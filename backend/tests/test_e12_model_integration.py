"""E1.2 model lifetimes, offline with the real SDK and fake providers."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.services import observability as obs, grounded_generation, llm
from langchain_core.messages import HumanMessage


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    import uuid
    client = Langfuse(public_key='pk-e12-' + uuid.uuid4().hex, secret_key='test-only', tracer_provider=TracerProvider(),
        span_exporter=exporter, mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_sdk', lambda: client)
    monkeypatch.setattr(obs, '_client', client)
    yield client, exporter
    client.shutdown()


def test_direct_provider_exactly_one_generation_with_actual_request_and_usage(sdk):
    client, exporter = sdk
    response = SimpleNamespace(id='fake', model='fake-model', usage={'prompt_tokens':4,'completion_tokens':2,'total_tokens':6},
        choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'), finish_reason='stop')])
    async def create(**kwargs):
        await asyncio.sleep(.01)
        return response
    create = AsyncMock(side_effect=create)
    service = SimpleNamespace(model='fake-model', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def run():
        with obs.span('root'):
            return await grounded_generation.generate_grounded_candidate(service,
                [{'role':'user','content':'question'}], [{'chunk_id':'c','content':'PRIVATE_SOURCE_LITERAL'}])
    result = asyncio.run(run())
    assert result[0] == 'answer [1]' and create.await_count == 1
    assert 'PRIVATE_SOURCE_LITERAL' in create.await_args.kwargs['messages'][0]['content']
    client.flush()
    spans = exporter.get_finished_spans()
    generations = [s for s in spans if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(generations) == 1 and generations[0].name == 'answer.generate'
    gen = generations[0]
    assert gen.end_time > gen.start_time and gen.parent is not None
    attrs = str(dict(gen.attributes))
    assert 'content_sha256' in attrs and 'fake-model' in attrs and 'total' in attrs
    assert 'PRIVATE_SOURCE_LITERAL' not in attrs and 'answer [1]' not in attrs


def test_real_langchain_fake_model_fallback_one_generation_per_call(sdk):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    client, exporter = sdk
    model = FakeListChatModel(responses=['first','second'])
    async def run():
        with obs.span('root'):
            return [await llm.invoke_provider_model(model, [HumanMessage(content='private')],
                        config={}, stage='agent', model_name='fake') for _ in range(2)]
    results = asyncio.run(run())
    assert [r.content for r in results] == ['first','second']
    client.flush()
    spans = exporter.get_finished_spans()
    gens = [s for s in spans if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(gens) == 2 and all(s.name == 'agent.agent' for s in gens)
    assert all(s.parent is not None for s in gens)
    assert all('private' not in str(dict(s.attributes)) for s in gens)


def test_stream_cancel_no_retry_and_generation_finishes(sdk):
    client, exporter = sdk
    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content='part'))])
        await asyncio.Event().wait()
    create = AsyncMock(return_value=chunks())
    service = llm.LLMService.__new__(llm.LLMService)
    service.model = 'fake-model'
    service._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async def run():
        with obs.span('root'):
            gen = service.generate_stream([{'role':'user','content':'question'}])
            assert await anext(gen) == 'part'
            await gen.aclose()
    asyncio.run(run())
    assert create.await_count == 1
    client.flush()
    gens = [s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(gens) == 1 and gens[0].name == 'answer.generate'
    assert 'CANCELLED' in str(dict(gens[0].attributes))


def test_actual_langchain_tool_fallback_records_each_call_and_identity(sdk, monkeypatch):
    from langchain_core.tools import tool
    from langchain_core.messages import AIMessage
    from app.services import agent_workflow
    client, exporter = sdk
    calls = []
    @tool
    async def search_knowledge_base(query: str, kb_ids: list[str]) -> str:
        """Search fixed test knowledge bases."""
        calls.append((query, kb_ids))
        return json.dumps({'context':'evidence', 'citations':[], 'retrieval_status':'SUCCESS'})
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', search_knowledge_base)
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    async def run():
        state = {'kb_ids':['kb'], 'search_params':{}, 'retry_count':0}
        with obs.span('root'):
            for i in range(2):
                state['messages'] = [AIMessage(content='', tool_calls=[{'name':'search_knowledge_base',
                    'id':f'call-{i}', 'args':{'query':f'query-{i}'}}])]
                state.update(await workflow.run_tools(state, {}))
        return state
    state = asyncio.run(run())
    assert calls == [('query-0',['kb']),('query-1',['kb'])]
    assert state['retrieval_attempt_count'] == 2
    client.flush()
    spans = exporter.get_finished_spans()
    tools = [s for s in spans if s.attributes.get('langfuse.observation.type') == 'tool']
    assert len(tools) == 2 and all(s.name == 'search_knowledge_base' for s in tools)
    metadata = [str(dict(s.attributes)) for s in tools]
    assert all('retrieval_attempt_id' in m and 'PRE_MEDIA_AUTH_UNAVAILABLE' in m for m in metadata)
    assert any('call-0' in m for m in metadata) and any('call-1' in m for m in metadata)
    assert all('query-0' not in m and 'query-1' not in m for m in metadata)


def test_unknown_provider_error_not_retried_and_generation_records_unknown(sdk):
    client, exporter = sdk
    create = AsyncMock(side_effect=TimeoutError('unknown delivery'))
    service = SimpleNamespace(model='fake', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def run():
        with obs.span('root'):
            with pytest.raises(TimeoutError) as caught:
                await grounded_generation.generate_grounded_candidate(service, [{'role':'user','content':'q'}],
                                                                       [{'chunk_id':'c','content':'evidence'}])
            assert caught.value.provider_status == 'UNKNOWN'
    asyncio.run(run())
    assert create.await_count == 1
    client.flush()
    gens = [s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(gens) == 1 and 'UNKNOWN' in str(dict(gens[0].attributes))


def test_authorized_text_uses_official_callback_without_manual_duplicate(sdk, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from app.services.trace_schema import capture_scope
    client, exporter = sdk
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY', 'INLINE')
    source='a'*64
    manifest={'entries':[{'dataset_version':'v', 'qid':'q', 'source_sha256':source, 'cloud_allowed':True}]}
    model=FakeListChatModel(responses=['synthetic approved reply'])
    async def run():
        with capture_scope(qid='q', dataset_version='v', source_sha256s=[source], manifest=manifest, capture_mode='EVAL'):
            with obs.span('root'):
                return await llm.invoke_provider_model(model, [HumanMessage(content='approved input')],
                                                       config={}, stage='agent', model_name='fake')
    assert asyncio.run(run()).content == 'synthetic approved reply'
    client.flush()
    spans=exporter.get_finished_spans()
    gens=[s for s in spans if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(gens)==1 and gens[0].name != 'agent.agent'
    assert gens[0].parent is not None


@pytest.mark.parametrize('as_string', [False, True])
def test_http_chat_fallback_is_one_generation_and_response_unchanged(sdk, as_string):
    client, exporter = sdk
    payload={'choices':[{'message':{'content':'ok'}, 'finish_reason':'stop'}],
             'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}}
    response = SimpleNamespace(status_code=200, headers={}, json=lambda:json.dumps(payload) if as_string else payload)
    calls=[]
    def send():
        calls.append(1)
        return response
    messages=[{'role':'user','content':'rerank candidates'}]
    with obs.span('root'):
        result=llm.invoke_provider_http(send, stage='rerank_chat', model_name='fake', check_status=False,
                                        messages=messages, model_parameters={'temperature':0})
    assert result is response and len(calls)==1
    client.flush()
    gens=[s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(gens)==1 and gens[0].name == 'rerank.chat'
    assert 'total' in str(dict(gens[0].attributes))


def test_authorized_actual_tool_uses_official_callback_once(sdk, monkeypatch):
    from langchain_core.tools import tool
    from langchain_core.messages import AIMessage
    from app.services import agent_workflow
    from app.services.trace_schema import capture_scope
    client, exporter=sdk
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY', 'INLINE')
    source='a'*64
    manifest={'entries':[{'dataset_version':'v','qid':'q','source_sha256':source,'cloud_allowed':True}]}
    calls=[]
    @tool
    async def search_knowledge_base(query:str, kb_ids:list[str]) -> str:
        """Search synthetic corpus."""
        calls.append(query)
        return json.dumps({'context':'safe', 'citations':[], 'retrieval_status':'SUCCESS'})
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', search_knowledge_base)
    workflow=agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    async def run():
        state={'kb_ids':['kb'], 'search_params':{}, 'messages':[AIMessage(content='', tool_calls=[{
            'name':'search_knowledge_base','id':'tc-1','args':{'query':'approved question'}}])]}
        with capture_scope(qid='q',dataset_version='v',source_sha256s=[source],manifest=manifest,capture_mode='EVAL'):
            with obs.span('root'):
                return await workflow.run_tools(state,{})
    state=asyncio.run(run())
    assert calls==['approved question'] and state['retrieval_attempt_count']==1
    client.flush()
    spans=exporter.get_finished_spans()
    tool_spans=[s for s in spans if s.attributes.get('langfuse.observation.type')=='tool']
    assert len(tool_spans)==1
    attributes=str(dict(tool_spans[0].attributes))
    assert 'tc-1' in attributes and state['retrieval_attempt_id'] in attributes


def test_grounded_provider_same_request_across_off_e11_e12(sdk, monkeypatch):
    client, _ = sdk
    observed=[]
    async def create(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(id='fake',model='fake',usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content='answer'),finish_reason='stop')])
    service=SimpleNamespace(model='fake',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    async def run():
        return await grounded_generation.generate_grounded_candidate(service,
            [{'role':'user','content':'q'}],[{'chunk_id':'c','content':'evidence'}])
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    off=asyncio.run(run())
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e11')
    old=asyncio.run(run())
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    new=asyncio.run(run())
    assert off==old==new and len(observed)==3 and observed[0]==observed[1]==observed[2]
    client.flush()


def test_rerank_sdk_chatcompletion_response_is_captured_without_mutation(sdk):
    import hashlib
    from openai.types.chat import ChatCompletion
    client, exporter = sdk
    response = ChatCompletion.model_validate({
        'id':'chatcmpl-fixture','created':123,'model':'fake-model','object':'chat.completion',
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'[{"index":0,"score":0.9}]'}}],
        'usage':{'prompt_tokens':12,'completion_tokens':7,'total_tokens':19},
    })
    original = response.model_dump()
    calls=[]
    def send():
        calls.append(1)
        return response
    with obs.span('root'):
        actual = llm.invoke_provider_http(send, stage='rerank_chat', model_name='fake-model', check_status=False,
            messages=[{'role':'user','content':'rank this'}], model_parameters={'temperature':0.0})
    assert actual is response and response.model_dump()==original and len(calls)==1
    client.flush()
    gens=[s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type')=='generation']
    assert len(gens)==1 and gens[0].name=='rerank.chat'
    attrs=dict(gens[0].attributes)
    output=json.loads(attrs['langfuse.observation.output'])
    assert output['content_sha256']==hashlib.sha256(b'[{"index":0,"score":0.9}]').hexdigest()
    usage=json.loads(attrs['langfuse.observation.usage_details'])
    assert usage == {'input':12, 'output':7, 'total':19}
    assert 'UNKNOWN' not in str(attrs)


def test_official_callback_timeout_is_unknown_not_failed_and_not_retried(sdk, monkeypatch):
    from app.services.trace_schema import capture_scope
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    client, exporter = sdk
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY', 'INLINE')
    calls = []
    class TimedOutModel(FakeListChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(messages)
            raise TimeoutError('synthetic result unknown')
    manifest = {'entries': [{'dataset_version':'v','qid':'q','source_sha256':'a'*64,'cloud_allowed':True}]}
    async def run():
        with capture_scope(run_id='timeout-run', qid='q', dataset_version='v', source_sha256s=['a'*64], manifest=manifest, capture_mode='EVAL'):
            with obs.span('qa.request'):
                with pytest.raises(TimeoutError):
                    await llm.invoke_provider_model(TimedOutModel(responses=['unused']), [HumanMessage(content='synthetic')], config={}, stage='agent', model_name='synthetic')
    asyncio.run(run())
    client.flush()
    generations = [s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type') == 'generation']
    assert len(calls) == len(generations) == 1
    assert generations[0].name == 'TimedOutModel'
    assert json.loads(generations[0].attributes['langfuse.observation.metadata.execution_status']) == 'UNKNOWN'
