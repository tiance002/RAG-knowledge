"""Quality adapter called by the existing RSI runner; no alternate scheduler."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import rsi_quality_registry as registry
from rsi_candidates import canonical, read_json, sha


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def source_fingerprint(product_root, source_manifest=None):
    root = Path(product_root).resolve()
    files = sorted((root / 'backend/app').rglob('*.py'))
    files.append(root / 'scripts/rsi_quality_worker.py')
    if not (root / 'backend/app').is_dir() or any(not p.resolve().is_relative_to(root) for p in files):
        raise ValueError('Product source inventory is missing or escapes its root')
    hashes = {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in files}
    if source_manifest is None:
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        return {'root': str(root), 'head': head, 'files': hashes}
    # A code-only export has no .git. Its trusted origin is declared in an
    # explicitly hash-bound manifest, while every actual file is still checked.
    path = Path(source_manifest['path'])
    if not path.is_absolute() or sha(path.read_bytes()) != source_manifest['sha256']:
        raise ValueError('Source manifest fingerprint mismatch')
    manifest = read_json(path)
    head = manifest.get('origin_head', '')
    if (manifest.get('schema') != 'rsi-product-source/v1' or
            not re.fullmatch(r'[0-9a-f]{40}', head) or manifest.get('files') != hashes):
        raise ValueError('Source manifest differs from actual product files')
    return {'root': str(root), 'head': head, 'files': hashes,
            'source_manifest': {'path': str(path), 'sha256': source_manifest['sha256']}}


def verify_source(expected):
    actual = source_fingerprint(expected['root'], expected.get('source_manifest'))
    if actual != expected:
        raise ValueError('Frozen product source changed; use a new version, not mixed results')


def job_for(artifact, inputs_path, repeat, cap, base_url, *, retrieval=None, max_embedding_requests=None,
            authorization_id='owner-rsi-quality-20261105', retry_once=False):
    inputs = read_json(inputs_path)
    ref = inputs['corpus_manifest']
    raw = Path(ref['path']).read_bytes()
    if sha(raw) != ref['sha256']:
        raise ValueError('Corpus manifest hash mismatch')
    corpus = json.loads(raw)
    chunks = {c['id']: c for c in corpus['chunks']}
    for row in inputs['chunks']:
        if sha(chunks[row['id']]['content'].encode()) != row['sha256']:
            raise ValueError('Frozen chunk content changed')
    source_defs = {s['id']: s for s in inputs['sources']}
    for source in source_defs.values():
        if source.get('authorized') is not True or sha(Path(source['path']).read_bytes()) != source['sha256']:
            raise ValueError('Source authorization/hash mismatch')
    sources, selected_chunks, cases, scopes = [], [], [], {}
    # Each distinct frozen source scope is represented by a real isolated KB.
    # A few shared rows repeat in memory; no corpus, DB, or disk index is copied.
    for case in inputs['cases']:
        allowed = case['scope']['chunk_ids']
        ids = list(chunks) if allowed == 'all_80' else allowed
        key = sha(canonical(ids))[:16]
        kb = 'scope-' + key
        if key not in scopes:
            scopes[key] = ids
            for sid in sorted({chunks[c]['source_id'] for c in ids}):
                source = source_defs[sid]
                sources.append({'id': key + '-' + sid, 'kb_id': kb,
                    'path': source['path'], 'sha256': source['sha256'], 'name': source['title']})
            for cid in ids:
                c = chunks[cid]
                selected_chunks.append({'id': key + '-' + cid, 'source_id': key + '-' + c['source_id'],
                    'content': c['content'], 'page_number': c.get('page_number')})
        cases.append({'id': case['qid'], 'question': case['question'], 'history': case.get('history', []),
                      'preferences': case.get('preferences', {}), 'kb_ids': [kb]})
    job = {'schema_version': 'rsi-quality-job-v1', 'dataset_version': inputs['dataset_version'],
        'model': 'Qwen/Qwen3.5-4B', 'provider_base_url': base_url,
        'authorization_id': authorization_id, 'max_model_requests': cap,
        'cases': cases, 'sources': sources, 'chunks': selected_chunks,
        'artifact': artifact, 'repeat': repeat}
    if retrieval is not None:
        if not isinstance(retrieval, dict):
            raise ValueError('explicit retrieval configuration must be an object')
        job['retrieval'] = dict(retrieval)
    if max_embedding_requests is not None:
        if type(max_embedding_requests) is not int or not 0 <= max_embedding_requests <= 100:
            raise ValueError('embedding request budget must be 0..100')
        job['max_embedding_requests'] = max_embedding_requests
    if retry_once:
        if type(retry_once) is not bool:
            raise ValueError('retry_once must be an explicit boolean')
        job['retry_once'] = True
    return job


def provider_key():
    if os.environ.get('SILICONFLOW_API_KEY'):
        return os.environ['SILICONFLOW_API_KEY']
    path = Path(os.environ['RAG_RUNTIME_SECRETS_FILE'])
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            name, value = line.split('=', 1)
            if name.strip() == 'SILICONFLOW_API_KEY' and value.strip():
                return value.strip().strip('\"\'')
    raise ValueError('Cloud Qwen credential missing')


def worker_environment():
    """Only process/runtime essentials cross the isolated worker boundary."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'TMPDIR', 'HOME',
               'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'VIRTUAL_ENV',
               'RAG_TEST_EXTRA_SITE_PACKAGES', 'RSI_QUALITY_EMBEDDING_BASE_URL',
               'RSI_QUALITY_EMBEDDING_MODEL', 'RSI_QUALITY_EMBEDDING_API_KEY',
               'RSI_QUALITY_EMBEDDING_CACHE_DIR'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    return env


def execute_artifact(artifact, inputs_path, output_dir, *, repeat=2, max_model_requests=240,
                     product_root, model_base_url='https://api.siliconflow.cn/v1', live=True,
                     retrieval=None, max_embedding_requests=None, authorization_id='owner-rsi-quality-20261105',
                     retry_once=False):
    out, root = Path(output_dir).resolve(), Path(product_root).resolve()
    out.mkdir(parents=True, exist_ok=False)
    job = job_for(artifact, inputs_path, repeat, max_model_requests, model_base_url,
                  retrieval=retrieval, max_embedding_requests=max_embedding_requests, authorization_id=authorization_id,
                  retry_once=retry_once)
    write(out / 'job.json', job)
    env = worker_environment()
    if live:
        env['SILICONFLOW_API_KEY'] = provider_key()
    cmd = [sys.executable, str(root / 'scripts/rsi_quality_worker.py'), '--input', str(out / 'job.json'),
           '--output', str(out / 'product')]
    if live:
        cmd.append('--live')
    with (out / 'command.log').open('xb') as log:
        process = subprocess.run(cmd, env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    write(out / 'process.json', {'command': cmd, 'exit_code': process.returncode})
    if not live:
        return {'status': 'PREFLIGHT', 'exit_code': process.returncode, 'records': [], 'provider_calls': 0}
    path = out / 'product/results.json'
    if not path.is_file():
        raise RuntimeError('Product worker did not produce records; inspect its retained logs')
    raw = read_json(path)
    records = []
    for row in raw['results']:
        usage = row.get('usage') or {}
        final = row.get('final_request') or {}
        records.append({'qid': row['case_id'], 'repeat_id': row['repeat_id'], 'answer': row.get('answer', ''),
            'preference_resolution': row.get('preference_resolution'),
            'citations': row.get('citations', []), 'status': row['status'], 'done': row.get('done', {}),
            'elapsed_seconds': row.get('elapsed_seconds'), 'input_tokens': usage.get('prompt_tokens'),
            'output_tokens': usage.get('completion_tokens'), 'context_chars': row.get('final_context_chars'),
            'context_bytes': row.get('final_context_bytes'), 'provider_calls': row.get('provider_call_count', 0),
            'prompt_messages_chars': row.get('final_prompt_messages_chars', final.get('prompt_messages_chars')),
            'prompt_messages_bytes': row.get('final_prompt_messages_bytes', final.get('prompt_messages_bytes')),
            'embedding_requests': row.get('embedding_requests'), 'embedding_cache_hits': row.get('embedding_cache_hits'),
            'embedding_usage_tokens': row.get('embedding_usage_tokens'),
            'embedding_calls': row.get('embedding_calls'), 'retrieval_calls': row.get('retrieval_calls'),
            'attempts': row.get('attempts'), 'attempt_count': row.get('attempt_count'),
            'initial_status': row.get('initial_status'), 'retry_status': row.get('retry_status'),
            'known_usage': row.get('known_usage'), 'unknown_usage_call_count': row.get('unknown_usage_call_count'),
            'raw_record': str(out / 'product' / f"record-{row['ordinal']:04d}.json")})
    result = {'run_id': uuid.uuid4().hex, 'status': raw['status'], 'exit_code': process.returncode,
        'records': records, 'provider_calls': raw['model_requests'], 'source_record': str(path),
        'source_sha256': sha(path.read_bytes()), 'cost': 'UNKNOWN', 'business_model': job['model'],
        'embedding_requests': raw.get('embedding_requests'), 'embedding_cache_hits': raw.get('embedding_cache_hits'),
        'embedding_usage_tokens': raw.get('embedding_usage_tokens'), 'embedding_summary': raw.get('embedding_summary'),
        'embedding_status': raw.get('embedding_status'), 'embedding_receipt_path': raw.get('embedding_receipt_path'),
        'retry_once': retry_once, 'retry_summary': raw.get('retry_summary'),
        'model_known_usage': raw.get('model_known_usage'), 'model_unknown_usage_call_count': raw.get('model_unknown_usage_call_count'),
        'embedding_known_usage_tokens': raw.get('embedding_known_usage_tokens'),
        'embedding_unknown_usage_call_count': raw.get('embedding_unknown_usage_call_count')}
    write(out / 'worker.json', result)
    return result


def execute_controlled_artifact(artifact, inputs_path, output_dir, *, product_root,
                                source_fingerprint, repeat=1):
    """Execute a typed F2 artifact through real product code with provider denied.

    The only simulated boundary is the exact approved provider POST. Receipts
    describe compile/wiring observations and must not be scored as model quality.
    """
    from rsi_quality_controlled_worker import verify_approved_source, EVIDENCE_MODE
    registry.validate_artifact(artifact, profile=registry.CONTROLLED_PROFILE)
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError('controlled repeat must be 1..10')
    root, out = Path(product_root).resolve(), Path(output_dir).resolve()
    if out.is_relative_to(root):
        raise ValueError('Controlled output must be outside the formal checkout')
    if Path(source_fingerprint['root']).resolve() != root:
        raise ValueError('Controlled product root differs from bound fingerprint')
    verify_source(source_fingerprint)
    approved = verify_approved_source(root)
    # Keep candidate retrieval out of the legacy worker artifact schema.
    legacy = {key: artifact[key] for key in ('prompt_addendum', 'memory_addendum', 'workflow')}
    inputs = read_json(inputs_path)
    cap = len(inputs['cases']) * repeat * 4
    job = job_for(legacy, inputs_path, repeat, cap, 'https://api.siliconflow.cn/v1',
                  retrieval=artifact['retrieval'], authorization_id='f2-offline-stub-only')
    out.mkdir(parents=True, exist_ok=False)
    write(out / 'job.json', job)
    # No secrets, embedding configuration, user Python paths or proxy variables.
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LOCALAPPDATA',
               'APPDATA', 'USERPROFILE'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               PYTHONNOUSERSITE='1', SILICONFLOW_API_KEY='f2-offline-fake-token')
    script = Path(__file__).with_name('rsi_quality_controlled_worker.py').resolve()
    cmd = [sys.executable, '-B', str(script), str(root), str(out / 'job.json'), str(out / 'isolated')]
    try:
        with (out / 'command.log').open('xb') as log:
            process = subprocess.run(cmd, env=env, cwd=out, stdout=log,
                                     stderr=subprocess.STDOUT, timeout=300)
    finally:
        verify_source(source_fingerprint)
        if verify_approved_source(root) != approved:
            raise ValueError('Approved product source changed during controlled execution')
    write(out / 'process.json', {'command': cmd, 'exit_code': process.returncode})
    product = out / 'isolated/product'
    report_path = product / 'results.json'
    receipt_path = out / 'isolated/controlled-receipt.json'
    if not report_path.is_file() or not receipt_path.is_file():
        raise RuntimeError('Controlled worker failed; inspect retained command.log')
    raw, receipt = read_json(report_path), read_json(receipt_path)
    if len(raw['results']) != len(inputs['cases']) * repeat:
        raise ValueError('Controlled result denominator differs from planned records')
    observations = iter(receipt['retrieval_observations'])
    records = []
    stub_receipts = {row['actual_request_sha256']: row for row in receipt['provider_stub_calls']}
    for row in raw['results']:
        final = row.get('final_request') or {}
        request_file = final.get('request_file')
        final_sha, wire_sha = None, None
        if request_file:
            request_path = (product / request_file).resolve(strict=True)
            if not request_path.is_relative_to(product.resolve()):
                raise ValueError('Final request receipt escapes isolated product output')
            known_paths = {(product / call['request_file']).resolve()
                           for call in row.get('provider_calls', [])}
            if request_path not in known_paths:
                raise ValueError('Final request receipt is not assigned to this record')
            request = read_json(request_path)
            actual = request['actual_request']
            wire = request['actual_request_utf8'].encode('utf-8')
            if json.loads(wire) != actual:
                raise ValueError('Actual request UTF-8 differs from recorded request')
            final_sha = sha(canonical(actual['messages']))
            wire_sha = sha(wire)
            observed = stub_receipts.get(wire_sha)
            if not observed or observed['messages_sha256'] != final_sha:
                raise ValueError('Final messages not bound to actual provider-stub request')
        calls = [next(observations) for _ in row.get('retrieval_calls', [])]
        if any(call['case_id'] != row['case_id'] for call in calls):
            raise ValueError('Retrieval receipt case order mismatch')
        record = {'qid': row['case_id'], 'repeat_id': row['repeat_id'], 'status': row['status'],
                  'answer': row.get('answer', ''), 'citations': row.get('citations', []),
                  'done': row.get('done'), 'evidence_mode': EVIDENCE_MODE,
                  'actual_external_calls': 0, 'input_tokens': None, 'output_tokens': None,
                  'final_prompt_messages_sha256': final_sha, 'actual_request_sha256': wire_sha,
                  'observed_top_k': calls[-1]['observed_top_k'] if calls else None,
                  'effective_config_sha256': calls[-1]['effective_config_sha256'] if calls else None,
                  'retrieval_observations': calls,
                  'raw_record': str(product / f"record-{row['ordinal']:04d}.json")}
        records.append(record)
    if next(observations, None) is not None:
        raise ValueError('Unassigned retrieval observations')
    result = {'status': raw['status'], 'exit_code': process.returncode,
              'evidence_mode': EVIDENCE_MODE, 'actual_external_calls': 0,
              'provider_stub_calls': len(receipt['provider_stub_calls']),
              'provider_calls': 0, 'records': records, 'cost': 'NOT_MEASURED_OFFLINE',
              'source_record': str(report_path), 'source_sha256': sha(report_path.read_bytes()),
              'controlled_receipt': str(receipt_path), 'approved_source': approved,
              'runtime_identity': receipt['runtime_identity'],
              'retrieval_config_sha256': sha(canonical(artifact['retrieval']))}
    write(out / 'worker.json', result)
    return result


def run_quality(manifest_path, out):
    checked = registry.verify_manifest(manifest_path)
    manifest = checked['manifest']
    verify_source(manifest['source_fingerprint'])
    execution = manifest['execution']
    baseline = Path(execution['baseline_run']).resolve()
    base_manifest = read_json(baseline / 'manifest.json')
    for field in ('source_fingerprint', 'fixed', 'cases', 'evaluation'):
        if base_manifest[field] != manifest[field]:
            raise ValueError('Cached baseline is not comparable')
    base_arm = next(iter(base_manifest['arms'].values()))
    if base_arm['candidate_id'] != manifest['arms']['A']['candidate_id']:
        raise ValueError('Cached A does not evaluate the direct parent')
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    write(out / 'manifest.json', manifest)
    write(out / 'baseline-reuse.json', {'path': str(baseline), 'status': 'REUSED',
        'results_sha256': sha((baseline / 'results.json').read_bytes()), 'new_model_requests': 0})
    result = execute_artifact(checked['artifacts']['B'], manifest['cases']['path'], out / 'B',
        repeat=execution.get('repeat', 2), max_model_requests=execution['max_model_requests'],
        product_root=execution['product_root'], model_base_url=execution['base_url'],
        retrieval=manifest['fixed'].get('retrieval') if isinstance(manifest['fixed'].get('retrieval'), dict) else None,
        max_embedding_requests=execution.get('max_embedding_requests'),
        authorization_id=execution.get('authorization_id', 'owner-rsi-quality-20261105'),
        retry_once=execution.get('retry_once', False))
    verify_source(manifest['source_fingerprint'])
    write(out / 'worker.json', result)
    return result
