"""Worker contracts and real HTTP/SSE+BM25 wiring, with offline provider only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / 'scripts/rsi_quality_worker.py'
spec = importlib.util.spec_from_file_location('quality_worker_contract', PATH)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def job():
    return {'schema_version': worker.SCHEMA, 'model': worker.MODEL,
        'max_model_requests': 2, 'repeat': 1,
        'cases': [{'id': 'case-one', 'question': '根据资料回答：Alpha protocol 有哪些步骤？',
                   'preferences': {'answer_style': 'concise'}}],
        'sources': [{'id': 'source-one', 'name': 'alpha.txt'}],
        'chunks': [{'id': 'chunk-one', 'source_id': 'source-one',
                    'content': 'Alpha protocol has two steps: first validate input, then save output.'}],
        'artifact': {'prompt_addendum': 'Keep source numbers beside claims.',
                     'memory_addendum': 'Prefer one short paragraph.',
                     'workflow': {'mode': 'chat', 'use_rewrite': False}}}


@pytest.mark.parametrize('field', ['gold', 'answer_points', 'expected_chunk_ids', 'api_key'])
def test_rejects_private_or_gold_input(field):
    candidate = job()
    candidate[field] = 'hidden'
    with pytest.raises(ValueError, match='unexpected fields'):
        worker.validate(candidate)


def test_preflight_has_zero_sends_and_rejects_ambiguous_turns():
    candidate = job()
    assert worker.preflight(candidate)['model_calls'] == 0
    candidate['cases'][0]['history'] = [{'role': 'user', 'content': 'Earlier question'}]
    with pytest.raises(ValueError, match='complete user/assistant turns'):
        worker.validate(candidate)


def test_refuses_duplicate_records_or_fake_document_scope():
    candidate = job()
    candidate.pop('repeat')
    candidate['records'] = [{'case_id': 'case-one', 'repeat_id': 1}] * 2
    with pytest.raises(ValueError, match='Duplicate record'):
        worker.validate(candidate)
    candidate = job()
    candidate['cases'][0]['file_ids'] = ['source-one']
    with pytest.raises(ValueError, match='unexpected fields'):
        worker.validate(candidate)


def test_prompt_hook_default_and_explicit_addition():
    from app.services.grounded_generation import build_grounded_prompt
    baseline = build_grounded_prompt('Evidence')
    assert baseline.endswith('\n\n资料：\nEvidence')
    assert build_grounded_prompt('Evidence', addendum='Additional style') == baseline + '\n\nAdditional style'


def test_response_usage_is_never_invented():
    parsed = worker.RecordingProxy.response_metadata('{"choices":[{"message":{"content":""},"finish_reason":"stop"}]}', 'application/json')
    assert parsed == {'usage': None, 'finish_reason': 'stop', 'provider_model': None, 'generated_content': ''}
    stream = 'data: {"choices":[{"delta":{"content":"42"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":9}}\n\ndata: [DONE]\n'
    parsed = worker.RecordingProxy.response_metadata(stream, 'text/event-stream')
    assert parsed['generated_content'] == '42'
    assert parsed['usage'] == {'total_tokens': 9}
    assert parsed['finish_reason'] == 'stop'


def test_preflight_cli_is_offline_and_output_exclusive(tmp_path):
    src, out = tmp_path / 'job.json', tmp_path / 'out'
    src.write_text(json.dumps(job()), encoding='utf-8')
    cmd = [sys.executable, str(PATH), '--input', str(src), '--output', str(out)]
    first = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=20)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads((out / 'preflight.json').read_text())['model_calls'] == 0
    assert sorted(p.name for p in out.iterdir()) == ['preflight.json']
    second = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=20)
    assert second.returncode != 0
    assert 'FileExistsError' in second.stderr


def run_offline_product(tmp_path, candidate, behavior='normal', extra_env=None):
    src, out = tmp_path / 'job.json', tmp_path / 'out'
    src.write_text(json.dumps(candidate, ensure_ascii=False), encoding='utf-8')
    # Only the external provider HTTP response is simulated. Actual factory,
    # OpenAI loopback HTTP serialization, BM25, ChatService and API SSE stay real.
    harness = tmp_path / 'offline_provider.py'
    harness.write_text('''
import importlib.util, json, sys, httpx
from pathlib import Path
spec = importlib.util.spec_from_file_location('worker', sys.argv[1])
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
behavior = sys.argv[4]
original = httpx.Client.post
provider_counts = {'llm': 0, 'embedding': 0}
def provider(self, url, *args, **kwargs):
    if str(url).startswith(w.PROVIDER):
        provider_counts['llm'] += 1
        payload = json.loads(kwargs['content'])
        assert payload['model'] == w.MODEL
        assert payload['enable_thinking'] is False
        assert payload['max_tokens'] == 4096
        if (behavior == 'unknown' or behavior == 'transient_unknown' and provider_counts['llm'] == 1
                or behavior == 'first_record_exhausted' and provider_counts['llm'] <= 2):
            raise httpx.ReadTimeout('offline lost response')
        messages = payload['messages']
        assert any('Alpha protocol' in m.get('content', '') for m in messages)
        text = '' if behavior == 'empty' else 'First validate input, then save output [1].'
        message, finish = {'role': 'assistant', 'content': text}, 'stop'
        joined = json.dumps(messages, ensure_ascii=False)
        if '文档相关性评分员' in joined:
            message['content'] = '0.9'
        elif payload.get('tools') and not any(m.get('role') == 'tool' for m in messages):
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'offline-call-1', 'type': 'function',
                'function': {'name': 'search_knowledge_base', 'arguments': json.dumps({'query': 'Alpha protocol'})}}]}
            finish = 'tool_calls'
        if behavior == 'incomplete_once' and provider_counts['llm'] == 1:
            finish = None
        body = {'model': w.MODEL, 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 31, 'completion_tokens': 11, 'total_tokens': 42}}
        return httpx.Response(200, json=body, headers={'authorization': 'must-not-save', 'set-cookie': 'secret-cookie'}, request=httpx.Request('POST', url))
    return original(self, url, *args, **kwargs)
httpx.Client.post = provider
original_async = httpx.AsyncClient.post
async def embedding_provider(self, url, *args, **kwargs):
    if str(url) == w.PROVIDER + '/embeddings':
        provider_counts['embedding'] += 1
        payload = kwargs['json']
        assert payload['model'] == 'BAAI/bge-m3'
        if (behavior == 'embedding_unknown'
                or behavior == 'embedding_unknown_once' and provider_counts['embedding'] == 1
                or behavior == 'embedding_query_unknown_once' and provider_counts['embedding'] == 2):
            raise httpx.ReadTimeout('offline embedding outcome unknown')
        return httpx.Response(200, json={'model': 'BAAI/bge-m3',
            'data': [{'index': i, 'embedding': [0.1, 0.2, 0.3]} for i, text in enumerate(payload['input'])],
            'usage': {'total_tokens': len(payload['input']) * 7}}, request=httpx.Request('POST', url))
    return await original_async(self, url, *args, **kwargs)
httpx.AsyncClient.post = embedding_provider
raise SystemExit(w.main(['--input', sys.argv[2], '--output', sys.argv[3], '--live']))
''', encoding='utf-8')
    env = dict(os.environ, SILICONFLOW_API_KEY='offline-test-key-never-artifact', PYTHONIOENCODING='utf-8')
    if extra_env:
        env.update(extra_env)
    result = subprocess.run([sys.executable, str(harness), str(PATH), str(src), str(out), behavior],
                            capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
    return result, out


def test_product_http_sse_bm25_prompt_and_memory_are_real(tmp_path):
    result, out = run_offline_product(tmp_path, job())
    assert result.returncode == 0, result.stdout + result.stderr + (out / 'product.log').read_text(encoding='utf-8')
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    row = report['results'][0]
    assert row['case_id'] == 'case-one' and row['answer'].endswith('[1].')
    assert row['citations'][0]['chunk_id'] == 'chunk-one'
    assert row['provider_call_count'] == 1
    assert row['elapsed_seconds'] >= row['provider_calls'][0]['elapsed_seconds']
    assert row['final_request']['context_bytes'] >= row['final_request']['context_chars'] > 0
    assert row['final_context_status'] == 'VERIFIED_IN_ACTUAL_REQUEST'
    assert row['final_context_chars'] == len(row['actual_grounded_evidence'])
    assert row['final_prompt_messages_chars'] > row['final_context_chars']
    assert row['final_request']['usage']['total_tokens'] == 42
    assert row['usage']['total_tokens'] == 42
    assert row['preference_resolution']['memory_addendum_applied'] is True
    request = json.loads((out / row['final_request']['request_file']).read_text(encoding='utf-8'))
    system = request['actual_request']['messages'][0]['content']
    assert 'Keep source numbers beside claims.' in system
    assert 'Prefer one short paragraph.' in system
    assert row['preference_resolution']['values']['answer_style'] == 'concise'
    all_artifacts = '\n'.join(p.read_text(encoding='utf-8', errors='ignore') for p in out.rglob('*') if p.is_file())
    assert 'offline-test-key-never-artifact' not in all_artifacts
    assert 'must-not-save' not in all_artifacts and 'secret-cookie' not in all_artifacts
    assert not (out / 'chroma-not-created').exists()
    assert not list(out.rglob('*.db'))


def test_empty_generated_answer_is_recorded_without_content_verdict(tmp_path):
    result, out = run_offline_product(tmp_path, job(), 'empty')
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads((out / 'results.json').read_text(encoding='utf-8'))['results'][0]
    assert row['done']['generation_status'] == 'EMPTY'
    assert row['final_request']['generated_content'] == ''
    assert row['provider_call_count'] == 1


def test_unknown_stops_without_reissuing_and_keeps_denominator(tmp_path):
    candidate = job()
    candidate['repeat'] = 2
    result, out = run_offline_product(tmp_path, candidate, 'unknown')
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert len(report['results']) == 2
    assert report['results'][0]['status'] == 'UNKNOWN'
    assert report['results'][1]['status'] == 'NOT RUN'
    assert report['model_requests'] == 1


def test_real_agent_flow_uses_bm25_tools_grading_and_final_memory_policy(tmp_path):
    candidate = job()
    candidate['max_model_requests'] = 5
    candidate['artifact']['workflow']['mode'] = 'agent'
    candidate['artifact']['prompt_addendum'] = ''
    result, out = run_offline_product(tmp_path, candidate)
    assert result.returncode == 0, result.stdout + result.stderr + (out / 'product.log').read_text(encoding='utf-8')
    row = json.loads((out / 'results.json').read_text(encoding='utf-8'))['results'][0]
    assert row['provider_call_count'] == 3
    assert row['usage']['total_tokens'] == 126
    assert row['final_request']['generated_content'].endswith('[1].')
    assert row['final_context_chars'] is None and row['final_context_status'] == 'UNKNOWN'
    assert row['final_prompt_messages_chars'] > 0
    assert row['citations'][0]['chunk_id'] == 'chunk-one'
    final = json.loads((out / row['final_request']['request_file']).read_text(encoding='utf-8'))['actual_request']
    assert any(m['role'] == 'tool' and 'Alpha protocol' in m['content'] for m in final['messages'])
    assert 'Prefer one short paragraph.' in final['messages'][0]['content']
    first = json.loads((out / 'calls/0001-request.json').read_text(encoding='utf-8'))['actual_request']
    assert 'Prefer one short paragraph.' not in first['messages'][0]['content']


def hybrid_job():
    candidate = job()
    candidate['retrieval'] = {'search_mode': 'hybrid', 'vector_weight': 0.7, 'bm25_weight': 0.3}
    candidate['max_embedding_requests'] = 5
    return candidate


def hybrid_env(cache):
    return dict(zip(worker.EMBEDDING_ENV, [worker.PROVIDER, 'BAAI/bge-m3', 'offline-embedding-secret']),
                RSI_QUALITY_EMBEDDING_CACHE_DIR=str(cache))


@pytest.mark.parametrize('config', [
    {'search_mode': 'vector'}, {'search_mode': 'hybrid', 'vector_weight': 0},
    {'search_mode': 'hybrid', 'bm25_weight': float('nan')},
    {'search_mode': 'hybrid', 'embedding_api_key': 'forbidden'},
    {'rerank_enabled': True}, {'top_k': True}])
def test_retrieval_rejects_ambiguous_or_unapproved_config(config):
    candidate = hybrid_job()
    candidate['retrieval'] = config
    with pytest.raises(ValueError):
        worker.validate(candidate)


def test_hybrid_preflight_is_zero_call_and_credential_free(monkeypatch):
    candidate = hybrid_job()
    for name in worker.EMBEDDING_ENV:
        monkeypatch.delenv(name, raising=False)
    check = worker.preflight(candidate)
    assert check['model_calls'] == check['embedding']['model_calls'] == 0
    assert check['fixed_search']['search_mode'] == 'hybrid'
    assert check['embedding']['allowed_identities'] == [{'base_url': worker.PROVIDER, 'model': 'BAAI/bge-m3'}]
    with pytest.raises(ValueError, match='process-only'):
        worker.embedding_environment(candidate)
    monkeypatch.setenv(worker.EMBEDDING_ENV[0], worker.PROVIDER)
    monkeypatch.setenv(worker.EMBEDDING_ENV[1], 'Pro/BAAI/bge-m3')
    monkeypatch.setenv(worker.EMBEDDING_ENV[2], 'secret')
    with pytest.raises(ValueError, match='allowlisted'):
        worker.embedding_environment(candidate)
    candidate.pop('max_embedding_requests')
    with pytest.raises(ValueError, match='bounded max_embedding_requests'):
        worker.validate(candidate)


def test_hybrid_real_product_vectors_runtime_and_provider_cache(tmp_path):
    # Only provider HTTP is offline simulated. Chroma, embedding adapter, model
    # resolver, weighted RRF, runtime SQLite settings and SSE are real product code.
    cache = tmp_path / 'trusted-cache'
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    result, out = run_offline_product(first, hybrid_job(), extra_env=hybrid_env(cache))
    assert result.returncode == 0, result.stdout + result.stderr + (out / 'product.log').read_text(encoding='utf-8')
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    row = report['results'][0]
    assert row['hybrid_execution_status'] == 'VERIFIED_REAL_PRODUCT_HYBRID'
    assert row['retrieval_calls'][0]['metrics']['vector_count'] == 1
    assert row['retrieval_calls'][0]['metrics']['bm25_count'] == 1
    assert row['retrieval_calls'][0]['kb_ids'] == ['rsi-quality-kb']
    assert report['model_roles']['embedding']['binding'] == 'sys_embedding'
    assert report['embedding_requests'] == 2
    assert report['embedding_usage_tokens'] == 14
    assert report['embedding_cache_hits'] == 0
    assert report['embedding_status'] == 'RETURNED'
    assert (out / 'chroma').exists()
    assert row['citations'][0]['chunk_id'] == 'chunk-one'
    assert row['final_context_status'] == 'VERIFIED_IN_ACTUAL_REQUEST'
    assert row['final_request']['provider_model'] == worker.MODEL
    cache_rows = [json.loads(p.read_text(encoding='utf-8')) for p in cache.glob('*.json')]
    assert len(cache_rows) == 2 and all(r['status'] == 'RETURNED' for r in cache_rows)
    result2, out2 = run_offline_product(second, hybrid_job(), extra_env=hybrid_env(cache))
    assert result2.returncode == 0, result2.stdout + result2.stderr + (out2 / 'product.log').read_text(encoding='utf-8')
    reused = json.loads((out2 / 'results.json').read_text(encoding='utf-8'))
    assert reused['embedding_requests'] == reused['embedding_usage_tokens'] == 0
    assert reused['embedding_cache_hits'] == 2
    assert reused['results'][0]['embedding_calls'][0]['request_status'] == 'CACHE_HIT'
    assert reused['results'][0]['retrieval_calls'][0]['metrics']['vector_count'] == 1
    receipt = json.loads((out2 / 'calls/embedding-0001-request.json').read_text(encoding='utf-8'))
    assert receipt['actual_request'] is None
    for directory in (out, out2, cache):
        assert 'offline-embedding-secret' not in ''.join(p.read_text(encoding='utf-8')
            for p in directory.rglob('*.json'))


def test_unknown_embedding_seed_is_not_cached_or_retried(tmp_path):
    cache = tmp_path / 'cache'
    result, out = run_offline_product(tmp_path, hybrid_job(), 'embedding_unknown', hybrid_env(cache))
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert report['status'] == 'INCOMPLETE'
    assert report['embedding_status'] == 'UNKNOWN'
    assert report['embedding_requests'] == 1 and report['model_requests'] == 0
    assert len(report['results']) == 1 and report['results'][0]['status'] == 'NOT RUN'
    assert not list(cache.glob('*.json'))


def test_hybrid_respects_case_scope_without_dropping_other_seeded_chunks(tmp_path):
    candidate = hybrid_job()
    candidate['sources'][0]['kb_id'] = 'scope-two'
    candidate['sources'].append({'id': 'source-outside', 'name': 'outside.txt', 'kb_id': 'scope-one'})
    candidate['chunks'].append({'id': 'chunk-outside', 'source_id': 'source-outside',
        'content': 'Alpha protocol unrelated outside-scope content must never be delivered.'})
    candidate['cases'][0]['kb_ids'] = ['scope-two']
    result, out = run_offline_product(tmp_path, candidate, extra_env=hybrid_env(tmp_path / 'cache'))
    assert result.returncode == 0, result.stdout + result.stderr + (out / 'product.log').read_text(encoding='utf-8')
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    row = report['results'][0]
    assert report['resources']['vector_seed_count'] == 2
    assert report['embedding_requests'] == 3  # Both KB seeds retained, one scoped query.
    assert row['request_payload']['kb_ids'] == ['scope-two']
    assert row['retrieval_calls'][0]['kb_ids'] == ['scope-two']
    assert row['retrieval_calls'][0]['candidate_ids'] == ['chunk-one']
    assert 'chunk-outside' not in row['actual_grounded_evidence']
    assert 'outside-scope content' not in row['actual_grounded_evidence']


def test_embedding_hard_cap_prevents_further_provider_and_answer_sends(tmp_path):
    candidate = hybrid_job()
    candidate['max_embedding_requests'] = 1
    result, out = run_offline_product(tmp_path, candidate, extra_env=hybrid_env(tmp_path / 'cache'))
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert report['status'] == 'INCOMPLETE'
    assert report['embedding_requests'] == 1
    assert report['model_requests'] == 0
    assert report['results'][0]['hybrid_execution_status'] == 'INCOMPLETE_OR_DEGRADED'


@pytest.mark.parametrize('behavior,expected_http,expected_status', [
    ('success', 200, 'RETURNED'), ('http_error', 503, 'FAILED'),
    ('transport_unknown', 502, 'UNKNOWN')])
def test_proxy_finalizes_metadata_before_forwarding(tmp_path, monkeypatch,
        behavior, expected_http, expected_status):
    import http.client
    import time
    import httpx

    proxy = worker.RecordingProxy(tmp_path, 'offline-key', 1)
    proxy.context = {'case_id': 'ordering-case', 'repeat_id': 1}
    proxy.deadline = time.monotonic() + 30
    server_class = worker.ThreadingHTTPServer
    observed = []

    def server(address, handler):
        original_reply = handler.reply
        def checked_reply(self, status, body, *args):
            # Inspect at the handoff boundary, before a single response byte is
            # sent. This fails deterministically if finalization is in finally
            # after reply, irrespective of thread or network scheduling.
            disk = json.loads((tmp_path / 'calls/0001-meta.json').read_text(encoding='utf-8'))
            observed.append({'memory': dict(proxy.calls[0]), 'disk': disk})
            return original_reply(self, status, body, *args)
        handler.reply = checked_reply
        return server_class(address, handler)

    def provider(client, url, *args, **kwargs):
        assert url == worker.PROVIDER + '/chat/completions'
        if behavior == 'transport_unknown':
            raise httpx.ReadTimeout('offline response lost')
        return httpx.Response(503 if behavior == 'http_error' else 200,
            json={'model': worker.MODEL, 'choices': [{'finish_reason': 'stop',
                'message': {'content': 'answer'}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(worker, 'ThreadingHTTPServer', server)
    monkeypatch.setattr(httpx.Client, 'post', provider)
    port = proxy.start()
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('POST', '/v1/chat/completions',
            body=json.dumps({'messages': [{'role': 'user', 'content': 'public question'}]}),
            headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == expected_http
        response.read()
    finally:
        connection.close()
        proxy.stop()
    assert len(observed) == 1
    before_forwarding = observed[0]
    assert before_forwarding['memory'] == before_forwarding['disk']
    assert before_forwarding['memory']['request_status'] == expected_status
    assert before_forwarding['memory']['elapsed_seconds'] >= 0
    if expected_status == 'UNKNOWN':
        assert proxy.stopped is True


@pytest.mark.parametrize('behavior', ['transient_unknown', 'incomplete_once'])
def test_retry_once_recovers_only_failed_record_and_keeps_initial_receipt(tmp_path, behavior):
    candidate = job()
    candidate.update(retry_once=True, max_model_requests=10)
    result, out = run_offline_product(tmp_path, candidate, behavior)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    row = report['results'][0]
    assert report['model_requests'] == row['provider_call_count'] == 2
    assert row['attempt_count'] == 2
    assert row['initial_status'] == 'UNKNOWN' and row['retry_status'] == 'RETURNED'
    assert [a['attempt_id'] for a in row['attempts']] == ['record-0001-attempt-1', 'record-0001-attempt-2']
    initial = json.loads((out / 'record-0001-attempt-1.json').read_text(encoding='utf-8'))
    assert initial['status'] == 'UNKNOWN'
    assert initial['provider_calls'][0]['request_status'] == 'UNKNOWN'
    assert row['provider_calls'][0]['request_file'] != row['provider_calls'][1]['request_file']
    assert row['provider_calls'][0]['attempt_number'] == 1
    assert row['provider_calls'][1]['attempt_number'] == 2
    if behavior == 'transient_unknown':
        assert row['usage'] is None and row['known_usage']['total_tokens'] == 42
        assert row['unknown_usage_call_count'] == 1
    else:
        assert row['usage']['total_tokens'] == 84
        assert row['unknown_usage_call_count'] == 0


def test_retry_once_exhausts_two_attempts_then_continues_independent_record(tmp_path):
    candidate = job()
    candidate.update(retry_once=True, max_model_requests=10, repeat=2)
    result, out = run_offline_product(tmp_path, candidate, 'first_record_exhausted')
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert report['model_requests'] == 3 and len(report['results']) == 2
    first, second = report['results']
    assert first['status'] == 'UNKNOWN' and first['attempt_count'] == 2
    assert second['status'] == 'RETURNED' and second['attempt_count'] == 1
    assert report['retry_summary'] == {'retried_records': 1, 'record_attempts': 3, 'exhausted_records': 1}
    assert report['model_known_usage']['total_tokens'] == 42
    assert report['model_unknown_usage_call_count'] == 2


@pytest.mark.parametrize('behavior', ['normal', 'empty'])
def test_retry_once_does_not_rerun_completed_or_content_empty_answer(tmp_path, behavior):
    candidate = job()
    candidate.update(retry_once=True, max_model_requests=10)
    result, out = run_offline_product(tmp_path, candidate, behavior)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert report['model_requests'] == 1 and report['results'][0]['attempt_count'] == 1


@pytest.mark.parametrize('behavior,expected_model_requests,expected_attempts', [
    ('embedding_unknown_once', 1, 1), ('embedding_query_unknown_once', 1, 2),
    ('embedding_unknown', 0, None)])
def test_retry_once_embedding_seed_or_record_has_single_retry_owner(tmp_path,
        behavior, expected_model_requests, expected_attempts):
    candidate = hybrid_job()
    candidate.update(retry_once=True, max_model_requests=10)
    result, out = run_offline_product(tmp_path, candidate, behavior, hybrid_env(tmp_path / 'cache'))
    report = json.loads((out / 'results.json').read_text(encoding='utf-8'))
    assert result.returncode == (2 if expected_attempts is None else 0), result.stdout + result.stderr
    assert report['model_requests'] == expected_model_requests
    assert report['embedding_requests'] == (2 if expected_attempts is None else 3)
    ledger = json.loads((out / 'embedding-calls.json').read_text(encoding='utf-8'))
    assert ledger[0]['record']['phase'] == 'embedding_seed'
    if behavior.startswith('embedding_unknown'):
        assert ledger[0]['request_status'] == 'UNKNOWN'
        assert ledger[1]['record']['attempt_number'] == 2
    if expected_attempts is not None:
        assert report['results'][0]['attempt_count'] == expected_attempts
        assert report['embedding_usage_tokens'] is None
        assert report['embedding_known_usage_tokens'] == 14
    else:
        assert report['results'][0]['status'] == 'NOT RUN'
        assert not list((tmp_path / 'cache').glob('*.json'))
    assert (out / 'calls/embedding-0001-meta.json').is_file()
    assert (out / 'calls/embedding-0002-meta.json').is_file()


def test_retry_opt_in_is_strict_and_preflight_stays_offline():
    candidate = job()
    candidate['retry_once'] = 1
    with pytest.raises(ValueError, match='boolean'):
        worker.validate(candidate)
    candidate['retry_once'] = True
    check = worker.preflight(candidate)
    assert check['model_calls'] == 0
    assert check['retry_policy']['max_attempts'] == 2
    assert check['retry_policy']['attempt_deadline_seconds'] == 180
    assert check['retry_policy']['provider_hidden_retries'] == 0
