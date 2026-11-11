"""P0 bounded measurements against an already-running API (no server/model setup).

Dry-run is entirely offline. Execution is opt-in and never retries a question.
Raw client receipts and a pinned manifest are retained even for failed streams.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

# Import only pure validation helpers; this does not start the old isolated runner.
try:
    from scripts.run_p1_http_regression import citation_check, final_citations, sha, source_hash
except ModuleNotFoundError:
    from run_p1_http_regression import citation_check, final_citations, sha, source_hash

SCHEMA = 'rag-performance-p0/v1'
REQUEST_FIELDS = ('model_id', 'search_mode', 'top_k', 'score_threshold',
                  'rerank_enabled', 'rerank_model_id', 'rerank_score_threshold',
                  'vector_weight', 'bm25_weight', 'rerank_top_k', 'context_window',
                  'use_rewrite', 'web_search_enabled')
PROVENANCE_FIELDS = ('dataset_version', 'corpus_version', 'active_index_version',
                     'model_bindings', 'dynamic_route', 'thinking_parameters',
                     'context_policy', 'api_concurrency', 'worker_concurrency',
                     'observability', 'machine_resources')
# The server currently emits these acknowledgements as ordinary answer_chunk events.
ACK_PREFIX = re.compile(r'^(?:(?:已保存长期(?:输出偏好|偏好)：[^。\n]*。|'
                        r'已删除[^。\n]*(?:系统默认设置|系统默认值)。|'
                        r'长期偏好保存失败，[^。\n]*。|'
                        r'偏好变更已提交，但未能确认当前值，请稍后查看设置。)\s*)+')


def write_json(path, value):
    """Never overwrite an earlier experiment artifact."""
    with Path(path).open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def no_secrets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r'api.?key|secret|authorization|password|access.?token', key, re.I):
                raise ValueError('secret-bearing configuration key prohibited: ' + key)
            no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            no_secrets(child)


def load_inputs(args):
    if sha(args.cases) != args.cases_sha256:
        raise ValueError('case file SHA-256 mismatch')
    if sha(args.provenance) != args.provenance_sha256:
        raise ValueError('provenance SHA-256 mismatch')
    all_cases = [json.loads(line) for line in args.cases.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    seen = set()
    for case in all_cases:
        if not isinstance(case.get('qid'), str) or not case['qid'] or case['qid'] in seen:
            raise ValueError('unique nonempty qid required')
        seen.add(case['qid'])
        for field in ('question', 'expected_chunk_ids', 'answer_points', 'kb_scope'):
            if field not in case:
                raise ValueError('missing frozen case field: ' + field)
        if not isinstance(case['question'], str) or not case['question'].strip():
            raise ValueError('nonempty question required')
        if not isinstance(case['kb_scope'], list) or not case['kb_scope'] or any(not isinstance(x, str) or not x for x in case['kb_scope']):
            raise ValueError('explicit nonempty server KB scope required')
        for message in case.get('history', []):
            if message.get('role') not in ('user', 'assistant') or not isinstance(message.get('content'), str):
                raise ValueError('history must be frozen user/assistant messages')
    if len(set(args.case)) != len(args.case) or not set(args.case).issubset(seen):
        raise ValueError('case selection contains duplicates or unknown qid')
    cases = [case for qid in args.case for case in all_cases if case['qid'] == qid]
    config = json.loads(args.request_config.read_text(encoding='utf-8-sig'))
    if set(config) != set(REQUEST_FIELDS):
        raise ValueError('request config must explicitly contain exactly: ' + ', '.join(REQUEST_FIELDS))
    no_secrets(config)
    if config['search_mode'] not in ('hybrid', 'fulltext', 'vector'):
        raise ValueError('invalid search_mode')
    for field in ('rerank_enabled', 'use_rewrite', 'web_search_enabled'):
        if type(config[field]) is not bool:
            raise ValueError(field + ' must be boolean')
    for field in ('top_k', 'rerank_top_k'):
        if type(config[field]) is not int or config[field] < 1:
            raise ValueError(field + ' must be a positive integer')
    if type(config['context_window']) is not int or config['context_window'] < 0:
        raise ValueError('context_window must be a nonnegative integer')
    for field in ('score_threshold', 'rerank_score_threshold', 'vector_weight', 'bm25_weight'):
        if type(config[field]) not in (int, float) or not 0 <= config[field] <= 1:
            raise ValueError(field + ' must lie in [0, 1]')
    if config['web_search_enabled'] is not False:
        raise ValueError('P0 KB baseline requires web_search_enabled=false')
    provenance = json.loads(args.provenance.read_text(encoding='utf-8-sig'))
    no_secrets(provenance)
    missing = set(PROVENANCE_FIELDS) - set(provenance)
    if missing:
        raise ValueError('provenance keys required (unknown values may be null): ' + ', '.join(sorted(missing)))
    if not provenance['dataset_version']:
        raise ValueError('dataset_version required')
    for artifact in provenance.get('artifacts', []):
        path = Path(artifact['path'])
        if not path.is_absolute():
            path = args.provenance.parent / path
        if sha(path) != artifact['sha256']:
            raise ValueError('provenance artifact hash mismatch: ' + str(path))
    return cases, config, provenance


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--cases-sha256', required=True)
    parser.add_argument('--case', action='append', required=True, help='exact frozen qid, repeatable')
    parser.add_argument('--request-config', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--provenance-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory; existing paths rejected')
    parser.add_argument('--mode', choices=('chat', 'agent'), required=True)
    parser.add_argument('--phase', choices=('cold', 'warm'), required=True)
    parser.add_argument('--cold-attestation', help='external cold-state observation; runner never resets caches')
    parser.add_argument('--warmup-count', type=int, default=0, help='extra measured requests, serial before warm samples')
    parser.add_argument('--concurrency', type=int, choices=(1, 2, 4), required=True)
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--max-requests', type=int, required=True, help='hard HTTP question count cap, includes warmups')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--telemetry-jsonl', type=Path, help='local append-only API observation log; never cleared')
    parser.add_argument('--citation-db', type=Path, help='optional matching runtime SQLite DB, opened mode=ro')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--ack-model-requests', action='store_true', help='operator confirms approved sends/budget; not a provider-call limiter')
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error('base-url requires an HTTP(S) origin without credentials/query/fragment')
    if args.repetitions < 1 or args.warmup_count < 0 or args.max_requests < 1 or args.timeout <= 0:
        parser.error('counts/timeouts must be positive (warmup may be zero)')
    if args.phase == 'cold' and (not args.cold_attestation or args.warmup_count):
        parser.error('cold requires --cold-attestation and forbids warmup')
    if args.phase == 'warm' and args.cold_attestation:
        parser.error('cold-attestation only applies to cold')
    if args.execute and not args.ack_model_requests:
        parser.error('--execute requires --ack-model-requests')
    planned = len(args.case) * args.repetitions + args.warmup_count
    if args.phase == 'cold' and (planned != 1 or args.concurrency != 1):
        parser.error('cold labels only one first request at concurrency 1; externally establish cold state per run')
    if planned > args.max_requests:
        parser.error('planned requests exceed --max-requests')
    return args


def answer_text(data):
    if data.get('kind') in ('preference_ack', 'progress') or data.get('content_role') in ('preference_ack', 'progress'):
        return ''
    content = data.get('content')
    if not isinstance(content, str):
        return ''
    return ACK_PREFIX.sub('', content)


def request_payload(case, config, mode):
    # Gold fields never cross the API boundary.
    return {**config, 'messages': case.get('history', []) + [{'role': 'user', 'content': case['question']}],
            'kb_ids': case['kb_scope'], 'mode': mode, 'stream': True}


def measure_one(base_url, payload, identity, destination, timeout):
    """Incremental client clock; server cleanup is NOT implied by EOF or close."""
    import httpx
    destination.mkdir(exist_ok=False)
    headers = {'X-RAG-Run-ID': identity['run_id'], 'X-RAG-Attempt-ID': identity['attempt_id'],
               'X-RAG-QID': identity['qid'], 'X-RAG-Dataset-Version': identity['dataset_version']}
    receipt = {**identity, 'schema': SCHEMA, 'request': payload, 'http_status': None,
               'first_body_ms': None, 'terminal_ms': None, 'eof_ms': None,
               'client_release_ms': None, 'server_request_cleanup_ms': None,
               'terminal_state': 'NOT_SENT', 'answer': '', 'done': None,
               'request_started_at': datetime.now(timezone.utc).isoformat(),
               'request_started_monotonic_seconds': time.perf_counter(),
               'events': [], 'parse_errors': [], 'error': None,
               'correctness': {'status': 'UNKNOWN', 'reason': 'requires evidence-based review'},
               'citation_validation': {'status': 'UNKNOWN', 'reason': 'not checked'},
               'unknowns': ['server_request_cleanup_ms', 'correctness']}
    write_json(destination / 'request.json', {'identity': identity, 'payload': payload})
    start = time.perf_counter()
    elapsed = lambda: round((time.perf_counter() - start) * 1000, 3)
    kind, lines = 'message', []

    def dispatch():
        nonlocal kind, lines
        if not lines:
            kind = 'message'
            return
        raw = '\n'.join(lines)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('SSE data is not an object')
        except (ValueError, TypeError) as exc:
            receipt['parse_errors'].append({'at_ms': elapsed(), 'event': kind, 'data': raw, 'error': type(exc).__name__})
            kind, lines = 'message', []
            return
        event = {'event': kind, 'data': data, 'received_ms': elapsed()}
        receipt['events'].append(event)
        if kind == 'answer_chunk':
            body = answer_text(data)
            if body.strip():
                if receipt['first_body_ms'] is None:
                    receipt['first_body_ms'] = event['received_ms']
                receipt['answer'] += body
        if kind in ('done', 'error'):
            if receipt['terminal_ms'] is None:
                receipt['terminal_ms'] = event['received_ms']
            if kind == 'done':
                receipt['done'] = data
                if receipt['terminal_state'] != 'SSE_ERROR':
                    receipt['terminal_state'] = 'SSE_DONE'
            else:
                receipt['terminal_state'] = 'SSE_ERROR'
                receipt['error'] = data
        kind, lines = 'message', []

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream('POST', base_url.rstrip('/') + '/api/v1/chat/completions',
                               json=payload, headers=headers) as response:
                receipt['http_status'] = response.status_code
                receipt['response_headers'] = {key: value for key, value in response.headers.items()
                                               if key.lower() in ('content-type', 'x-request-id', 'x-trace-id')}
                receipt['terminal_state'] = 'HTTP_ERROR' if response.status_code != 200 else 'SSE_INCOMPLETE'
                # Exact received bytes retained; splitting lines happens as bytes arrive,
                # never after buffering response.text.
                pending = b''
                with (destination / 'response.sse').open('xb') as raw:
                    for block in response.iter_bytes():
                        raw.write(block)
                        raw.flush()
                        if response.status_code != 200:
                            continue
                        pending += block
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            line = line.rstrip(b'\r').decode('utf-8', errors='strict')
                            if not line:
                                dispatch()
                            elif line.startswith('event:'):
                                kind = line[6:].lstrip(' ')
                            elif line.startswith('data:'):
                                lines.append(line[5:].lstrip(' '))
                receipt['eof_ms'] = elapsed()
                if pending or lines:
                    receipt['parse_errors'].append({'at_ms': elapsed(), 'error': 'UNTERMINATED_SSE_FRAME',
                                                     'pending_bytes': len(pending), 'data_lines': lines})
    except Exception as exc:
        receipt['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        receipt['terminal_state'] = 'TRANSPORT_ERROR_AFTER_TERMINAL' if receipt['terminal_ms'] is not None else 'TRANSPORT_ERROR'
    finally:
        receipt['client_release_ms'] = elapsed()
    if receipt['terminal_state'] == 'SSE_DONE':
        if receipt['parse_errors']:
            receipt['terminal_state'] = 'MALFORMED_SSE'
        elif not receipt['answer']:
            receipt['terminal_state'] = 'EMPTY_ANSWER'
        elif receipt['done'].get('finish_reason') in ('length', 'max_tokens') or receipt['done'].get('generation_status') in ('TRUNCATED', 'INCOMPLETE', 'FAILED', 'UNKNOWN') or receipt['done'].get('status') in ('FAILED', 'UNKNOWN'):
            receipt['terminal_state'] = 'TRUNCATED_OR_FAILED'
    return receipt


def telemetry_rows(path, offset=0):
    if path is None or not path.exists():
        return [], ['telemetry unavailable']
    with path.open('rb') as source:
        source.seek(offset)
        data = source.read()
    records, errors = [], []
    for index, line in enumerate(data.splitlines(), 1):
        try:
            records.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            errors.append('telemetry malformed line ' + str(index))
    return records, errors



def wait_for_telemetry(path, offset, attempt_ids, *, timeout_seconds=2.0, interval_seconds=0.05):
    """Bounded wait for every exact attempt's ASGI lifecycle marker."""
    if path is None or not attempt_ids:
        return telemetry_rows(path, offset)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        records, errors = telemetry_rows(path, offset)
        seen = {row.get('attempt_id') for row in records
                if row.get('stage') == 'qa.asgi_lifecycle' and row.get('event') == 'stage'}
        if set(attempt_ids).issubset(seen) or time.monotonic() >= deadline:
            return records, errors
        time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))


def aggregate_telemetry(receipt, records, errors=()):
    """Count every Provider response in the trace, including grader/rewrite.

    Without an authoritative completeness marker, totals remain UNKNOWN and
    observed counts/sums are clearly labelled lower bounds.
    """
    matched = [row for row in records if row.get('attempt_id') == receipt['attempt_id']
               or (row.get('metadata') or {}).get('attempt_id') == receipt['attempt_id']]
    providers = [row for row in matched if row.get('stage') in ('llm.response', 'rerank.usage') and row.get('event') == 'stage']
    starts = [row for row in matched if row.get('stage') == 'provider.request_started' and row.get('event') == 'stage']
    dispatches, seen_dispatches = [], set()
    for row in starts:
        request_id = (row.get('metadata') or {}).get('local_request_id') or row.get('event_id')
        if request_id and request_id in seen_dispatches:
            continue
        if request_id:
            seen_dispatches.add(request_id)
        dispatches.append(row)
    # Deduplicate repeated exports of the same event, never by model/usage (two
    # legitimate identical calls still count separately).
    seen, unique = set(), []
    for row in providers:
        event_id = row.get('event_id')
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        unique.append(row)
    # The attempt finish is the sole accounting source for new traces. Legacy
    # response events lack local IDs and remain visible but uncorrelated; adding
    # them to attempt totals could double-count one paid call.
    finishes = [row for row in matched if row.get('stage') == 'provider.request_finished' and row.get('event') == 'stage']
    finish_by_id, conflicting_ids = {}, set()
    for row in finishes:
        meta = row.get('metadata') or {}
        local_id = meta.get('local_request_id')
        if not local_id:
            continue
        previous = finish_by_id.get(local_id)
        if previous is not None:
            old = previous.get('metadata') or {}
            if any(old.get(key) != meta.get(key) for key in ('usage_status', 'usage', 'usage_invalid_fields', 'status')):
                conflicting_ids.add(local_id)
        else:
            finish_by_id[local_id] = row
    fields = ('input_tokens', 'output_tokens', 'total_tokens')
    sums = {key: 0 for key in fields}
    coverage = {key: 0 for key in fields}
    supplied = set()
    for local_id, row in finish_by_id.items():
        if local_id in conflicting_ids:
            continue
        meta = row.get('metadata') or {}
        usage = meta.get('usage') if meta.get('usage_status') == 'PROVIDER_REPORTED' else None
        if not isinstance(usage, dict):
            continue
        valid = False
        for key in fields:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                sums[key] += value
                coverage[key] += 1
                valid = True
        if valid:
            supplied.add(local_id)
    dispatch_ids = {(row.get('metadata') or {}).get('local_request_id') for row in dispatches}
    dispatch_ids.discard(None)
    perf = [row.get('metadata', {}) for row in matched if row.get('stage') == 'qa.performance']
    summary = perf[-1] if perf else {}
    complete = (summary.get('provider_telemetry_complete') is True and not errors and bool(dispatch_ids)
                and len(dispatch_ids) == len(dispatches) and dispatch_ids == set(finish_by_id)
                and not conflicting_ids)
    lower_bound = {key: sums[key] if coverage[key] else None for key in fields}
    channel_coverage = {key: {'provider_reported_attempts': coverage[key], 'observed_dispatches': len(dispatches),
                              'status': 'COMPLETE' if complete and coverage[key] == len(dispatches)
                                        else 'PARTIAL' if coverage[key] else 'UNKNOWN'} for key in fields}
    all_fields_complete = complete and all(coverage[key] == len(dispatches) for key in fields)
    missing = len(dispatch_ids - supplied) + sum(1 for row in dispatches if not (row.get('metadata') or {}).get('local_request_id'))
    receipt['trace_ids'] = sorted({row['trace_id'] for row in matched if row.get('trace_id')})
    receipt['provider'] = {'status': 'COMPLETE' if complete else 'UNKNOWN',
                           'call_count': len(dispatches) if complete else None,
                           'observed_request_count': len(dispatches),
                           'observed_call_count_lower_bound': len(dispatches),
                           'observed_response_count': len(unique), 'observed_count_semantics': 'EXACT' if complete else 'LOWER_BOUND',
                           'tokens': sums if all_fields_complete else None,
                           'observed_token_lower_bound': lower_bound, 'token_channel_coverage': channel_coverage,
                           'missing_usage_records': missing, 'conflicting_finish_ids': sorted(conflicting_ids),
                           'records': unique, 'uncorrelated_legacy_records': unique,
                           'legacy_usage_status': 'UNKNOWN_UNCORRELATED' if unique else 'NOT_OBSERVED',
                           'dispatch_records': dispatches, 'finish_records': finishes,
                           'done_usage_used': False}
    lifecycle = [row.get('metadata') or {} for row in matched
                 if row.get('stage') == 'qa.asgi_lifecycle' and row.get('event') == 'stage']
    lifecycle_summary = lifecycle[0] if len(lifecycle) == 1 else {}
    receipt['asgi_app_ms'] = lifecycle_summary.get('asgi_app_ms')
    receipt['post_body_app_cleanup_ms'] = lifecycle_summary.get('post_body_app_cleanup_ms')
    receipt['asgi_app_status'] = lifecycle_summary.get('asgi_app_status')
    receipt['asgi_timing_scope'] = lifecycle_summary.get('timing_scope')
    receipt['asgi_lifecycle_status'] = 'OBSERVED' if len(lifecycle) == 1 else 'AMBIGUOUS' if lifecycle else 'UNKNOWN'
    receipt['server_request_cleanup_ms'] = summary.get('request_cleanup_ms')
    receipt['server_pipeline_ms'] = summary.get('server_pipeline_ms')
    receipt['post_terminal_cleanup_ms'] = summary.get('post_terminal_cleanup_ms')
    receipt['server_timing_scope'] = summary.get('timing_scope')
    receipt['stage_timings_ms'] = {}
    receipt['telemetry_warnings'] = []
    for row in matched:
        if row.get('event') == 'end' and isinstance(row.get('latency_ms'), (int, float)):
            receipt['stage_timings_ms'].setdefault(row.get('stage', 'UNKNOWN'), []).append(row['latency_ms'])
        elif row.get('stage') == 'performance.stage':
            meta = row.get('metadata') or {}
            if isinstance(meta.get('duration_ms'), (int, float)):
                stage_name = meta.get('stage_name')
                if not isinstance(stage_name, str) or not stage_name:
                    receipt['telemetry_warnings'].append({'code': 'UNKNOWN_STAGE_NAME',
                        'raw_stage_name': stage_name, 'event_id': row.get('event_id')})
                    stage_name = 'UNKNOWN'
                receipt['stage_timings_ms'].setdefault(stage_name, []).append(meta['duration_ms'])
    receipt['runtime_observed_config'] = [row for row in matched if row.get('stage') in ('qa.config', 'qa.resolved_config')]
    receipt['stages'] = [row for row in matched if row.get('stage') in ('performance.stage', 'qa.performance')]
    for field in ('actual_candidate_count', 'final_evidence_count', 'retry_count', 'upgrade_count'):
        receipt[field] = summary.get(field)
    receipt['telemetry_errors'] = list(errors)
    receipt['unknowns'] = [key for key in receipt['unknowns'] if key != 'server_request_cleanup_ms' or receipt[key] is None]
    receipt['unknowns'] += [key for key in ('actual_candidate_count', 'final_evidence_count', 'retry_count', 'upgrade_count') if receipt[key] is None]
    if not complete:
        receipt['unknowns'].append('provider_call_count')
    if not all_fields_complete:
        receipt['unknowns'].append('provider_token_totals')
    if any((row.get('metadata') or {}).get('status') == 'TRUNCATED' for row in unique):
        if receipt['terminal_state'] == 'SSE_DONE':
            receipt['terminal_state'] = 'PROVIDER_TRUNCATED'
    return matched


def validate_citations(receipt, db_path):
    citations = final_citations([(row['event'], row['data']) for row in receipt['events']])
    if db_path is None:
        receipt['citation_validation']['observed_count'] = len(citations)
        return
    checks = []
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            for citation in citations:
                per_scope = [citation_check(db, citation, kb) for kb in receipt['request']['kb_ids']]
                valid = any(check.get('valid_scope_and_quote') is True or check.get('valid_scope_and_source_hash') is True for check in per_scope)
                checks.append({'citation': citation, 'valid': valid, 'checks_by_scope': per_scope})
        receipt['citation_validation'] = {'status': 'PASS' if checks and all(row['valid'] for row in checks) else 'FAIL' if checks else 'NO_CITATIONS', 'checks': checks}
    except (sqlite3.Error, OSError) as exc:
        receipt['citation_validation'] = {'status': 'UNKNOWN', 'error_type': type(exc).__name__}


def main(argv=None):
    args = parse_args(argv)
    cases, config, provenance = load_inputs(args)
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True)
    manifest = {'schema': SCHEMA, 'run_id': uuid.uuid4().hex,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'execution': 'EXECUTE' if args.execute else 'OFFLINE_PREFLIGHT',
                'base_url': args.base_url, 'mode': args.mode, 'phase': args.phase,
                'cold_attestation': args.cold_attestation, 'concurrency': args.concurrency,
                'cache_state_evidence': 'EXTERNAL_ATTESTATION' if args.phase == 'cold' else 'EXPLICIT_WARMUP_REQUESTS' if args.warmup_count else 'UNVERIFIED_EXTERNAL_WARM_STATE',
                'repetitions': args.repetitions, 'warmup_count': args.warmup_count,
                'planned_request_count': len(cases) * args.repetitions + args.warmup_count,
                'max_requests': args.max_requests, 'case_ids': [row['qid'] for row in cases],
                'cases_sha256': sha(args.cases), 'request_config_sha256': sha(args.request_config),
                'provenance_sha256': sha(args.provenance), 'runner_sha256': sha(Path(__file__)),
                'validator_sha256': sha(root / 'scripts/run_p1_http_regression.py'),
                'git_commit': git.stdout.strip() if git.returncode == 0 else None,
                'local_app_source_sha256': source_hash(root / 'backend'),
                'request_config': config, 'provenance': provenance,
                'client_transport': 'per_request_httpx_client_trust_env_false_no_retries',
                'runtime_settings': None, 'runtime_settings_status': 'NOT_PROBED',
                'note': 'local Git/source hashes do not establish remote deployed revision'}
    if args.execute:
        import httpx
        # Read effective persisted settings, not source-code defaults. No model calls.
        try:
            with httpx.Client(timeout=args.timeout, trust_env=False) as client:
                response = client.get(args.base_url.rstrip('/') + '/api/v1/settings')
                response.raise_for_status()
                settings = response.json()
                no_secrets(settings)
                manifest['runtime_settings'] = settings
                manifest['runtime_settings_status'] = 'LIVE_HTTP_READ'
        except Exception as exc:
            manifest['runtime_settings_status'] = 'FAILED_NO_QUESTIONS_SENT'
            manifest['runtime_probe_error_type'] = type(exc).__name__
            digest = canonical_hash(manifest)
            write_json(args.output / 'manifest.json', {**manifest, 'manifest_sha256': digest})
            write_json(args.output / 'summary.json', {'status': 'BLOCKED_RUNTIME_SETTINGS',
                       'manifest_sha256': digest, 'completed_receipt_count': 0,
                       'not_sent_count': manifest['planned_request_count'], 'provider_calls': 0})
            return 2
    manifest_hash = canonical_hash(manifest)
    write_json(args.output / 'manifest.json', {**manifest, 'manifest_sha256': manifest_hash})
    if not args.execute:
        print(json.dumps({'status': 'OFFLINE_PREFLIGHT_PASS', 'provider_calls': 0,
                          'manifest_sha256': manifest_hash, 'output': str(args.output)}))
        return 0
    offset = args.telemetry_jsonl.stat().st_size if args.telemetry_jsonl and args.telemetry_jsonl.exists() else 0
    jobs = [('warmup', index, cases[index % len(cases)]) for index in range(args.warmup_count)]
    jobs += [('measured', rep, case) for rep in range(args.repetitions) for case in cases]
    results = []

    def run(job):
        sample_kind, rep, case = job
        identity = {'run_id': manifest['run_id'], 'attempt_id': uuid.uuid4().hex,
                    'qid': case['qid'], 'dataset_version': provenance['dataset_version'],
                    'manifest_sha256': manifest_hash, 'sample_kind': sample_kind,
                    'repetition': rep, 'mode': args.mode, 'phase': args.phase,
                    'concurrency': args.concurrency}
        directory = args.output / identity['attempt_id']
        receipt = measure_one(args.base_url, request_payload(case, config, args.mode), identity, directory, args.timeout)
        # Persist before any post-run analysis so failure cannot erase raw receipts.
        write_json(directory / 'client-receipt.json', receipt)
        return receipt

    # Warmups are serial and separately retained. Never silently ramp 1 -> 2 -> 4.
    stopped = False
    for job in jobs[:args.warmup_count]:
        result = run(job)
        results.append(result)
        if result['terminal_state'] != 'SSE_DONE':
            stopped = True
            break
    measured = jobs[args.warmup_count:]
    if not stopped:
        # Dispatch bounded waves; failure/unknown suppresses later waves. In-flight
        # requests in a wave cannot be recalled and are retained individually.
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for start in range(0, len(measured), args.concurrency):
                wave = list(pool.map(run, measured[start:start + args.concurrency]))
                results.extend(wave)
                if any(row['terminal_state'] != 'SSE_DONE' for row in wave):
                    stopped = True
                    break
    records, errors = wait_for_telemetry(args.telemetry_jsonl, offset,
                                         [receipt['attempt_id'] for receipt in results])
    for receipt in results:
        matched = aggregate_telemetry(receipt, records, errors)
        case = next(case for case in cases if case['qid'] == receipt['qid'])
        receipt['offline_review_reference'] = {key: case[key] for key in ('expected_chunk_ids', 'answer_points')}
        validate_citations(receipt, args.citation_db)
        write_json(args.output / receipt['attempt_id'] / 'telemetry.json', matched)
        write_json(args.output / receipt['attempt_id'] / 'receipt.json', receipt)
    counts = {state: sum(row['terminal_state'] == state for row in results)
              for state in sorted({row['terminal_state'] for row in results})}
    summary = {'schema': SCHEMA, 'manifest_sha256': manifest_hash,
               'status': 'FAIL' if stopped else 'MEASURED_QUALITY_UNREVIEWED',
               'planned_request_count': manifest['planned_request_count'], 'completed_receipt_count': len(results),
               'not_sent_count': manifest['planned_request_count'] - len(results),
               'measured_count': sum(row['sample_kind'] == 'measured' for row in results),
               'warmup_count': sum(row['sample_kind'] == 'warmup' for row in results),
               'terminal_states': counts, 'correctness': 'UNKNOWN', 'percentiles': 'NOT_COMPUTED_SMALL_SAMPLE',
               'attempt_ids': [row['attempt_id'] for row in results]}
    write_json(args.output / 'summary.json', summary)
    print(json.dumps(summary))
    return 1 if stopped else 0


if __name__ == '__main__':
    sys.exit(main())
