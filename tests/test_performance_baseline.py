"""Real loopback HTTP contract tests; no application/provider/model required."""
import importlib.util
import json
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('performance_baseline', ROOT / 'scripts/run_performance_baseline.py')
baseline = importlib.util.module_from_spec(spec)
import sys
sys.path.insert(0, str(ROOT))
spec.loader.exec_module(baseline)


class Stub(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = json.dumps({'data': {'revision': 7, 'maxChatHistoryRounds': 5}}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((body, dict(self.headers)))
        question = body['messages'][-1]['content']
        self.send_response(503 if question == 'http-failed' else 200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()

        def emit(kind, data):
            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode())
            self.wfile.flush()

        if question == 'http-failed':
            self.wfile.write(b'provider unavailable')
        elif question == 'empty':
            emit('done', {})
        elif question == 'malformed':
            self.wfile.write(b'event: answer_chunk\ndata: {garbled}\n\n')
            emit('done', {})
        elif question == 'unfinished-frame':
            self.wfile.write(b'event: answer_chunk\ndata: {"content":"never framed"}')
        elif question == 'sse-error':
            emit('error', {'status': 'UNKNOWN', 'retryable': False})
        else:
            emit('answer_chunk', {'content': '已保存长期偏好：回答风格为简洁。\n\n'})
            emit('progress', {'content': '正在检索'})
            emit('answer_chunk', {'content': 'typing', 'kind': 'progress'})
            time.sleep(0.07)
            emit('answer_chunk', {'content': '实际正文'})
            time.sleep(0.07)
            if question != 'truncated':
                emit('done', {'usage': {'total_tokens': 999999}})
            time.sleep(0.07)
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture
def api():
    Stub.requests = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:' + str(server.server_port)
    server.shutdown()
    server.server_close()
    thread.join()


def identity():
    return {'run_id': 'run-1', 'attempt_id': 'attempt-1', 'qid': 'q1', 'dataset_version': 'stub-v1'}


def measure(api, tmp_path, question):
    return baseline.measure_one(api, {'messages': [{'role': 'user', 'content': question}], 'kb_ids': ['kb-stub']},
                                identity(), tmp_path / question, 3)


def test_incremental_first_actual_body_terminal_eof_and_release(api, tmp_path):
    receipt = measure(api, tmp_path, 'normal')
    assert receipt['terminal_state'] == 'SSE_DONE'
    assert receipt['answer'] == '实际正文'
    assert receipt['terminal_ms'] - receipt['first_body_ms'] >= 45
    assert receipt['eof_ms'] - receipt['terminal_ms'] >= 45
    assert receipt['client_release_ms'] >= receipt['eof_ms']
    assert receipt['server_request_cleanup_ms'] is None
    assert receipt['request_started_monotonic_seconds'] > 0
    assert receipt['events'][0]['received_ms'] < receipt['first_body_ms']
    assert (tmp_path / 'normal' / 'response.sse').read_bytes().startswith(b'event: answer_chunk')
    assert Stub.requests[0][1]['X-RAG-Attempt-ID'] == 'attempt-1'


@pytest.mark.parametrize(('question', 'state'), [('empty', 'EMPTY_ANSWER'), ('truncated', 'SSE_INCOMPLETE'),
                                               ('malformed', 'MALFORMED_SSE'), ('unfinished-frame', 'SSE_INCOMPLETE'),
                                               ('http-failed', 'HTTP_ERROR'), ('sse-error', 'SSE_ERROR')])
def test_failed_empty_and_truncated_receipts_preserved(api, tmp_path, question, state):
    receipt = measure(api, tmp_path, question)
    assert receipt['terminal_state'] == state
    assert receipt['client_release_ms'] > 0
    assert (tmp_path / question / 'request.json').exists()
    assert (tmp_path / question / 'response.sse').exists()
    assert receipt['correctness']['status'] == 'UNKNOWN'
    if question == 'truncated':
        assert receipt['answer'] == '实际正文'
        assert receipt['terminal_ms'] is None


def provider(event_id, role, usage, attempt='attempt-1'):
    return {'attempt_id': attempt, 'trace_id': 'trace-a', 'event_id': event_id,
            'stage': 'llm.response', 'event': 'stage',
            'metadata': {'role': role, 'usage': usage, 'usage_status': 'PROVIDER_REPORTED' if usage else 'UNKNOWN'}}


def test_whole_trace_provider_aggregation_and_unknown_not_done_usage(api, tmp_path):
    receipt = measure(api, tmp_path, 'normal')
    rows = [provider('tool', 'agent_tool_choice', {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}),
            provider('grade', 'grading', {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}),
            provider('rewrite', 'rewrite', None),
            provider('other', 'generation', {'total_tokens': 100}, attempt='other-attempt')]
    rows.append(rows[0])
    baseline.aggregate_telemetry(receipt, rows)
    assert receipt['provider']['observed_response_count'] == 3
    assert receipt['provider']['observed_token_lower_bound']['total_tokens'] is None
    assert receipt['provider']['legacy_usage_status'] == 'UNKNOWN_UNCORRELATED'
    assert receipt['provider']['tokens'] is None
    assert receipt['provider']['call_count'] is None
    assert receipt['provider']['missing_usage_records'] == 0
    assert receipt['provider']['done_usage_used'] is False
    assert receipt['done']['usage']['total_tokens'] == 999999


def test_complete_marker_and_server_cleanup_explicit(api, tmp_path):
    receipt = measure(api, tmp_path, 'normal')
    rows = [provider('generate', 'generation', {'prompt_tokens': 5, 'completion_tokens': 4, 'total_tokens': 9}),
            {'attempt_id': 'attempt-1', 'stage': 'qa.performance',
             'metadata': {'provider_telemetry_complete': True, 'request_cleanup_ms': 450,
                          'actual_candidate_count': 8, 'final_evidence_count': 3}}]
    baseline.aggregate_telemetry(receipt, rows)
    assert receipt['provider']['call_count'] is None
    assert receipt['provider']['tokens'] is None
    assert receipt['provider']['legacy_usage_status'] == 'UNKNOWN_UNCORRELATED'
    assert receipt['server_request_cleanup_ms'] == 450
    assert receipt['actual_candidate_count'] == 8
    assert receipt['retry_count'] is None
    assert 'retry_count' in receipt['unknowns']


def test_pipeline_scope_does_not_claim_http_cleanup_and_all_stage_values_retained(api, tmp_path):
    receipt = measure(api, tmp_path, 'normal')
    rows = [{'attempt_id': 'attempt-1', 'stage': 'qa.performance', 'metadata': {
                'server_pipeline_ms': 230, 'post_terminal_cleanup_ms': 72,
                'timing_scope': 'chat_generator_including_flush_excluding_outer_span_close'}},
            {'attempt_id': 'attempt-1', 'stage': 'performance.stage', 'metadata': {
                'stage_name': 'chroma', 'duration_ms': 12}},
            {'attempt_id': 'attempt-1', 'stage': 'performance.stage', 'metadata': {
                'stage_name': 'chroma', 'duration_ms': 15}}]
    baseline.aggregate_telemetry(receipt, rows)
    assert receipt['server_request_cleanup_ms'] is None
    assert receipt['server_pipeline_ms'] == 230
    assert receipt['post_terminal_cleanup_ms'] == 72
    assert receipt['stage_timings_ms']['chroma'] == [12, 15]
    assert 'excluding_outer_span_close' in receipt['server_timing_scope']


def test_provider_dispatches_include_embedding_rerank_and_failed_calls(api, tmp_path):
    receipt = measure(api, tmp_path, 'normal')
    rows = []
    for stage in ('embedding', 'rerank', 'quality', 'generation'):
        rows.append({'attempt_id': 'attempt-1', 'event_id': stage, 'stage': 'provider.request_started',
                     'event': 'stage', 'metadata': {'stage': stage, 'local_request_id': stage}})
    rows += [{'attempt_id': 'attempt-1', 'stage': 'rerank.usage', 'event': 'stage',
              'metadata': {'usage': {'total_tokens': 30}}},
             provider('quality-response', 'quality', {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7})]
    rows.append(rows[0])
    baseline.aggregate_telemetry(receipt, rows)
    assert receipt['provider']['observed_request_count'] == 4
    assert receipt['provider']['observed_call_count_lower_bound'] == 4
    assert receipt['provider']['observed_response_count'] == 2
    assert receipt['provider']['observed_token_lower_bound']['total_tokens'] is None
    assert receipt['provider']['legacy_usage_status'] == 'UNKNOWN_UNCORRELATED'
    assert receipt['provider']['call_count'] is None
    assert receipt['provider']['tokens'] is None
    # All four dispatches lack correlated attempt usage.
    assert receipt['provider']['missing_usage_records'] == 4


@pytest.mark.parametrize('stage_name', [{'sha256': 'a' * 64, 'chars': 19}, None, ['unexpected'], ''])
def test_hashed_or_unknown_stage_name_preserves_raw_and_warns(stage_name):
    receipt = {'attempt_id': 'attempt-1', 'unknowns': ['server_request_cleanup_ms'], 'terminal_state': 'SSE_DONE'}
    row = {'attempt_id': 'attempt-1', 'stage': 'performance.stage', 'event_id': 'stage-1',
           'metadata': {'stage_name': stage_name, 'duration_ms': 12.5}}
    matched = baseline.aggregate_telemetry(receipt, [row])
    assert receipt['stage_timings_ms'] == {'UNKNOWN': [12.5]}
    assert matched == [row]
    assert receipt['telemetry_warnings'] == [{'code': 'UNKNOWN_STAGE_NAME', 'raw_stage_name': stage_name, 'event_id': 'stage-1'}]


def make_inputs(tmp_path):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text(json.dumps({'qid': 'q1', 'question': 'normal', 'kb_scope': ['kb-stub'],
                                 'expected_chunk_ids': [], 'answer_points': ['actual body']}) + '\n', encoding='utf-8')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(model_id='stub-model', search_mode='hybrid', top_k=3, score_threshold=0,
                                     rerank_enabled=False, rerank_model_id=None, rerank_score_threshold=0,
                                     vector_weight=0.7, bm25_weight=0.3, rerank_top_k=3,
                                     context_window=1, use_rewrite=False, web_search_enabled=False)), encoding='utf-8')
    provenance = tmp_path / 'provenance.json'
    provenance.write_text(json.dumps({**{key: None for key in baseline.PROVENANCE_FIELDS},
                                      'dataset_version': 'stub-v1'}), encoding='utf-8')
    return cases, config, provenance


def arguments(tmp_path, api, output_name='run'):
    cases, config, provenance = make_inputs(tmp_path)
    return ['--base-url', api, '--cases', str(cases), '--cases-sha256', baseline.sha(cases),
            '--case', 'q1', '--request-config', str(config), '--provenance', str(provenance),
            '--provenance-sha256', baseline.sha(provenance), '--output', str(tmp_path / output_name),
            '--mode', 'chat', '--phase', 'warm', '--concurrency', '1', '--max-requests', '1']


def test_offline_preflight_no_http_and_immutable_manifest(tmp_path):
    argv = arguments(tmp_path, 'http://127.0.0.1:1')
    assert baseline.main(argv) == 0
    manifest = json.loads((tmp_path / 'run' / 'manifest.json').read_text(encoding='utf-8'))
    digest = manifest.pop('manifest_sha256')
    assert digest == baseline.canonical_hash(manifest)
    assert manifest['execution'] == 'OFFLINE_PREFLIGHT'
    assert manifest['runtime_settings_status'] == 'NOT_PROBED'
    with pytest.raises(FileExistsError):
        baseline.main(argv)


def test_full_cli_live_stub_records_unknown_and_raw_receipt(api, tmp_path):
    argv = arguments(tmp_path, api)
    assert baseline.main(argv + ['--execute', '--ack-model-requests']) == 0
    manifest = json.loads((tmp_path / 'run' / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['runtime_settings']['data']['revision'] == 7
    summary = json.loads((tmp_path / 'run' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['correctness'] == 'UNKNOWN'
    assert summary['measured_count'] == 1
    receipt_path = tmp_path / 'run' / summary['attempt_ids'][0] / 'receipt.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    assert receipt['provider']['call_count'] is None
    assert receipt['provider']['observed_response_count'] == 0
    assert receipt['request']['top_k'] == 3
    assert 'answer_points' not in receipt['request']
    assert receipt['manifest_sha256'] == manifest['manifest_sha256']


def test_guards_hashes_concurrency_send_ack_and_secret_keys(tmp_path):
    argv = arguments(tmp_path, 'http://127.0.0.1:1')
    with pytest.raises(SystemExit):
        baseline.parse_args(argv + ['--execute'])
    with pytest.raises(SystemExit):
        baseline.parse_args(argv + ['--concurrency', '8'])
    with pytest.raises(SystemExit):
        baseline.parse_args(argv + ['--phase', 'cold'])
    args = baseline.parse_args(argv)
    args.cases_sha256 = 'wrong'
    with pytest.raises(ValueError, match='SHA-256'):
        baseline.load_inputs(args)
    with pytest.raises(ValueError, match='secret-bearing'):
        baseline.no_secrets({'models': [{'api_key': 'never-write-me'}]})


def test_cli_runtime_probe_failure_keeps_manifest_and_sends_no_question(tmp_path):
    argv = arguments(tmp_path, 'http://127.0.0.1:1')
    assert baseline.main(argv + ['--execute', '--ack-model-requests', '--timeout', '0.1']) == 2
    summary = json.loads((tmp_path / 'run' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['status'] == 'BLOCKED_RUNTIME_SETTINGS'
    assert summary['completed_receipt_count'] == 0
    assert summary['not_sent_count'] == 1


def test_failed_wave_stops_future_requests_and_retains_raw(api, tmp_path):
    argv = arguments(tmp_path, api)
    cases = tmp_path / 'cases.jsonl'
    case = json.loads(cases.read_text(encoding='utf-8'))
    case['question'] = 'empty'
    cases.write_text(json.dumps(case) + '\n', encoding='utf-8')
    argv[argv.index('--cases-sha256') + 1] = baseline.sha(cases)
    argv[argv.index('--max-requests') + 1] = '3'
    assert baseline.main(argv + ['--repetitions', '3', '--execute', '--ack-model-requests']) == 1
    summary = json.loads((tmp_path / 'run' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['completed_receipt_count'] == 1
    assert summary['not_sent_count'] == 2
    assert len(Stub.requests) == 1
    raw = tmp_path / 'run' / summary['attempt_ids'][0] / 'client-receipt.json'
    assert json.loads(raw.read_text(encoding='utf-8'))['terminal_state'] == 'EMPTY_ANSWER'


@pytest.mark.parametrize('concurrency', [2, 4])
def test_concurrency_explicit_unique_attempts_and_manifest(api, tmp_path, concurrency):
    argv = arguments(tmp_path, api)
    argv[argv.index('--max-requests') + 1] = str(concurrency)
    argv[argv.index('--concurrency') + 1] = str(concurrency)
    assert baseline.main(argv + ['--repetitions', str(concurrency), '--execute', '--ack-model-requests']) == 0
    summary = json.loads((tmp_path / 'run' / 'summary.json').read_text(encoding='utf-8'))
    assert len(set(summary['attempt_ids'])) == concurrency
    starts = []
    for attempt in summary['attempt_ids']:
        receipt = json.loads((tmp_path / 'run' / attempt / 'receipt.json').read_text(encoding='utf-8'))
        assert receipt['concurrency'] == concurrency
        starts.append(receipt['request_started_monotonic_seconds'])
    assert max(starts) - min(starts) < 0.2


def test_lifecycle_poll_exact_identity_and_bounded_missing(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_text(json.dumps({'stage': 'qa.asgi_lifecycle', 'event': 'stage', 'attempt_id': 'other'}) + '\n', encoding='utf-8')
    def write_marker():
        time.sleep(.05)
        with path.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'stage': 'qa.asgi_lifecycle', 'event': 'stage', 'attempt_id': 'wanted',
                                  'metadata': {'asgi_app_ms': 40, 'post_body_app_cleanup_ms': 7,
                                               'asgi_app_status': 'RETURNED',
                                               'timing_scope': 'asgi_app_return_excluding_record_and_network_teardown'}}) + '\n')
    thread = threading.Thread(target=write_marker)
    thread.start()
    records, errors = baseline.wait_for_telemetry(path, 0, ['wanted'], timeout_seconds=.5, interval_seconds=.01)
    thread.join()
    assert not errors and len(records) == 2
    receipt = {'attempt_id': 'wanted', 'unknowns': [], 'terminal_state': 'SSE_DONE'}
    baseline.aggregate_telemetry(receipt, records)
    assert receipt['post_body_app_cleanup_ms'] == 7 and receipt['asgi_app_ms'] == 40
    assert receipt['server_request_cleanup_ms'] is None
    assert receipt['provider']['status'] == 'UNKNOWN'
    started = time.monotonic()
    records, errors = baseline.wait_for_telemetry(path, 0, ['missing'], timeout_seconds=.06, interval_seconds=.01)
    assert .04 <= time.monotonic() - started < .5
    receipt = {'attempt_id': 'missing', 'unknowns': [], 'terminal_state': 'SSE_DONE'}
    baseline.aggregate_telemetry(receipt, records, errors)
    assert receipt['asgi_lifecycle_status'] == 'UNKNOWN' and receipt['post_body_app_cleanup_ms'] is None


def test_attempt_usage_deduplicates_ids_excludes_legacy_and_retains_partial():
    receipt = {'attempt_id': 'a', 'unknowns': [], 'terminal_state': 'SSE_DONE'}
    def event(kind, local, **metadata):
        return {'attempt_id':'a', 'event':'stage', 'stage':'provider.request_'+kind,
                'event_id':kind+'-'+local, 'metadata':{'local_request_id':local, **metadata}}
    rows = [event('started','e1',stage='embedding'), event('started','e2',stage='embedding'),
            event('started','r1',stage='rerank'), event('started','l1',stage='agent'),
            event('finished','e1',usage_status='PROVIDER_REPORTED',usage={'input_tokens':7,'total_tokens':7},status='RETURNED'),
            event('finished','e2',usage_status='PROVIDER_REPORTED',usage={'input_tokens':7,'total_tokens':7},status='RETURNED'),
            event('finished','r1',usage_status='PROVIDER_REPORTED',usage={'total_tokens':3},status='RETURNED'),
            event('finished','l1',usage_status='UNKNOWN',status='UNKNOWN'),
            {'attempt_id':'a','event':'stage','event_id':'old','stage':'rerank.usage','metadata':{'usage':{'total_tokens':3}}},
            provider('old-llm','agent',{'total_tokens':99},attempt='a')]
    rows += [rows[4],rows[0]]
    baseline.aggregate_telemetry(receipt, rows)
    p=receipt['provider']
    assert p['observed_request_count']==4 and p['observed_call_count_lower_bound']==4
    assert p['observed_token_lower_bound']=={'input_tokens':14,'output_tokens':None,'total_tokens':17}
    assert p['token_channel_coverage']['output_tokens']['status']=='UNKNOWN'
    assert p['missing_usage_records']==1 and p['tokens'] is None
    assert p['legacy_usage_status']=='UNKNOWN_UNCORRELATED' and len(p['uncorrelated_legacy_records'])==2


def test_conflicting_finish_records_are_never_summed():
    receipt = {'attempt_id':'a','unknowns':[], 'terminal_state':'SSE_DONE'}
    rows=[{'attempt_id':'a','event':'stage','stage':'provider.request_started','event_id':'s',
           'metadata':{'local_request_id':'id'}},
          {'attempt_id':'a','event':'stage','stage':'provider.request_finished','event_id':'f1',
           'metadata':{'local_request_id':'id','usage_status':'PROVIDER_REPORTED','usage':{'total_tokens':2},'status':'RETURNED'}},
          {'attempt_id':'a','event':'stage','stage':'provider.request_finished','event_id':'f2',
           'metadata':{'local_request_id':'id','usage_status':'PROVIDER_REPORTED','usage':{'total_tokens':3},'status':'RETURNED'}}]
    baseline.aggregate_telemetry(receipt,rows)
    assert receipt['provider']['conflicting_finish_ids']==['id']
    assert receipt['provider']['observed_token_lower_bound']['total_tokens'] is None
    assert receipt['provider']['missing_usage_records']==1
