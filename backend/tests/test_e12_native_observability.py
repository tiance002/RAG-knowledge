import json
from unittest.mock import patch

from app.services import observability as obs
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from langfuse import Langfuse


def test_native_actual_sdk_one_generation_and_ordered_transitions(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-synthetic', secret_key='synthetic', tracer_provider=TracerProvider(sampler=TraceIdRatioBased(1.0)), span_exporter=exporter, mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_attempted', True)
    with obs.span('qa.request'):
        with obs.span('retrieval.search'):
            obs.candidates('retrieval.before_dedup', [{'id':'a','score':.7},{'id':'b','score':.5}])
            obs.event('rag.candidate_transition', operation='topk', input_candidate_ids=['a','b'], output_candidate_ids=['a'], removed=[{'candidate_id':'b','reason_code':'outside_topk'}])
            obs.candidates('rerank.selected', [{'id':'a','score':.7,'rerank_score':.9}])
        with obs.generation('answer.generate', model='fake', input={'content_visibility':'NOT_CAPTURED'}) as gen:
            obs.event('llm.response', model='fake', usage={'total_tokens': 2})
            gen.update(output={'content_visibility':'NOT_CAPTURED'}, usage={'total_tokens':2}, finish_reason='stop')
    client.flush()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert sorted(names) == ['answer.generate', 'qa.request', 'retrieval.search']
    retrieval = next(s for s in spans if s.name == 'retrieval.search')
    attrs = dict(retrieval.attributes)
    assert attrs['langfuse.observation.type'] == 'retriever'
    stages = json.loads(attrs['langfuse.observation.metadata.stages'])
    assert [x['stage'] for x in stages] == ['retrieval.before_dedup', 'rag.candidate_transition', 'rerank.selected']
    assert [r['id'] for r in stages[0]['metadata']['candidates']] == ['a','b']
    assert stages[1]['metadata']['removed'][0]['candidate_id'] == 'b'
    assert stages[2]['metadata']['candidates'][0]['score_value'] == .9
    gen = next(s for s in spans if s.name == 'answer.generate')
    assert dict(gen.attributes)['langfuse.observation.type'] == 'generation'
    assert gen.parent.span_id == next(s for s in spans if s.name == 'qa.request').context.span_id
    client.shutdown()


def test_native_callback_fails_closed_and_does_not_mutate_config(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    original={'callbacks': ['existing']}
    result, attached, reason = obs.langchain_callback_config(original)
    assert result == original and result is not original and not attached
    assert reason == 'PRE_MEDIA_AUTH_UNAVAILABLE'


def test_native_off_does_not_change_operation(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    with obs.generation('answer.generate', model='fake') as generation:
        generation.update(output='private')
        assert sum([1,2]) == 3
    assert obs.native_enabled() is False


def test_native_sdk_provider_uses_explicit_full_sampling(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('LANGFUSE_ENABLED','true')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY','pk-fake')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY','synthetic')
    monkeypatch.setattr(obs, '_attempted', False)
    monkeypatch.setattr(obs, '_client', None)
    found={}
    def construct(**kwargs):
        found.update(kwargs)
        return object()
    with patch('langfuse.Langfuse', construct):
        assert obs._sdk() is not None
    assert 'TraceIdRatioBased{1.0}' in found['tracer_provider'].sampler.get_description()
    assert callable(found['mask_otel_spans']) and callable(found['should_export_span'])


def test_eval_inline_text_callback_uses_same_client_and_rejects_secret_output(monkeypatch, tmp_path):
    from app.services.trace_schema import capture_scope
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import HumanMessage
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY', 'INLINE')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', 'pk-e12-safe-callback')
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-e12-safe-callback', secret_key='synthetic', tracer_provider=TracerProvider(), span_exporter=exporter, should_export_span=obs._native_should_export, mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_attempted', True)
    manifest={'entries':[{'dataset_version':'v', 'qid':'q', 'source_sha256':'a'*64, 'cloud_allowed':True}]}
    with capture_scope(qid='q', dataset_version='v', source_sha256s=['a'*64], manifest=manifest, capture_mode='EVAL'):
        with obs.span('qa.request'):
            cfg, attached, reason = obs.langchain_callback_config({}, messages=[HumanMessage(content='public question')])
            assert attached, reason
            assert FakeListChatModel(responses=['public answer']).invoke([HumanMessage(content='public question')], config=cfg).content == 'public answer'
            cfg, attached, reason = obs.langchain_callback_config({}, messages=[HumanMessage(content='public question')])
            assert attached, reason
            assert FakeListChatModel(responses=['Authorization: Bearer private-value']).invoke([HumanMessage(content='public question')], config=cfg).content.startswith('Authorization')
            _, attached, _ = obs.langchain_callback_config({}, messages=[HumanMessage(content=[{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}])])
            assert not attached
    client.flush()
    spans = exporter.get_finished_spans()
    assert len([s for s in spans if dict(s.attributes).get('langfuse.observation.type') == 'generation']) == 1
    exported = repr([dict(s.attributes) for s in spans])
    assert 'public question' in exported and 'public answer' in exported
    assert 'private-value' not in exported
    assert 'PRE_MEDIA_INPUT_REJECTED' in (tmp_path / 'events.jsonl').read_text(encoding='utf-8')
    client.shutdown()


def test_eval_inline_tool_callback_records_identity_and_rejects_unsafe_output(monkeypatch, tmp_path):
    from app.services.trace_schema import capture_scope
    from langchain_core.tools import tool
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY','INLINE')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path / 'events.jsonl'))
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY','pk-e12-tool-callback')
    exporter=InMemorySpanExporter()
    client=Langfuse(public_key='pk-e12-tool-callback',secret_key='synthetic',tracer_provider=TracerProvider(),span_exporter=exporter,should_export_span=obs._native_should_export,mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs,'_client',client)
    monkeypatch.setattr(obs,'_attempted',True)
    @tool
    def lookup(query: str) -> str:
        """Read synthetic evidence."""
        return 'public result' if query == 'public question' else 'Authorization: Bearer private-tool-output'
    manifest={'entries':[{'dataset_version':'v','qid':'q','source_sha256':'a'*64,'cloud_allowed':True}]}
    with capture_scope(qid='q',dataset_version='v',source_sha256s=['a'*64],manifest=manifest,capture_mode='EVAL'):
        with obs.span('qa.request'):
            cfg, attached, reason=obs.langchain_callback_config({},tool_input={'query':'public question'},tool_metadata={'tool_call_id':'call-1','retrieval_attempt_id':'attempt-1'})
            assert attached,reason
            assert lookup.invoke({'query':'public question'},config=cfg)=='public result'
            cfg, attached, reason=obs.langchain_callback_config({},tool_input={'query':'second'},tool_metadata={'tool_call_id':'call-2','retrieval_attempt_id':'attempt-2'})
            assert attached,reason
            assert lookup.invoke({'query':'second'},config=cfg).startswith('Authorization')
            _, attached, _=obs.langchain_callback_config({},tool_input={'query':'data:image/png;base64,AAAA'})
            assert not attached
    client.flush()
    spans=[s for s in exporter.get_finished_spans() if s.attributes.get('langfuse.observation.type')=='tool']
    assert len(spans)==2
    attrs=[dict(s.attributes) for s in spans]
    assert 'call-1' in repr(attrs) and 'attempt-1' in repr(attrs)
    assert 'private-tool-output' not in repr(attrs)
    assert 'PRE_MEDIA_OUTPUT_REJECTED' in (tmp_path/'events.jsonl').read_text(encoding='utf-8')
    client.shutdown()


def test_native_content_policy_and_source_dedup(monkeypatch, tmp_path):
    from app.services.trace_schema import capture_scope
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events.jsonl'))
    exporter=InMemorySpanExporter()
    client=Langfuse(public_key='pk-e12-dedup',secret_key='synthetic',tracer_provider=TracerProvider(),span_exporter=exporter, mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True), should_export_span=obs._native_should_export, mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs,'_client',client)
    monkeypatch.setattr(obs,'_attempted',True)
    manifest={'entries':[{'dataset_version':'v','qid':'q','source_sha256':'a'*64,'cloud_allowed':True}]}
    with capture_scope(qid='q',dataset_version='v',source_sha256s=['a'*64],manifest=manifest,capture_mode='EVAL'):
        monkeypatch.setenv('RAG_E12_CONTENT_POLICY','LOCAL_ONLY')
        assert obs.capture('allowed text',content_role='provider_request')['content_visibility'] != 'CLOUD_INLINE'
        monkeypatch.setenv('RAG_E12_CONTENT_POLICY','INLINE')
        assert obs.capture('allowed text',content_role='provider_request')['content_visibility'] == 'CLOUD_INLINE'
        with obs.span('retrieval.search'):
            row={'id':'a','content':'same runtime content','score':.7}
            obs.candidates('retrieval.before_dedup',[row])
            obs.candidates('retrieval.after_dedup',[row])
    client.flush()
    span=next(s for s in exporter.get_finished_spans() if s.name=='retrieval.search')
    attrs=dict(span.attributes)
    sources=json.loads(attrs['langfuse.observation.metadata.source_contents'])
    stages=json.loads(attrs['langfuse.observation.metadata.stages'])
    assert len(sources)==1
    ref=stages[0]['metadata']['candidates'][0]['content_ref']
    assert ref == stages[1]['metadata']['candidates'][0]['content_ref']
    assert ref in sources and sources[ref]['content_sha256']==ref
    assert isinstance(obs.sanitize('private uncontrolled string', 'content_ref'),dict)
    client.shutdown()


def test_native_large_candidate_array_is_lossless(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events'))
    exporter=InMemorySpanExporter()
    client=Langfuse(public_key='pk-e12-large',secret_key='synthetic',tracer_provider=TracerProvider(),span_exporter=exporter)
    monkeypatch.setattr(obs,'_client',client)
    monkeypatch.setattr(obs,'_attempted',True)
    rows=[{'id':f'c{i}','score':i/500} for i in range(300)]
    with obs.span('retrieval.search'):
        obs.candidates('retrieval.before_dedup',rows)
        obs.event('rag.candidate_transition',operation='topk',input_candidate_ids=[r['id'] for r in rows],output_candidate_ids=['c0'],removed=[{'candidate_id':r['id'],'reason_code':'outside_topk','rank_before':i+1} for i,r in enumerate(rows) if i])
    client.flush()
    attrs=dict(exporter.get_finished_spans()[0].attributes)
    stages=json.loads(attrs['langfuse.observation.metadata.stages'])
    assert len(stages[0]['metadata']['candidates'])==300
    assert stages[0]['metadata']['candidates'][-1]['id']=='c299'
    assert len(stages[1]['metadata']['removed'])==299
    assert stages[1]['metadata']['removed'][-1]['candidate_id']=='c299'
    client.shutdown()


def test_sdk_sampler_explicit_zero_and_full_in_memory():
    import uuid
    counts=[]
    for rate in (0.0, 1.0):
        exporter=InMemorySpanExporter()
        client=Langfuse(public_key='pk-sample-'+uuid.uuid4().hex,secret_key='synthetic',tracer_provider=TracerProvider(sampler=TraceIdRatioBased(rate)),span_exporter=exporter)
        with client.start_as_current_observation(name='root'):
            with client.start_as_current_observation(name='child'): pass
        client.flush()
        counts.append(len(exporter.get_finished_spans()))
        client.shutdown()
    assert counts==[0,2]


def test_native_sdk_failure_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events'))
    monkeypatch.setattr(obs,'_sdk',lambda: (_ for _ in ()).throw(RuntimeError('not available')))
    with obs.span('qa.request'):
        obs.event('qa.answer',answer_mode='GROUNDED_GENERATION')
        result='business result'
    assert result=='business result'
    assert 'sdk.initialization_failed' in (tmp_path/'events').read_text(encoding='utf-8')


def test_native_mask_handles_json_and_bare_safe_primitives_without_leaking():
    from types import SimpleNamespace
    attrs={
        'langfuse.observation.metadata.run_id':'synthetic-run',
        'langfuse.observation.metadata.qid':'"synthetic-q"',
        'langfuse.observation.metadata.execution_status':'CANCELLED',
        'langfuse.observation.metadata.code_hash':'a'*64,
        'langfuse.observation.metadata.free_text':'unapproved bare content',
        'langfuse.observation.metadata.reason_code':'Authorization: Bearer do-not-export',
        'langfuse.observation.input':'data:image/png;base64,AAAA',
    }
    result=obs._native_mask_spans(params=SimpleNamespace(spans={'span':SimpleNamespace(attributes=attrs)}))
    dropped=set(result.span_patches['span'].delete_attributes)
    assert 'langfuse.observation.metadata.free_text' in dropped
    assert 'langfuse.observation.metadata.reason_code' in dropped
    assert 'langfuse.observation.input' in dropped
    assert not dropped.intersection({'langfuse.observation.metadata.run_id','langfuse.observation.metadata.qid','langfuse.observation.metadata.execution_status','langfuse.observation.metadata.code_hash'})


def test_failed_rerank_batch_marks_observed_score_origin_without_changing_results(monkeypatch, tmp_path):
    from app.services import rerank
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events'))
    monkeypatch.setattr(rerank,'estimate_tokens',lambda text:len(text))
    class Service:
        def rerank(self,query,candidates,model=None):
            if candidates==['bad']:
                raise ValueError('synthetic rerank failure')
            return [{'index':0,'score':.95}]
    rows=[{'id':'failed','content':'bad','score':.6},{'id':'scored','content':'yes','score':.2}]
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    baseline=rerank.batch_rerank('q',rows,Service(),None,max_context_tokens=5,top_k=2)
    exporter=InMemorySpanExporter()
    client=Langfuse(public_key='pk-e12-rerank-fallback',secret_key='synthetic',tracer_provider=TracerProvider(),span_exporter=exporter,mask=lambda **kw:obs.sanitize(kw.get('data'),final_export=True),should_export_span=obs._native_should_export,mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs,'_client',client);monkeypatch.setattr(obs,'_attempted',True)
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    observed=rerank.batch_rerank('q',rows,Service(),None,max_context_tokens=5,top_k=2)
    assert observed==baseline
    assert next(r for r in observed if r['id']=='failed')['rerank_score']==.6
    client.flush()
    attrs=dict(next(s for s in exporter.get_finished_spans() if s.name=='rerank.batch').attributes)
    stages=json.loads(attrs['langfuse.observation.metadata.stages'])
    candidates=next(s['metadata']['candidates'] for s in stages if s['stage']=='rerank.batch_output')
    failed=next(c for c in candidates if c['id']=='failed')
    scored=next(c for c in candidates if c['id']=='scored')
    assert failed['score_kind']=='original_retrieval' and failed['score_value']==.6
    assert failed['rerank_score'] is None and failed['fallback_score_origin']=='original_score'
    assert scored['score_kind']=='rerank' and scored['score_value']==.95 and scored['rerank_score']==.95
    client.shutdown()


def test_graph_callback_copies_manager_preserves_business_metadata_and_hides_private_fields(monkeypatch,tmp_path):
    import asyncio
    from app.services.trace_schema import capture_scope
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.messages import HumanMessage
    from langchain_core.callbacks.base import AsyncCallbackHandler
    from langchain_core.callbacks.manager import AsyncCallbackManager
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA','e12')
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY','INLINE')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events'))
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY','pk-e12-graph-callback')
    exporter=InMemorySpanExporter()
    client=Langfuse(public_key='pk-e12-graph-callback',secret_key='synthetic',tracer_provider=TracerProvider(),span_exporter=exporter,mask=lambda **kw:obs.sanitize(kw.get('data'),final_export=True),should_export_span=obs._native_should_export,mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs,'_client',client); monkeypatch.setattr(obs,'_attempted',True)
    seen=[]
    class Existing(AsyncCallbackHandler):
        async def on_chat_model_start(self,serialized,messages,*,metadata=None,**kwargs):
            seen.append(dict(metadata or {}))
    existing=Existing(); manager=AsyncCallbackManager([existing],inheritable_handlers=[existing])
    metadata={'langgraph_step':1,'langgraph_node':'agent','private_custom':'EXAMPLE_PRIVATE_GRAPH_SENTINEL'}
    original={'callbacks':manager,'metadata':metadata}
    manifest={'entries':[{'dataset_version':'v','qid':'q','source_sha256':'a'*64,'cloud_allowed':True}]}
    async def run():
        with capture_scope(qid='q',dataset_version='v',source_sha256s=['a'*64],manifest=manifest,capture_mode='EVAL'):
            with obs.span('qa.request'):
                cfg,attached,reason=obs.langchain_callback_config(original,messages=[HumanMessage(content='public')])
                assert attached,reason
                assert cfg['callbacks'] is not manager and len(manager.handlers)==1
                assert cfg['metadata'] is metadata and cfg['metadata']==metadata
                return await FakeListChatModel(responses=['public answer']).ainvoke([HumanMessage(content='public')],config=cfg)
    assert asyncio.run(run()).content=='public answer'
    assert seen and seen[0]['private_custom']=='EXAMPLE_PRIVATE_GRAPH_SENTINEL'
    client.flush();spans=exporter.get_finished_spans()
    assert len([s for s in spans if s.attributes.get('langfuse.observation.type')=='generation'])==1
    assert 'EXAMPLE_PRIVATE_GRAPH_SENTINEL' not in repr([dict(s.attributes) for s in spans])
    client.shutdown()


def test_actual_stategraph_tool_retrieval_descriptor_parent_is_sdk_tool(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    output=tmp_path/'agent'
    env=dict(os.environ, PYTHONIOENCODING='utf-8')
    completed=subprocess.run([sys.executable,str(root/'scripts/run_e12_acceptance.py'),'--execute','--schema','e12','--content-policy','INLINE','--case','agent_two_rounds','--output-dir',str(output)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)
    assert completed.returncode==0,completed.stdout+completed.stderr
    spans=json.loads((output/'observations.json').read_text(encoding='utf8'))
    by_id={span['id']:span for span in spans}
    searches=[span for span in spans if span['name']=='retrieval.search']
    assert len(searches)==2
    for span in searches:
        assert by_id[span['parent_id']]['name']=='search_knowledge_base'
        descriptor=json.loads(span['attributes']['langfuse.observation.input'])
        assert descriptor['observation_id']==span['id']
        assert descriptor['parent_observation_id']==span['parent_id']
        assert json.loads(span['attributes']['langfuse.observation.metadata.parent_observation_id'])==span['parent_id']


def test_actual_sdk_initialization_failure_cannot_deadlock_business(monkeypatch, tmp_path):
    import threading
    import langfuse
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'true')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', 'synthetic-public')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'synthetic-secret')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_client', None)
    monkeypatch.setattr(obs, '_attempted', False)
    monkeypatch.setattr(obs, '_sdk_init_error', None)
    def unavailable(**kwargs):
        raise RuntimeError('synthetic SDK initialization failure')
    monkeypatch.setattr(langfuse, 'Langfuse', unavailable)
    completed = []
    def request():
        with obs.span('qa.request'):
            completed.append('business continues')
    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), 'SDK error logging must not reacquire a non-reentrant init lock'
    assert completed == ['business continues']
    assert obs._client is None and obs._sdk_init_error == 'RuntimeError'
    assert 'sdk.initialization_failed' in (tmp_path/'events').read_text(encoding='utf-8')
