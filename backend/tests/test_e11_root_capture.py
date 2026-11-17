"""Local fake-SDK tests: no network, provider, or server writes."""
import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.services import observability as obs
from app.services.trace_schema import capture_scope


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.updates = []
        self.flush_count = 0

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        if self.fail:
            raise RuntimeError('SDK unavailable')
        yield SimpleNamespace(update=lambda **data: self.updates.append((kwargs['name'], data)))

    def get_current_trace_id(self): return 'a' * 32
    def get_current_observation_id(self): return 'b' * 16
    def flush(self):
        self.flush_count += 1
        if self.fail: raise RuntimeError('offline')


def setup_observation(monkeypatch, tmp_path, client):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(obs, '_sdk', lambda: client)
    monkeypatch.setattr(obs, '_client', client)


def test_root_records_sse_answer_and_input_as_separate_standard_io(monkeypatch, tmp_path):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        obs.event('grounded.candidate', output=obs.capture('raw answer', content_role='provider_response'))
        yield ('answer_chunk', {'content': 'raw answer'})
        yield ('answer_chunk', {'content': ' + disclosure'})
        yield ('done', {})
    async def run():
        return [item async for item in stream([{'role': 'user', 'content': 'private question'}])]
    with capture_scope(run_id='run', attempt_id='attempt', qid='q', dataset_version='v'):
        result = asyncio.run(run())
    assert result[-1][0] == 'done'
    input_update, output_update = [data for name, data in client.updates if name == 'qa.request']
    assert input_update['input']['content_sha256'] == hashlib.sha256(b'private question').hexdigest()
    assert output_update['output']['content_sha256'] == hashlib.sha256(b'raw answer + disclosure').hexdigest()
    assert output_update['metadata']['final_answer_sha256'] == hashlib.sha256(b'raw answer + disclosure').hexdigest()
    assert output_update['metadata']['execution_status'] == 'RETURNED'
    assert client.flush_count == 1
    records = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    candidate = next(r for r in records if r['stage'] == 'grounded.candidate')
    assert candidate['output']['content_sha256'] == hashlib.sha256(b'raw answer').hexdigest()
    assert 'raw answer' not in json.dumps(records) and 'private question' not in json.dumps(records)


def test_root_sdk_failure_leaves_sse_unchanged(monkeypatch, tmp_path):
    client = FakeClient(fail=True)
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        yield ('answer_chunk', {'content': 'answer'})
        yield ('done', {})
    async def run(): return [item async for item in stream([{'role': 'user', 'content': 'q'}])]
    assert asyncio.run(run()) == [('answer_chunk', {'content': 'answer'}), ('done', {})]
    assert client.flush_count == 1
    records = [json.loads(line) for line in (tmp_path/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    start = next(r for r in records if r.get('stage') == 'qa.request' and r.get('event') == 'start')
    assert start['cloud_export'] == 'NOT_CONFIRMED'


def test_capture_inline_requires_both_manifest_and_explicit_allow(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    source = 'a' * 64
    manifest = {'entries': [{'source_sha256': source, 'dataset_version': 'v', 'qid': 'q', 'cloud_allowed': True}]}
    with capture_scope(run_id='r', attempt_id='a', dataset_version='v', qid='q', source_sha256s=[source], manifest=manifest, capture_mode='EVAL'):
        assert obs.capture('public', content_role='provider_request')['content_visibility'] != 'CLOUD_INLINE'
        permitted = obs.capture('public', content_role='provider_request', allow_cloud=True)
        assert permitted['content_visibility'] == 'CLOUD_INLINE'
        assert obs.sanitize(permitted, final_export=True)['text'] == 'public'
        tampered = dict(permitted, text='secret content')
        assert 'secret content' not in json.dumps(obs.sanitize(tampered, final_export=True))
    with capture_scope(dataset_version='v', qid='other', source_sha256s=[source], manifest=manifest, capture_mode='EVAL'):
        assert obs.capture('private', content_role='provider_request', allow_cloud=True)['content_visibility'] != 'CLOUD_INLINE'


def test_root_capture_failure_cannot_break_completed_sse(monkeypatch, tmp_path):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    monkeypatch.setattr(obs, 'capture', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('capture unavailable')))
    @obs.observe('qa.request')
    async def stream(messages):
        yield ('answer_chunk', {'content': 'answer'})
        yield ('done', {})
    async def run(): return [x async for x in stream([{'role': 'user', 'content': 'q'}])]
    assert asyncio.run(run()) == [('answer_chunk', {'content': 'answer'}), ('done', {})]


def test_cli_rejects_path_and_missing_root(tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'read_e11_snapshot.py'
    path = subprocess.run([sys.executable, str(script), '../private'], capture_output=True)
    assert path.returncode != 0 and b'snapshot reference' in path.stderr
    missing = subprocess.run([sys.executable, str(script), 'a' * 64, '--root', str(tmp_path/'missing')], capture_output=True)
    assert missing.returncode == 1 and not missing.stdout
    assert not (tmp_path/'missing').exists()


def test_inline_token_remains_valid_after_capture_scope_ends(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    source = 'a' * 64
    manifest = {'entries': [{'source_sha256': source, 'dataset_version': 'v', 'qid': 'q', 'cloud_allowed': True}]}
    with capture_scope(dataset_version='v', qid='q', source_sha256s=[source], manifest=manifest, capture_mode='EVAL'):
        payload = obs.capture('authorized', content_role='provider_request', allow_cloud=True)
    assert obs.sanitize(payload, final_export=True)['text'] == 'authorized'
    altered = dict(payload, qid='different')
    assert 'authorized' not in json.dumps(obs.sanitize(altered, final_export=True))


def test_sdk_background_mask_keeps_only_authorized_content(monkeypatch, tmp_path):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-test', secret_key='test-only', tracer_provider=TracerProvider(),
                      span_exporter=exporter, mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events.jsonl'))
    monkeypatch.setattr(obs, '_sdk', lambda: client)
    monkeypatch.setattr(obs, '_client', client)
    source = 'a' * 64
    manifest = {'entries': [{'source_sha256': source, 'dataset_version': 'v', 'qid': 'q', 'cloud_allowed': True}]}
    try:
        with capture_scope(dataset_version='v', qid='q', source_sha256s=[source], manifest=manifest, capture_mode='EVAL'):
            with obs.span('qa.sdk'):
                obs.event('visible', input=obs.capture('approved payload', content_role='provider_request', allow_cloud=True))
                obs.event('private', input=obs.capture('private payload', content_role='provider_request'))
        client.flush()
        spans = exporter.get_finished_spans()
        attributes = str([dict(span.attributes) for span in spans])
        assert 'approved payload' in attributes
        assert 'private payload' not in attributes
        assert '_capture_authorization' not in attributes
    finally:
        client.shutdown()


def test_actual_sdk_preserves_root_resolution_and_binds_child_snapshots(monkeypatch, tmp_path):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-e11-root-metadata', secret_key='local-test-only', tracer_provider=TracerProvider(),
                      span_exporter=exporter, mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True))
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        obs.event('qa.config', mode='chat', kb_ids=['kb1'], search_mode='fulltext', top_k=5, rerank_enabled=False)
        obs.event('retrieval.file_lineage', file_id='file1', index_version='a'*64)
        with obs.span('retrieval.search'):
            obs.observation_io(input=obs.capture('query', content_role='provider_request'))
        obs.event('grounded.request.sent', input=obs.capture(messages, content_role='provider_request'),
                  model='fake-model', max_tokens=4096, temperature=.7)
        yield ('answer_chunk', {'content':'answer'})
        yield ('done', {'generation_status':'COMPLETED', 'answer_mode':'GROUNDED_GENERATION'})
    async def run(): return [x async for x in stream([{'role':'user', 'content':'q'}])]
    try:
        with capture_scope(run_id='r', attempt_id='a', qid='q1', dataset_version='v1'):
            asyncio.run(run())
        client.flush()
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name == 'qa.request')
        child = next(s for s in spans if s.name == 'grounded.request.sent')
        search = next(s for s in spans if s.name == 'retrieval.search')
        search_input = json.loads(search.attributes['langfuse.observation.input'])
        assert search_input['observation_id'] == format(search.context.span_id, '016x')
        assert search_input['parent_observation_id'] == format(root.context.span_id, '016x')
        meta = {k.removeprefix('langfuse.observation.metadata.'): json.loads(v) for k, v in root.attributes.items()
                if k.startswith('langfuse.observation.metadata.')}
        assert meta['run_id']=='r' and meta['qid']=='q1' and meta['mode']=='chat' and meta['kb_scope']==['kb1']
        assert len(meta['code_hash']) == 64 and meta['capture_mode']=='BASIC'
        assert meta['effective_config']['top_k']==5 and meta['effective_config']['chat_model']=='fake-model'
        assert meta['effective_config']['embedding_model']=='NOT_APPLICABLE' and meta['effective_config']['max_tokens']==4096
        assert meta['execution_status']=='RETURNED' and len(meta['index_fingerprint'])==64
        assert meta['index_fingerprint_scope']=='observed_file_chunks_only'
        child_input=json.loads(child.attributes['langfuse.observation.input'])
        assert child_input['observation_id'] == format(child.context.span_id, '016x')
        assert child_input['parent_observation_id'] == format(root.context.span_id, '016x')
    finally:
        client.shutdown()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX ownership and mode checks')
def test_posix_cli_reads_valid_and_rejects_corrupt_expired_and_symlink(tmp_path):
    import subprocess
    import sys
    import time
    root = tmp_path/'snapshots'
    root.mkdir(mode=0o700)
    raw = b'evidence'
    digest = hashlib.sha256(raw).hexdigest()
    target = root/(digest + '.txt')
    target.write_bytes(raw)
    target.chmod(0o600)
    script = Path(__file__).resolve().parents[2]/'scripts'/'read_e11_snapshot.py'
    def run(): return subprocess.run([sys.executable, str(script), digest, '--root', str(root)], capture_output=True)
    assert run().stdout == raw
    target.write_bytes(b'corrupt')
    assert run().returncode == 1
    target.write_bytes(raw)
    os.utime(target, (time.time()-8*86400, time.time()-8*86400))
    assert run().returncode == 1
    target.unlink()
    other = tmp_path/'other'
    other.write_bytes(raw)
    target.symlink_to(other)
    assert run().returncode == 1


@pytest.mark.skipif(os.name == 'posix', reason='checks unverified non-POSIX ACL refusal')
def test_windows_cli_fails_closed_for_unverified_acl(tmp_path):
    import subprocess
    import sys
    raw = b'evidence'
    digest = hashlib.sha256(raw).hexdigest()
    (tmp_path/(digest + '.txt')).write_bytes(raw)
    script = Path(__file__).resolve().parents[2]/'scripts'/'read_e11_snapshot.py'
    result = subprocess.run([sys.executable, str(script), digest, '--root', str(tmp_path)], capture_output=True)
    assert result.returncode == 1 and not result.stdout
    assert b'PermissionError' in result.stderr


def _root_output(client):
    return next(data for name, data in reversed(client.updates) if name == 'qa.request' and 'output' in data)


@pytest.mark.parametrize('items,expected,empty', [
    ([('answer_chunk', {'content':'partial'}), ('done', {'generation_status':'TRUNCATED'})], 'TRUNCATED', False),
    ([('done', {'generation_status':'EMPTY'})], 'RETURNED', True),
    ([('error', {'status':'FAILED'})], 'FAILED', None),
    ([], 'UNKNOWN', None),
])
def test_root_terminal_distinguishes_empty_truncated_error_and_missing(monkeypatch, tmp_path, items, expected, empty):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        for item in items:
            yield item
    async def run(): return [item async for item in stream([{'role':'user','content':'q'}])]
    assert asyncio.run(run()) == items
    metadata = _root_output(client)['metadata']
    assert metadata['execution_status'] == expected
    assert metadata['answer_empty'] is empty
    if empty is None:
        assert metadata['final_answer_sha256'] is None


def test_root_generator_close_records_cancelled(monkeypatch, tmp_path):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        yield ('answer_chunk', {'content':'partial'})
        yield ('done', {})
    async def run():
        gen = stream([{'role':'user','content':'q'}])
        assert (await anext(gen))[0] == 'answer_chunk'
        await gen.aclose()
    asyncio.run(run())
    assert _root_output(client)['metadata']['execution_status'] == 'CANCELLED'


def test_root_asyncio_cancel_records_cancelled(monkeypatch, tmp_path):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        yield ('answer_chunk', {'content':'partial'})
        await asyncio.Event().wait()
    async def run():
        gen = stream([{'role':'user','content':'q'}])
        assert (await anext(gen))[0] == 'answer_chunk'
        pending = asyncio.create_task(anext(gen))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
    asyncio.run(run())
    assert _root_output(client)['metadata']['execution_status'] == 'CANCELLED'


def test_root_tracks_provider_raw_and_transformed_sse_hashes(monkeypatch, tmp_path):
    client = FakeClient()
    setup_observation(monkeypatch, tmp_path, client)
    @obs.observe('qa.request')
    async def stream(messages):
        obs.capture('raw response', content_role='provider_response')
        yield ('answer_chunk', {'content':'raw response plus disclosure'})
        yield ('done', {'generation_status':'COMPLETED'})
    async def run(): return [x async for x in stream([{'role':'user','content':'q'}])]
    asyncio.run(run())
    meta = _root_output(client)['metadata']
    assert meta['original_answer_sha256'] == hashlib.sha256(b'raw response').hexdigest()
    assert meta['final_answer_sha256'] == hashlib.sha256(b'raw response plus disclosure').hexdigest()
    assert meta['transform_reason'] == 'sse_assembly'
