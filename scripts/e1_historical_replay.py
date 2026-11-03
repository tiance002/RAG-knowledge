"""Frozen E1 imports and historical replay. Never import or invoke the RAG app.

Default command is local-only. Cloud execution requires a separately approved,
hash-bound manifest and two genuine human FAIL labels. Existing E1 files are
read-only; all outputs must go into a new directory.
"""
import argparse
import ast
from collections import Counter
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
E1 = ROOT / 'var/e1-20261029'
DATASET = 'rag/e1-judge-calibration-v1'
HOST = 'https://us.cloud.langfuse.com'
PROJECT = 'cmuido3u800zyad0con8gaqw1'
QIDS = ['office50:001', 'office50:011',
        'mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0958',
        'mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0431',
        'longbench-zh:dureader:9938c97e6ccf006c5fc66ea3294b9835e1584d95713576a9']


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def rows(path):
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_rows(path, values):
    Path(path).write_bytes(b''.join(canonical(v) + b'\n' for v in values))


def checked_file(relative, digest):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(E1.resolve()):
        raise ValueError('historical evidence outside E1 directory')
    if sha(path.read_bytes()) != digest:
        raise ValueError(f'historical hash mismatch: {relative}')
    return path


def extract_answer(path):
    chunks, done, result = [], [], []
    for block in path.read_text(encoding='utf-8').split('\n\n'):
        lines = block.splitlines()
        event = next((s[6:].strip() for s in lines if s.startswith('event:')), None)
        raw = '\n'.join(s[5:].lstrip() for s in lines if s.startswith('data:'))
        if not event:
            continue
        data = json.loads(raw)
        if event == 'error':
            raise ValueError('historical SSE contains error')
        if event == 'answer_chunk':
            if not isinstance(data.get('content'), str):
                raise ValueError('invalid answer chunk')
            chunks.append(data['content'])
        if event == 'done':
            done.append(data)
        if event == 'rag_result':
            result.append(data)
    if len(done) != 1 or not result or not ''.join(chunks).strip():
        raise ValueError('incomplete historical SSE')
    return ''.join(chunks)


def prompt_identity(case_dir, question):
    matches = []
    for path in (case_dir / 'private-snapshots').glob('*.txt'):
        try:
            obj = ast.literal_eval(path.read_text(encoding='utf-8'))
        except (ValueError, SyntaxError):
            continue
        if (isinstance(obj, list) and len(obj) >= 2 and isinstance(obj[0], dict)
                and isinstance(obj[1], dict) and '资料：\n[1]' in obj[0].get('content', '')
                and obj[1].get('role') == 'user'):
            if obj[1].get('content') != question:
                raise ValueError('historical question mismatch')
            matches.append({'snapshot_sha256': sha(path.read_bytes()),
                            'messages_sha256': sha(canonical(obj)),
                            'template_prefix_sha256': sha(obj[0]['content'].split('资料：\n', 1)[0].encode()),
                            'prompt_version': 'NOT_CAPTURED',
                            'snapshot': str(path.relative_to(ROOT))})
    if len(matches) != 1:
        raise ValueError('expected exactly one original prompt snapshot')
    return matches[0]


def scan_sensitive(value):
    def strings(obj):
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, dict):
            return [s for v in obj.values() for s in strings(v)]
        if isinstance(obj, list):
            return [s for v in obj for s in strings(v)]
        return []
    # Scan actual strings, not JSON escape sequences such as "is:\\n".
    text = '\n'.join(strings(value))
    patterns = {
        'credential': r'(?i)(?:sk-(?:lf-|proj-)?[a-z0-9_-]{16,}|bearer\s+[a-z0-9_.-]{12,}|-----BEGIN .*PRIVATE KEY)',
        'email': r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
        'cn_identity': r'(?<!\d)\d{17}[\dXx](?!\d)',
        'cn_mobile': r'(?<!\d)1[3-9]\d{9}(?!\d)',
        'local_path': r'(?i)(?<![a-z])[a-z]:[\\/]|/opt/',
    }
    return {name: len(re.findall(pattern, text)) for name, pattern in patterns.items()}


def prepare(output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('output must be a new directory; refusing overwrite')
    ledger = rows(E1 / 'ledger-v4.jsonl')
    candidates = {r['qid']: r for r in rows(E1 / 'e1_candidates.jsonl')}
    if len(ledger) != 30 or len({r['qid'] for r in ledger}) != 30:
        raise ValueError('expected 30 unique ledger records')
    # Verify the full denominator, not only convenient selected items.
    for row in ledger:
        for field in ['sse', 'result']:
            checked_file(row[field], row[field + '_sha256'])
        if row.get('events_file'):
            checked_file(row['events_file'], row['events_sha256'])
    by_qid = {r['qid']: r for r in ledger}
    ai = {r['qid']: r for r in rows(E1 / 'review6-independent-20261029.jsonl')}
    office = {r['qid']: r for r in rows(E1 / 'office-source-verification.jsonl')}
    source_answers = {r['qid']: r for r in rows(E1 / 'e1-source-answers.jsonl')}
    preflight = json.loads((E1 / 'preflight-fulltext-r3.json').read_text(encoding='utf-8'))
    items, replay, preview = [], [], []
    for qid in QIDS:
        row, candidate = by_qid[qid], candidates[qid]
        sse = ROOT / row['sse']
        answer = extract_answer(sse)
        case = row['result_case']
        actual_cases = json.loads((ROOT / row['result']).read_text(encoding='utf-8'))['cases']
        if len([r for r in actual_cases if r == case]) != 1:
            raise ValueError('result case differs from frozen ledger')
        if (case['qid'] != qid or case['http_status'] != 200 or not case['sse_complete']
                or case['http_terminal_state'] != 'SSE_DONE' or row['execution'] != 'COMPLETED'):
            raise ValueError('historical request not completed')
        if len(case['trace_ids']) != 1:
            raise ValueError('ambiguous trace identity')
        historical = row.get('prior_ledger_record', {}).get('current_answer')
        if historical is None:
            historical = source_answers.get(qid, {}).get('answer') or next(
                (r['answer'] for r in rows(E1 / 'e1-remaining6-answers.jsonl') if r['qid'] == qid), None)
        if historical != answer:
            raise ValueError('SSE differs from saved historical answer')
        prompt = prompt_identity(sse.parent, candidate['question'])
        events_path = ROOT / row['events_file'] if row.get('events_file') else sse.parent / 'events.jsonl'
        events_hash = sha(events_path.read_bytes())
        events = rows(events_path)
        if not any(e['trace_id'] == case['trace_ids'][0] and e['stage'] == 'qa.request' for e in events):
            raise ValueError('original trace not found in local events')
        if qid in office:
            evidence = office[qid]
            source = {'sha256': evidence['actual_sha256'], 'location': evidence['location'],
                      'document': Path(evidence['source_path']).name,
                      'verification': 'PRIOR_FROZEN_SOURCE_AUDIT', 'audit_file': 'office-source-verification.jsonl'}
            if not evidence['hash_match'] or evidence['gold_frozen'] != candidate['gold'][0]:
                raise ValueError('office Gold/source mismatch')
        elif candidate['format'] == 'PDF':
            evidence = candidate['evidence']['historical_followup_pdf']
            path = Path(evidence['source'])
            if sha(path.read_bytes()) != evidence['source_sha256']:
                raise ValueError('PDF source hash mismatch')
            source = {'sha256': evidence['source_sha256'], 'page_one_based': evidence['page'],
                      'document': path.name, 'verification': 'LOCAL_FILE_HASH_AND_PRIOR_PAGE_AUDIT'}
        else:
            evidence = source_answers[qid]['source_verification']
            if source_answers[qid]['gold'] != candidate['gold']:
                raise ValueError('text Gold mismatch')
            source = {'sha256': evidence['source_sha256'], 'record_id': qid.split(':', 1)[1],
                      'verification': 'PRIOR_FROZEN_SOURCE_AUDIT', 'audit_file': 'e1-source-answers.jsonl'}
        human = row['human_core']
        review = {'human_core': human, 'human_basis': row['human_core_basis'],
                  'assessor': 'USER' if human in ['PASS', 'FAIL'] else ai[qid]['assessor'],
                  'ai_core': ai[qid]['core_judgment'] if qid in ai else None,
                  'source': 'ledger-v4.jsonl', 'source_sha256': sha((E1 / 'ledger-v4.jsonl').read_bytes())}
        unit = '%' if qid == 'office50:011' else 'layers (including input)' if qid.endswith('row-0431') else 'hours, UTC+08:00' if qid.startswith('longbench-zh') else None
        metadata = {'document_type': candidate['format'], 'gold_granularity': 'field' if qid.startswith('office50') else 'reference_answer',
                    'dataset_version': 'e1-20261029/candidates-sha256:' + sha((E1 / 'e1_candidates.jsonl').read_bytes()),
                    'source_identity': source, 'human_review': review, 'unit': unit,
                    'cloud_authorization': 'PENDING', 'execution_type': 'HISTORICAL_REPLAY'}
        item = {'input': {'qid': qid, 'question': candidate['question']},
                'expected_output': {'reference_answer': candidate['gold'], 'unit': unit}, 'metadata': metadata}
        record = {'qid': qid, 'run_id': sse.parent.parent.name, 'answer': answer,
                  'answer_hash': sha(answer.encode('utf-8')), 'original_trace_id': case['trace_ids'][0],
                  'historical_model': preflight['models']['wire_model'],
                  'historical_code_version': 'app-sha256:' + preflight['code_sha256'],
                  'historical_config': case['request_config'], 'historical_prompt': prompt, 'review': review,
                  'sse': row['sse'], 'sse_sha256': row['sse_sha256'],
                  'result': row['result'], 'result_sha256': row['result_sha256'],
                  'events_file': str(events_path.relative_to(ROOT)), 'events_sha256': events_hash,
                  'events_hash_basis': 'EXISTING_LEDGER' if row.get('events_file') else 'NEW_FREEZE_OF_EXISTING_EVENTS',
                  'dataset_item_sha256': sha(canonical(item)), 'source_identity': source}
        review_case = {'input': {'question': candidate['question'], 'reference_answer': candidate['gold']},
                       'output': {'answer': answer}}
        scan = scan_sensitive({'dataset_item': item, 'review_case': review_case})
        items.append(item); replay.append(record)
        preview.append({'qid': qid, 'human_core': human, 'ai_core': review['ai_core'],
                        'source': source, 'dataset_item_bytes': len(canonical(item)),
                        'observation_io_bytes': len(canonical(review_case)), 'answer_utf8_bytes': len(answer.encode()),
                        'answer_hash': record['answer_hash'], 'sensitive_pattern_counts': scan,
                        'cloud_authorization': 'PENDING'})
    output.mkdir(parents=True)
    write_rows(output / 'dataset-items.jsonl', items)
    write_rows(output / 'historical-answers.jsonl', replay)
    hashes = {name: sha((output / name).read_bytes()) for name in ['dataset-items.jsonl', 'historical-answers.jsonl']}
    counts = dict(Counter(r['human_core'] for r in preview))
    summary = {'status': 'BLOCKED', 'dataset_name': DATASET, 'planned_items': len(items),
               'full_ledger_sse_result_verified': 30, 'existing_event_hashes_verified': sum(bool(r.get('events_file')) for r in ledger),
               'human_counts': counts, 'ai_fail_candidates': 1,
               'blockers': ['CLOUD_BODY_UPLOAD_NOT_AUTHORIZED', 'SECOND_HUMAN_FAIL_MISSING'],
               'external_model_calls': 0, 'cloud_writes': 0, 'files_sha256': hashes, 'items': preview,
               'scan_scope': 'All proposed Dataset fields and eval.review_case input/output; regex scan is not permission or exhaustive PII review.',
               'source_limitations': 'DOCX/text source bytes rely on prior frozen audits; PDFs rehashed locally. Gold preserved verbatim, no regeneration.',
               'current_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'sdk_version': importlib.metadata.version('langfuse')}
    write_json(output / 'dry-run.json', summary)
    write_json(output / 'authorization.pending.json', {'approved': False, 'destination': HOST, 'project_id': PROJECT,
               'dataset_name': DATASET, 'files_sha256': hashes, 'qids': QIDS,
               'scope': ['question', 'frozen_gold', 'historical_answer', 'source_and_review_metadata'],
               'authorization_evidence': None})
    text = ['# E1 历史回放待审材料', '', 'Cloud 写入尚未授权；原账本未修改。第二道人审 FAIL 尚缺失。', '']
    for item, record in zip(items, replay):
        text += ['## ' + record['qid'], '', '**原问题**：' + item['input']['question'], '',
                 '**冻结 Gold**：' + json.dumps(item['expected_output'], ensure_ascii=False), '',
                 '**原审核**：' + json.dumps(record['review'], ensure_ascii=False), '',
                 '**历史答案原文**：', '', record['answer'], '', '**答案 SHA-256**：`' + record['answer_hash'] + '`', '']
    (output / 'review-cases.md').write_text('\n'.join(text), encoding='utf-8')
    return summary


def validate_bundle(bundle):
    bundle = Path(bundle)
    summary = json.loads((bundle / 'dry-run.json').read_text(encoding='utf-8'))
    for name, digest in summary['files_sha256'].items():
        if sha((bundle / name).read_bytes()) != digest:
            raise ValueError('bundle hash mismatch')
    items, replay = rows(bundle / 'dataset-items.jsonl'), rows(bundle / 'historical-answers.jsonl')
    if len(items) != 5 or [i['input']['qid'] for i in items] != QIDS or [r['qid'] for r in replay] != QIDS:
        raise ValueError('bundle membership mismatch')
    for item, record in zip(items, replay):
        if sha(canonical(item)) != record['dataset_item_sha256'] or sha(record['answer'].encode()) != record['answer_hash']:
            raise ValueError('item/answer hash mismatch')
        if extract_answer(checked_file(record['sse'], record['sse_sha256'])) != record['answer']:
            raise ValueError('original answer mismatch')
    return items, replay


def make_task(client, items, replay):
    by_qid = {r['qid']: (i, r) for i, r in zip(items, replay)}
    def task(*, item, **kwargs):
        data = item['input'] if isinstance(item, dict) else item.input
        expected = item.get('expected_output') if isinstance(item, dict) else item.expected_output
        original, record = by_qid[data['qid']]
        if data != original['input'] or expected != original['expected_output']:
            raise ValueError('dataset differs from frozen bundle')
        metadata = {key: record[key] for key in ['qid', 'run_id', 'original_trace_id', 'historical_model',
                                                'historical_code_version', 'historical_prompt', 'answer_hash', 'review']}
        metadata.update(execution_type='HISTORICAL_REPLAY', real_model_run=False, external_model_calls=0)
        client.update_current_span(metadata=metadata)
        with client.start_as_current_observation(name='eval.review_case', as_type='span',
                input={'question': data['question'], 'reference_answer': expected['reference_answer']},
                output={'answer': record['answer']}, metadata=metadata):
            return {'answer': record['answer']}
    return task


@contextmanager
def deny_network(allowed_host=None):
    """Deny external network, or restrict replay to one Langfuse host:443."""
    from unittest.mock import patch
    attempts = []
    local = threading.local()
    original_pair, original_connect, original_dns = socket.socketpair, socket.socket.connect, socket.getaddrinfo
    allowed_ips = set()
    def denied(*args, **kwargs):
        attempts.append({'kind': 'blocked_endpoint', 'endpoint': str(args[-1] if len(args) > 1 else args)})
        raise RuntimeError('network endpoint blocked during replay')
    def connect(sock, address):
        if getattr(local, 'ipc_pair', False):
            return original_connect(sock, address)
        if allowed_host and isinstance(address, tuple) and address[0] in allowed_ips and address[1] == 443:
            return original_connect(sock, address)
        return denied(sock, address)
    def dns(host, port, *args, **kwargs):
        if allowed_host and host == allowed_host and int(port) == 443:
            result = original_dns(host, port, *args, **kwargs)
            allowed_ips.update(r[4][0] for r in result)
            return result
        return denied(host, port)
    def socketpair(*args, **kwargs):
        # Windows asyncio implements its private wakeup pipe as a socketpair.
        local.ipc_pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            local.ipc_pair = False
    with patch.object(socket, 'getaddrinfo', dns), patch.object(socket.socket, 'connect', connect), patch.object(socket, 'socketpair', socketpair):
        yield attempts


def cloud_environment():
    env = os.environ.copy()
    for line in (ROOT / 'backend/.env.langfuse.local').read_text(encoding='utf-8-sig').splitlines():
        key, sep, value = line.partition('=')
        if sep and key in ['LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL']:
            env[key] = value.strip().strip('\"\'')
    if env.get('LANGFUSE_BASE_URL', '').rstrip('/') != HOST:
        raise ValueError('unexpected Langfuse destination')
    env['LANGFUSE_HOST'] = HOST
    return env


def cli(bundle, label, args, allow_404=False):
    directory = Path(bundle) / 'cloud-cli'
    directory.mkdir(exist_ok=True)
    if (directory / (label + '.json')).exists():
        raise ValueError('refusing to overwrite CLI evidence')
    result = subprocess.run(['C:/Program Files/nodejs/npx.cmd', '--yes', 'langfuse-cli', 'api', *args, '--json'],
        env=cloud_environment(), capture_output=True, encoding='utf-8', timeout=120)
    (directory / (label + '.json')).write_text(result.stdout, encoding='utf-8')
    (directory / (label + '.stderr.log')).write_text(result.stderr, encoding='utf-8')
    write_json(directory / (label + '.exit.json'), {'exit_code': result.returncode, 'args': args})
    if not result.stdout:
        raise RuntimeError('CLI empty output: ' + label)
    envelope = json.loads(result.stdout)
    if allow_404 and envelope.get('status') == 404:
        return None
    if result.returncode or envelope.get('status') != 200:
        raise RuntimeError('CLI failed: ' + label + '; inspect preserved raw evidence')
    return envelope['body']


def authorized_bundle(bundle):
    items, replay = validate_bundle(bundle)
    approval = json.loads((Path(bundle) / 'authorization.approved.json').read_text(encoding='utf-8'))
    summary = json.loads((Path(bundle) / 'dry-run.json').read_text(encoding='utf-8'))
    if (approval.get('approved') is not True or approval.get('destination') != HOST
            or approval.get('project_id') != PROJECT or approval.get('dataset_name') != DATASET
            or approval.get('files_sha256') != summary['files_sha256'] or approval.get('qids') != QIDS
            or not approval.get('authorization_evidence')):
        raise ValueError('Cloud body authorization absent or mismatched')
    supplemental = approval['supplemental_human_review']
    count = Counter()
    for item, record in zip(items, replay):
        item['metadata']['cloud_authorization'] = 'APPROVED_FOR_THIS_FROZEN_IMPORT'
        effective = record['review']['human_core']
        if record['qid'] == supplemental['qid']:
            if supplemental['answer_hash'] != record['answer_hash'] or supplemental['human_core'] != 'FAIL':
                raise ValueError('supplemental review not bound to historical answer')
            record['review']['supplemental_human_review'] = supplemental
            item['metadata']['human_review']['supplemental_human_review'] = supplemental
            effective = supplemental['human_core']
        item['metadata']['effective_human_core'] = effective
        count[effective] += 1
    if count['PASS'] < 2 or count['FAIL'] < 2:
        raise ValueError('requires two human PASS and two human FAIL')
    return items, replay


def cloud_import(bundle):
    items, replay = authorized_bundle(bundle)
    project = cli(bundle, 'project-check', ['projects', 'list'])
    if PROJECT not in [r['id'] for r in project['data']]:
        raise ValueError('credential project mismatch')
    rules = cli(bundle, 'rules-check', ['evaluation-rules', 'list', '--all'])
    if rules['data'] or rules.get('meta', {}).get('cursor'):
        raise ValueError('existing evaluation rules require inspection before import/replay')
    dataset = cli(bundle, 'dataset-absence', ['datasets', 'get', DATASET], allow_404=True)
    if dataset is not None:
        raise ValueError('dataset already exists; refusing any overwrite')
    body = {'name': DATASET, 'description': 'E1 frozen question/Gold calibration; historical answers replayed separately; 3 human PASS / 2 human FAIL; no RAG or Judge calls.',
            'metadata': {'execution_type': 'HISTORICAL_REPLAY', 'import_bundle_sha256': sha((Path(bundle) / 'dataset-items.jsonl').read_bytes()),
                         'human_labels': {'PASS': 3, 'FAIL': 2}, 'original_ledger_preserved': True}}
    write_json(Path(bundle) / 'dataset-create.json', body)
    dataset = cli(bundle, 'dataset-create', ['datasets', 'create', '--body-file', str(Path(bundle) / 'dataset-create.json')])
    for n, (item, record) in enumerate(zip(items, replay)):
        payload = {'datasetName': DATASET, 'input': item['input'], 'expectedOutput': item['expected_output'],
                   'metadata': item['metadata'], 'sourceTraceId': record['original_trace_id'], 'status': 'ACTIVE'}
        path = Path(bundle) / f'item-create-{n}.json'
        write_json(path, payload)
        cli(bundle, f'item-create-{n}', ['dataset-items', 'create', '--body-file', str(path)])
    report = {'status': 'PASS', 'dataset_id': dataset['id'], 'dataset_name': DATASET, 'items_uploaded': len(items),
              'dataset_url': f'{HOST}/project/{PROJECT}/datasets/{dataset["id"]}', 'external_model_calls': 0,
              'scope': 'CLI mutation results; SDK/API readback and UI checks follow.'}
    write_json(Path(bundle) / 'cloud-import-result.json', report)
    return report


def cloud_replay(bundle, attempt='v1'):
    import httpx
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    items, replay = authorized_bundle(bundle)
    if not re.fullmatch(r'v\d+', attempt):
        raise ValueError('invalid attempt identity')
    reservation = Path(bundle) / ('cloud-replay-reserved-' + attempt + '.json')
    with reservation.open('x', encoding='utf-8') as handle:
        json.dump({'state': 'RESERVED_NO_AUTOMATIC_RETRY', 'bundle_sha256': sha((Path(bundle) / 'historical-answers.jsonl').read_bytes())}, handle)
    env = cloud_environment()
    rules = cli(bundle, 'rules-before-replay-' + attempt, ['evaluation-rules', 'list', '--all'])
    if rules['data'] or rules.get('meta', {}).get('cursor'):
        raise ValueError('automatic evaluation rules present; replay stopped')
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with deny_network('us.cloud.langfuse.com') as blocked:
        client = Langfuse(public_key=env['LANGFUSE_PUBLIC_KEY'], secret_key=env['LANGFUSE_SECRET_KEY'],
                          base_url=HOST, tracer_provider=provider, sample_rate=1.0,
                          environment='e1-historical-replay', httpx_client=httpx.Client(trust_env=False, timeout=30))
        try:
            dataset = client.get_dataset(DATASET)
            actual = {i.input['qid']: i for i in dataset.items}
            if set(actual) != set(QIDS):
                raise ValueError('Cloud dataset membership differs from approved bundle')
            for item in items:
                row = actual[item['input']['qid']]
                if row.input != item['input'] or row.expected_output != item['expected_output'] or row.metadata != item['metadata']:
                    raise ValueError('Cloud dataset fields differ from approved bundle')
            write_json(Path(bundle) / 'dataset-readback.json', {'dataset_id': dataset.id,
                'items': [i.model_dump(mode='json') for i in dataset.items], 'field_equality': 'PASS'})
            result = dataset.run_experiment(name='E1 historical answer replay ' + attempt, run_name='e1-historical-replay-20261029-' + attempt,
                description='Frozen historical RAG answers; HISTORICAL_REPLAY, zero new model calls, no evaluators.',
                task=make_task(client, items, replay), evaluators=[], run_evaluators=[], max_concurrency=1,
                metadata={'execution_type': 'HISTORICAL_REPLAY', 'real_model_run': False, 'external_model_calls': 0})
            if len(result.item_results) != 5:
                raise ValueError('SDK silently filtered failed experiment items')
            results_by_qid = {r.item.input['qid']: r for r in result.item_results}
            for record in replay:
                row = results_by_qid[record['qid']]
                if row.output != {'answer': record['answer']} or row.evaluations:
                    raise ValueError('SDK replay output differs or scoring unexpectedly occurred')
            client.flush()
            observations = []
            for span in exporter.get_finished_spans():
                if span.name == 'eval.review_case':
                    observations.append({'trace_id': format(span.context.trace_id, '032x'),
                        'observation_id': format(span.context.span_id, '016x'), 'input': json.loads(span.attributes['langfuse.observation.input']),
                        'output': json.loads(span.attributes['langfuse.observation.output'])})
            write_rows(Path(bundle) / ('cloud-replay-observations-' + attempt + '.local.jsonl'), observations)
            write_json(Path(bundle) / ('network-guard-' + attempt + '.json'), {'blocked': blocked, 'allowed_host': 'us.cloud.langfuse.com', 'allowed_port': 443})
            if len(observations) != 5 or blocked:
                raise ValueError('observation count mismatch or forbidden network attempted')
            report = {'status': 'SDK_EXECUTED_CLOUD_READBACK_PENDING', 'experiment_id': result.experiment_id,
                      'experiment_url': result.experiment_url, 'items': 5, 'external_model_calls': 0,
                      'blocked_network_attempts': len(blocked), 'judge_calls': 0,
                      'item_results': [{'qid': qid, 'trace_id': row.trace_id,
                                       'trace_url': f'{HOST}/project/{PROJECT}/traces/{row.trace_id}'} for qid, row in results_by_qid.items()]}
            write_json(Path(bundle) / 'cloud-replay-result.json', report)
            return report
        finally:
            client.shutdown()


def sdk_check(bundle):
    import httpx
    from datetime import datetime, timezone
    from langfuse import Langfuse
    from langfuse._client.datasets import DatasetClient
    from langfuse.api.commons.types.dataset import Dataset
    from langfuse.api.commons.types.dataset_item import DatasetItem
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    items, replay = validate_bundle(bundle)
    exporter = InMemorySpanExporter()
    now = datetime.now(timezone.utc)
    mock_requests = []
    def offline_http(request):
        mock_requests.append({'method': request.method, 'path': request.url.path})
        if request.method == 'GET' and request.url.path == '/api/public/projects':
            return httpx.Response(200, json={'data': []})
        raise ValueError('unexpected SDK HTTP request')
    with deny_network() as attempts:
        client = Langfuse(public_key='pk-lf-e1-offline-replay', secret_key='offline-placeholder',
                          base_url='http://127.0.0.1:1', tracer_provider=TracerProvider(),
                          span_exporter=exporter, environment='e1-historical-replay-offline',
                          httpx_client=httpx.Client(transport=httpx.MockTransport(offline_http)))
        dataset = DatasetClient(dataset=Dataset(id='offline-dataset', name=DATASET, project_id='offline-project',
            metadata={}, created_at=now, updated_at=now),
            items=[DatasetItem(id='offline-' + str(n), dataset_id='offline-dataset', dataset_name=DATASET,
                input=i['input'], expected_output=i['expected_output'], metadata=i['metadata'],
                status='ACTIVE', created_at=now, updated_at=now, source_trace_id=None, source_observation_id=None,
                media_references=[])
                for n, i in enumerate(items)], langfuse_client=client)
        result = dataset.run_experiment(name='E1 HISTORICAL_REPLAY offline SDK verification',
            experiment_id='offline-e1-replay', run_name='offline-only', task=make_task(client, items, replay),
            evaluators=[], run_evaluators=[], max_concurrency=1,
            metadata={'execution_type': 'HISTORICAL_REPLAY', 'real_model_run': False})
        client.flush()
        spans = exporter.get_finished_spans()
        cases = [s for s in spans if s.name == 'eval.review_case']
        if len(result.item_results) != 5 or len(cases) != 5:
            raise ValueError('SDK item/observation count mismatch')
        extracted = []
        for span in cases:
            attributes = dict(span.attributes)
            inp, out = json.loads(attributes['langfuse.observation.input']), json.loads(attributes['langfuse.observation.output'])
            record = next(r for r in replay if r['answer'] == out['answer'])
            original = next(i for i in items if i['input']['qid'] == record['qid'])
            if inp != {'question': original['input']['question'], 'reference_answer': original['expected_output']['reference_answer']}:
                raise ValueError('SDK observation input changed')
            extracted.append({'qid': record['qid'], 'input': inp, 'output': out,
                              'type': attributes.get('langfuse.observation.type'), 'answer_hash': sha(out['answer'].encode())})
        for result_item, record in zip(result.item_results, replay):
            if result_item.output != {'answer': record['answer']} or result_item.evaluations:
                raise ValueError('SDK output mismatch or unexpected evaluation')
        client.shutdown()
    if attempts:
        raise ValueError('offline SDK attempted network; inspect SDK behavior')
    if any(s.attributes.get('langfuse.observation.type') in ['generation', 'embedding', 'evaluator'] for s in spans):
        raise ValueError('unexpected model/evaluator observation')
    report = {'status': 'PASS', 'sdk_version': importlib.metadata.version('langfuse'), 'items': 5,
              'review_observations': 5, 'spans': len(spans), 'network_attempts': len(attempts),
              'external_model_calls': 0, 'judge_calls': 0, 'cloud_writes': 0,
              'mock_http_requests': mock_requests,
              'scope': 'Real installed SDK DatasetClient runner + in-memory exporter; offline dataset object, not Cloud/UI acceptance.'}
    write_json(Path(bundle) / 'sdk-offline-result.json', report)
    write_rows(Path(bundle) / 'sdk-observation-readback.local.jsonl', extracted)
    return report


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'sdk-check', 'cloud-import', 'cloud-replay'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--attempt', default='v1')
    args = parser.parse_args()
    action = {'prepare': prepare, 'sdk-check': sdk_check, 'cloud-import': cloud_import, 'cloud-replay': cloud_replay}[args.command]
    result = action(args.output, args.attempt) if args.command == 'cloud-replay' else action(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
