"""Read-only, hash-bound per-attempt observations from the two host formats.

This module projects metadata, not private request/answer bodies.  UNKNOWN is a
value, never a zero measurement or evidence that a stage did not run.
"""
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from .reader import digest, read_artifact


UNKNOWN = 'UNKNOWN'
SCHEMA = 'rsi-case-observations/v1'


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) else UNKNOWN


def _identity(value):
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) and value != '' else UNKNOWN


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else UNKNOWN


def _read(path, expected):
    """Parse the very bytes checked, rather than reopening a checked pathname."""
    if _hash(expected) == UNKNOWN:
        raise ValueError('CASE_SOURCE_HASH_REQUIRED')
    path = Path(path).resolve(strict=True)
    if any(re.search(r'(^|[-_])(gold|sealed|closed|holdout)([-_.]|$)', part, re.I)
           for part in path.parts):
        raise ValueError('CASE_CLOSED_SOURCE_FORBIDDEN')
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError('CASE_SOURCE_HASH_MISMATCH')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('CASE_SOURCE_INVALID')
    return data, {'sha256': expected, 'source_id': 'sha256:' + expected}


def _adjacent(directory, relative):
    path = directory / relative
    if path.resolve(strict=True) != directory.resolve(strict=True) / relative:
        raise ValueError('CASE_SOURCE_PATH_MISMATCH')
    return path


def _ref(source, pointer):
    return {'source_id': source['source_id'], 'pointer': pointer}


def _unique(records, key):
    seen = {}
    for record in records:
        identity = key(record)
        # Missing identities must not collapse unrelated unknown observations.
        if UNKNOWN in identity:
            yield record
            continue
        if identity in seen:
            if ({k: v for k, v in seen[identity].items() if k != 'sources'}
                    != {k: v for k, v in record.items() if k != 'sources'}):
                raise ValueError('CASE_CONFLICTING_IDENTITY')
            continue
        seen[identity] = record
        yield record


def _check_identity(record, parent, keys):
    for key in keys:
        if key in record and key in parent and record[key] != parent[key]:
            raise ValueError('CASE_IDENTITY_MISMATCH')


def _validate_quality_identity(record):
    for key in ('case_id', 'attempt_id'):
        if record.get(key) is not None and (not isinstance(record[key], str) or not record[key]):
            raise ValueError('CASE_IDENTITY_INVALID')
    for key in ('repeat_id', 'attempt_number'):
        if record.get(key) is not None and (type(record[key]) is not int or record[key] < 1):
            raise ValueError('CASE_IDENTITY_INVALID')


def _stage(status):
    if status in ('NOT RUN', 'NOT_RUN'):
        return 'NOT_RUN'
    return status if status in ('RETURNED', 'FAILED', 'COMPLETED', 'UNKNOWN', 'CACHE_HIT') else UNKNOWN


def _usage(call):
    usage = call.get('usage')
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if not isinstance(usage, dict):
        usage = {}
    amounts = {key: value if type(value := usage.get(key)) is int and value >= 0 else UNKNOWN for key in keys}
    return {'status': 'PROVIDER_REPORTED' if all(value != UNKNOWN for value in amounts.values()) else UNKNOWN,
            **amounts}


def _calls(attempt, namespace):
    result = []
    for call in attempt.get(namespace + '_calls') or []:
        if not isinstance(call, dict):
            raise ValueError('CASE_CALL_INVALID')
        owner = call.get('record') if namespace == 'embedding' else call
        if isinstance(owner, dict):
            _validate_quality_identity(owner)
            _check_identity(owner, attempt, ('case_id', 'repeat_id', 'attempt_id', 'attempt_number'))
        attributable = isinstance(owner, dict) and all(
            owner.get(key) == attempt.get(key) and _identity(owner.get(key)) != UNKNOWN
            for key in ('case_id', 'repeat_id', 'attempt_id'))
        sequence = call.get('seq')
        sequence = sequence if type(sequence) is int and sequence > 0 else UNKNOWN
        # Allocation of seq precedes dispatch in RecordingProxy; UNKNOWN alone
        # therefore does not prove that a provider request left the process.
        actual = call.get('actual_provider_requests') if namespace == 'embedding' else (
            1 if call.get('request_status') == 'RETURNED' or type(call.get('http_status')) is int else UNKNOWN)
        actual = actual if type(actual) is int and actual in (0, 1) else UNKNOWN
        result.append({'namespace': namespace, 'sequence_id': sequence,
            'attribution_status': 'BOUND' if attributable else UNKNOWN,
            'actual_provider_requests': actual, 'status': _stage(call.get('request_status')),
            'provider_model': _identity(call.get('provider_model')),
            'provider_request_id': _identity(call.get('provider_request_id')),
            'usage': _usage(call) if actual == 1 else _usage({}),
            'elapsed_seconds': _number(call.get('elapsed_seconds')),
            'elapsed_scope': 'provider_and_receipt_processing' if namespace == 'provider' else 'embedding_operation',
            'context_chars': _number(call.get('context_chars')) if call.get('context_status') == 'VERIFIED_IN_ACTUAL_REQUEST' else UNKNOWN,
            'context_bytes': _number(call.get('context_bytes')) if call.get('context_status') == 'VERIFIED_IN_ACTUAL_REQUEST' else UNKNOWN,
            'context_measurement_scope': call.get('context_measurement_scope') if call.get('context_status') == 'VERIFIED_IN_ACTUAL_REQUEST' else UNKNOWN,
            'prompt_sha256': UNKNOWN, 'request_sha256': UNKNOWN})
    return list(_unique(result, lambda call: (call['namespace'], call['sequence_id'])))


def _retrieval(attempt):
    output = []
    config = attempt.get('retrieval_config') or {}
    for call in attempt.get('retrieval_calls') or []:
        metrics = call.get('metrics') or {}
        candidates = call.get('candidate_ids')
        candidates = candidates if isinstance(candidates, list) and all(isinstance(cid, str) for cid in candidates) else UNKNOWN
        measured = {key: _number(metrics.get(key)) for key in (
            'vector_count', 'bm25_count', 'merged_count', 'final_count',
            'vector_latency_ms', 'bm25_latency_ms', 'total_latency_ms')}
        output.append({'candidate_ids': candidates,
            'candidate_count': len(candidates) if isinstance(candidates, list) else UNKNOWN,
            'search_mode': call.get('search_mode') if call.get('search_mode') in ('fulltext', 'hybrid', 'vector') else UNKNOWN,
            'status': 'RETURNED', 'measurement_scope': 'RetrievalService.search_return_metrics',
            'metrics': measured,
            'fallback_used': metrics.get('fallback_used') if type(metrics.get('fallback_used')) is bool else UNKNOWN,
            'configured_rerank_enabled': config.get('rerank_enabled') if type(config.get('rerank_enabled')) is bool else UNKNOWN,
            'readiness': {key: metrics.get(key) if type(metrics.get(key)) is bool else UNKNOWN
                          for key in ('vector_ready', 'bm25_ready', 'rerank_ready')},
            'stage_memberships': {key: UNKNOWN for key in ('vector', 'bm25', 'fusion', 'rerank')}})
    return output


def _missing_planned(job, rows):
    planned = job.get('records')
    if not isinstance(planned, list):
        repeats = job.get('repeat')
        planned = ([{'case_id': case['id'], 'repeat_id': repeat}
                    for case in job.get('cases', []) if isinstance(case, dict) and 'id' in case
                    for repeat in range(1, repeats + 1)]
                   if type(repeats) is int and repeats > 0 else [])
    observed = {(row['qid'], row['repeat_id']) for row in rows}
    return [{'qid': _identity(record.get('case_id')), 'repeat_id': _identity(record.get('repeat_id')),
             'attempt_id': UNKNOWN, 'status': UNKNOWN}
            for record in planned if isinstance(record, dict)
            and (record.get('case_id'), record.get('repeat_id')) not in observed]


def _base(run_id, candidate_id, qid, repeat_id, attempt_id, versions, sources):
    return {'run_id': _identity(run_id), 'candidate_id': _identity(candidate_id),
            'qid': _identity(qid), 'repeat_id': _identity(repeat_id),
            'attempt_id': _identity(attempt_id), 'versions': versions, 'sources': sources,
            'prompt_sha256': UNKNOWN, 'final_request_sha256': UNKNOWN}


def project_quality_cases(worker_file, expected_sha256, *, candidate_id, artifact_sha256,
                          evaluation_sha256, product_source_sha256=None, candidate_kind):
    """Project raw attempts; never use merged retry usage as a request receipt."""
    worker_path = Path(worker_file).resolve(strict=True)
    if worker_path.name != 'worker.json':
        raise ValueError('CASE_WORKER_PATH_INVALID')
    worker, worker_ref = _read(worker_path, expected_sha256)
    source = _adjacent(worker_path.parent, Path('product') / 'results.json')
    # Transferred workers may retain an ECS absolute pathname. Only the fixed
    # adjacent source is opened; the recorded identity must still name it.
    recorded = str(worker.get('source_record', '')).replace('\\', '/').split('/')
    if recorded[-2:] != ['product', 'results.json']:
        raise ValueError('CASE_SOURCE_PATH_MISMATCH')
    raw, raw_ref = _read(source, worker.get('source_sha256'))
    job_path = _adjacent(worker_path.parent, Path('job.json'))
    job_bytes = job_path.read_bytes()
    job = json.loads(job_bytes)
    if not isinstance(job, dict) or _hash(artifact_sha256) == UNKNOWN or digest(job.get('artifact')) != artifact_sha256:
        raise ValueError('CASE_ARTIFACT_VERSION_MISMATCH')
    artifact = job['artifact']
    if isinstance(artifact, dict) and 'candidate_id' in artifact and artifact['candidate_id'] != candidate_id:
        raise ValueError('CASE_CANDIDATE_VERSION_MISMATCH')
    if _hash(evaluation_sha256) == UNKNOWN:
        raise ValueError('CASE_EVALUATION_HASH_REQUIRED')
    if product_source_sha256 is not None and _hash(product_source_sha256) == UNKNOWN:
        raise ValueError('CASE_PRODUCT_SOURCE_HASH_INVALID')
    job_ref = {'source_id': 'sha256:' + sha256(job_bytes).hexdigest()}
    versions = {'artifact_sha256': artifact_sha256, 'evaluation_sha256': evaluation_sha256,
        'worker_sha256': UNKNOWN, 'product_source_sha256': UNKNOWN,
        'declared_product_source_sha256': product_source_sha256 or UNKNOWN,
        'candidate_kind': candidate_kind, 'effective_config_sha256': UNKNOWN,
        'evaluation_scope': 'host_declared_version_only', 'product_source_scope': 'host_declared_unlinked'}
    rows = []
    seen_attempts, seen_calls = {}, {}
    for index, case in enumerate(raw.get('results', [])):
        if not isinstance(case, dict):
            raise ValueError('CASE_RESULT_INVALID')
        _validate_quality_identity(case)
        attempts = case.get('attempts')
        if attempts is None:
            attempts = [case]  # Explicit legacy attempt_id is retained; absence stays UNKNOWN.
        if not isinstance(attempts, list):
            raise ValueError('CASE_ATTEMPTS_INVALID')
        for offset, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                raise ValueError('CASE_ATTEMPT_INVALID')
            _validate_quality_identity(attempt)
            _check_identity(attempt, case, ('case_id', 'repeat_id'))
            identity = tuple(_identity(attempt.get(key)) for key in ('case_id', 'repeat_id', 'attempt_id'))
            if UNKNOWN not in identity:
                if identity in seen_attempts:
                    if seen_attempts[identity] != attempt:
                        raise ValueError('CASE_CONFLICTING_IDENTITY')
                    continue
                seen_attempts[identity] = attempt
            for namespace in ('provider', 'embedding'):
                for call in attempt.get(namespace + '_calls') or []:
                    sequence = call.get('seq')
                    if type(sequence) is int and sequence > 0:
                        call_identity = (namespace, sequence)
                        if call_identity in seen_calls and seen_calls[call_identity] != call:
                            raise ValueError('CASE_CONFLICTING_REQUEST_IDENTITY')
                        seen_calls[call_identity] = call
            pointer = f'/results/{index}' + (f'/attempts/{offset}' if 'attempts' in case else '')
            row = _base(worker.get('run_id'), candidate_id, case.get('case_id'), case.get('repeat_id'),
                attempt.get('attempt_id'), dict(versions), [_ref(worker_ref, ''), _ref(raw_ref, pointer), _ref(job_ref, '/artifact')])
            config = attempt.get('retrieval_config')
            if isinstance(config, dict):
                row['versions']['effective_config_sha256'] = digest(config)
            row.update(provenance_scope='raw_product_attempt_metadata',
                identity_status='BOUND' if all(row[key] != UNKNOWN for key in ('run_id', 'qid', 'repeat_id', 'attempt_id')) else UNKNOWN,
                stages={'case': _stage(attempt.get('status')), 'answer': UNKNOWN, 'token': UNKNOWN},
                failure_stage=_identity(attempt.get('failure_stage')),
                elapsed_seconds=_number(attempt.get('elapsed_seconds')), elapsed_scope='product_record_attempt',
                provider_requests=_calls(attempt, 'provider'), embedding_requests=_calls(attempt, 'embedding'),
                retrieval_calls=_retrieval(attempt),
                context_chars=_number(attempt.get('final_context_chars')) if attempt.get('final_context_status') == 'VERIFIED_IN_ACTUAL_REQUEST' else UNKNOWN,
                context_bytes=_number(attempt.get('final_context_bytes')) if attempt.get('final_context_status') == 'VERIFIED_IN_ACTUAL_REQUEST' else UNKNOWN)
            rows.append(row)
    rows = list(_unique(rows, lambda row: tuple(row[key] for key in ('run_id', 'candidate_id', 'qid', 'repeat_id', 'attempt_id'))))
    return {'schema': SCHEMA, 'sources': [worker_ref, raw_ref, job_ref], 'rows': rows,
            'unassigned_requests_status': UNKNOWN, 'missing_planned_cases': _missing_planned(job, rows)}


def _facts(value):
    # Preserve factual statuses without copying evaluator text or private bodies.
    allowed = {'PRESENT', 'ABSENT', 'MISSING', 'UNKNOWN', 'PASS', 'FAIL', 'SUPPORTED', 'UNSUPPORTED'}
    return {key: status for key, status in value.items() if isinstance(key, str) and status in allowed} if isinstance(value, dict) else {}


def project_replay_cases(round_file, expected_sha256, replay_manifest_file, replay_manifest_sha256,
                         replay_results_file, replay_results_sha256, *, angle, evaluation_sha256):
    """Join explicit A/B metadata and validated Round facts, without opening captures."""
    archived = read_artifact(round_file, expected_sha256, 'round')
    manifest, manifest_ref = _read(replay_manifest_file, replay_manifest_sha256)
    results, results_ref = _read(replay_results_file, replay_results_sha256)
    if _hash(evaluation_sha256) == UNKNOWN or manifest.get('evaluation', {}).get('sha256') != evaluation_sha256:
        raise ValueError('CASE_EVALUATION_VERSION_MISMATCH')
    decisions = [decision for decision in archived['data'].get('candidate_decisions', []) if decision.get('angle') == angle]
    if len(decisions) != 1:
        raise ValueError('CASE_REPLAY_DECISION_AMBIGUOUS')
    decision = decisions[0]
    arms = manifest.get('arms', {})
    selected = [arm for arm in ('A', 'B') if arms.get(arm, {}).get('candidate_id') == decision.get('candidate_id')]
    if len(selected) != 1:
        raise ValueError('CASE_REPLAY_CANDIDATE_MISMATCH')
    candidate_arm = selected[0]
    spec = arms[candidate_arm]
    parent_spec = arms.get('A', {})
    for field, expected in (('parent_id', parent_spec.get('candidate_id')),
                            ('parent_worker_sha256', parent_spec.get('worker_sha256')),
                            ('parent_config_sha256', parent_spec.get('sha256'))):
        if field in archived['data'] and archived['data'][field] != expected:
            raise ValueError('CASE_REPLAY_PARENT_VERSION_MISMATCH')
    if (_hash(decision.get('worker_hash')) == UNKNOWN or spec.get('worker_sha256') != decision['worker_hash']
            or _hash(decision.get('config_hash')) == UNKNOWN or spec.get('sha256') != decision['config_hash']):
        raise ValueError('CASE_REPLAY_VERSION_MISMATCH')
    questions = list(_unique(decision.get('per_question', []), lambda row: (_identity(row.get('qid')),)))
    by_qid = {row['qid']: row for row in questions if _identity(row.get('qid')) != UNKNOWN}
    raw_rows = list(_unique(results.get('rows', []), lambda row: (_identity(row.get('qid')),)))
    rows = []
    run_id = manifest.get('run_id', results.get('summary', {}).get('run_id'))
    if manifest.get('run_id') and results.get('summary', {}).get('run_id') not in (None, manifest['run_id']):
        raise ValueError('CASE_REPLAY_RUN_MISMATCH')
    for index, raw in enumerate(raw_rows):
        index = results.get('rows', []).index(raw)
        question = by_qid.get(raw.get('qid'))
        if question is None:
            raise ValueError('CASE_REPLAY_QID_MISMATCH')
        upstream = _hash(raw.get('upstream_sha256'))
        paired = upstream != UNKNOWN and upstream == question.get('upstream_hash') and question.get('pair_identity') is True
        if upstream != UNKNOWN and question.get('upstream_hash') not in (None, UNKNOWN, upstream):
            raise ValueError('CASE_REPLAY_UPSTREAM_MISMATCH')
        for arm in ('A', 'B'):
            arm_spec = arms.get(arm, {})
            score = raw.get('arms', {}).get(arm, {})
            for key, expected in (('candidate_id', arm_spec.get('candidate_id')), ('qid', raw.get('qid')),
                                  ('run_id', run_id), ('upstream_sha256', raw.get('upstream_sha256'))):
                if key in score and score[key] != expected:
                    raise ValueError('CASE_REPLAY_IDENTITY_MISMATCH')
            for key, expected in (('worker_sha256', arm_spec.get('worker_sha256')), ('config_sha256', arm_spec.get('sha256'))):
                if key in score and score[key] != expected:
                    raise ValueError('CASE_REPLAY_VERSION_MISMATCH')
            prefix = 'candidate' if arm == candidate_arm else 'parent'
            measured = paired and question.get('execution') == 'COMPLETED' and question.get('pair_status') == 'PAIRED'
            chars = _number(question.get('chars_' + arm)) if measured else UNKNOWN
            if measured and score.get('context_chars') is not None and score['context_chars'] != chars:
                raise ValueError('CASE_REPLAY_CONTEXT_MISMATCH')
            refs = [_ref(archived, '/candidate_decisions'), _ref(manifest_ref, '/arms/' + arm), _ref(results_ref, f'/rows/{index}/arms/{arm}')]
            row = _base(run_id, arm_spec.get('candidate_id'), raw.get('qid'), raw.get('repeat_id'), score.get('attempt_id'),
                {'worker_sha256': _hash(arm_spec.get('worker_sha256')), 'effective_config_sha256': _hash(arm_spec.get('sha256')),
                 'evaluation_sha256': evaluation_sha256, 'evaluation_scope': 'manifest_version_and_round_assessment',
                 'worker_scope': 'archived_manifest_version_only', 'product_source_sha256': UNKNOWN}, refs)
            summary = results.get('summary', {})
            row.update(arm=arm, provenance_scope='validated_round_replay_metadata',
                identity_status='BOUND' if measured and all(row[key] != UNKNOWN for key in ('run_id', 'repeat_id', 'attempt_id')) else UNKNOWN,
                pair_binding_status='BOUND' if measured else UNKNOWN,
                failure_stage=_identity(raw.get('failure_stage')),
                stages={'case': 'COMPLETED' if measured else UNKNOWN,
                        'answer': 'NOT_RUN' if summary.get('answer_quality') == 'NOT_RUN' else UNKNOWN,
                        'token': 'NOT_RUN' if summary.get('token_usage') == 'NOT_RUN' else UNKNOWN},
                context_chars=chars, context_bytes=_number(score.get('context_utf8_bytes')) if measured else UNKNOWN,
                elapsed_seconds=UNKNOWN, elapsed_scope=UNKNOWN, provider_requests=[], embedding_requests=[],
                provider_measurement_status=UNKNOWN, upstream_sha256=upstream,
                facts=_facts(question.get(prefix + '_facts')) if measured else {},
                provenance_status=question.get('provenance_' + arm) if measured and question.get('provenance_' + arm) in ('PRESENT', 'ABSENT') else UNKNOWN)
            rows.append(row)
    return {'schema': SCHEMA, 'sources': [{'source_id': archived['source_id'], 'sha256': archived['sha256']}, manifest_ref, results_ref], 'rows': rows,
            'missing_result_qids': sorted(set(by_qid) - {row.get('qid') for row in raw_rows})}
