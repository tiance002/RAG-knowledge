"""Curate and replay Judge inputs without running RAG or any Judge model.

Expected labels/reasons stay in a local calibration ledger, never in Cloud data.
Only sources explicitly approved in the previous E1 import are reused.
"""
import argparse
from collections import Counter
import copy
import json
import os
from pathlib import Path
import sys

import e1_historical_replay as base

ROOT = base.ROOT
PREVIOUS = ROOT / 'var/e1-historical-replay-20261029-r2'
NAME = 'rag/judge-calibration-v1'
BANNED = {'expected_verdict', 'review_reason', 'coverage', 'label_status', 'rubric_notes'}


def assert_no_labels(obj):
    if isinstance(obj, dict):
        if BANNED.intersection(obj):
            raise ValueError('calibration answer label leaked into Cloud payload')
        for value in obj.values():
            assert_no_labels(value)
    elif isinstance(obj, list):
        for value in obj:
            assert_no_labels(value)


def prepare(directory):
    directory = Path(directory)
    if (directory / 'calibration-ledger.jsonl').exists():
        raise ValueError('refusing to overwrite frozen calibration ledger')
    items, historical = base.authorized_bundle(PREVIOUS)
    by_qid = {item['input']['qid']: (item, record) for item, record in zip(items, historical)}
    office, mirror, utc = 'office50:011', base.QIDS[2], base.QIDS[4]
    # Curated labels are proposals, not fabricated user reviews. Original labels
    # are retained independently even where this new rubric requires review.
    definitions = [
        ('C01', office, None, 'PASS', '历史答案准确给出7%；附带金额来自既有来源审查，不因Gold只列7%就认定其无依据。', ['numeric_correct']),
        ('C02', office, 'The percentage of work completed to date is 70%.', 'FAIL', '冻结字段为7%，构造答案改成70%，核心数值错误。', ['numeric_error']),
        ('C03', office, '已支出的项目预算比例为7%；这个数字不表示工程完成比例，工程完成比例未知。', 'FAIL', '把work completed的7%改解释成预算支出比例，统计量和分母口径混淆且未回答完成比例。', ['statistical_scope_confusion']),
        ('C04', utc, '北京时间比UTC快8小时：把UTC时刻加8小时得到北京时间，必要时跨到次日；反向换算减8小时。', 'PASS', '与冻结Gold的UTC+0800=北京时间同义，保留方向、差值和单位。', ['synonym_correct']),
        ('C05', mirror, '一块抛光的金属板和一面化妆镜。', 'PASS', '抛光金属板实例化shiny object，化妆镜实例化mirror；两类必答项都保留，并非删去镜子类别。', ['alternative_instance_correct']),
        ('C06', mirror, '镜子。', 'PARTIAL', '只给出mirror，遗漏另一个必答反射面例子；已有内容正确，没有否认另一类。', ['missing_required_item']),
        ('C07', office, '工程完成比例为7%。此外，该报告保证项目将在下周五全部竣工并通过最终验收。', 'PASS', '按现有Judge的核心正确性口径，7%正确所以核心PASS；追加的下周五竣工及终验保证缺乏冻结材料支持，须单独评价支持性，不能与核心正确性混算。', ['unsupported_addition']),
        ('C08', utc, None, 'REVIEW_REQUIRED', '原人工核心结论PASS不变；历史回答另外含UTC/GMT、伦敦时间及跨日边界等补充断言。Gold只覆盖核心换算，须先确认Judge是评核心还是完整答案，不能把Gold未写的信息自动判成幻觉。', ['ambiguous_review_scope']),
    ]
    ledger, upload = [], []
    for case_id, qid, synthetic_text, verdict, reason, coverage in definitions:
        original, record = by_qid[qid]
        synthetic = synthetic_text is not None
        answer = synthetic_text if synthetic else record['answer']
        row = {'case_id': case_id, 'qid': qid, 'question': original['input']['question'],
               'reference_answer': copy.deepcopy(original['expected_output']['reference_answer']),
               'answer': answer, 'expected_verdict': verdict, 'review_reason': reason,
               'source_type': 'SYNTHETIC_CURATED' if synthetic else 'HISTORICAL_E1_SSE',
               'synthetic_answer': synthetic, 'coverage': coverage,
               'label_status': 'USER_CORE_PASS_REUSED' if case_id == 'C01' else 'CURATED_PROPOSAL_REQUIRES_USER_REVIEW',
               'original_human_core': record['review']['human_core'], 'original_trace_id': record['original_trace_id'],
               'answer_hash': base.sha(answer.encode('utf-8')), 'original_sse_sha256': record['sse_sha256'],
               'question_gold_source_sha256': base.sha(base.canonical(original)),
               'source_identity': record['source_identity']}
        item = {'input': {'case_id': case_id, 'qid': qid, 'question': row['question']},
                'expected_output': {'reference_answer': row['reference_answer']},
                'metadata': {'case_id': case_id, 'qid': qid, 'synthetic_answer': synthetic,
                             'source_type': row['source_type'], 'answer_hash': row['answer_hash'],
                             'reference_trace_id': record['original_trace_id'], 'source_identity': record['source_identity'],
                             'execution_type': 'CALIBRATION_REPLAY', 'real_model_run': False}}
        assert_no_labels(item)
        ledger.append(row); upload.append(item)
    directory.mkdir(parents=True, exist_ok=True)
    base.write_rows(directory / 'calibration-ledger.jsonl', ledger)
    base.write_rows(directory / 'dataset-items.jsonl', upload)
    files = {name: base.sha((directory / name).read_bytes()) for name in ['calibration-ledger.jsonl', 'dataset-items.jsonl']}
    privacy = []
    for row, item in zip(ledger, upload):
        io = {'input': {'question': row['question'], 'reference_answer': row['reference_answer']}, 'output': {'answer': row['answer']}}
        assert_no_labels(io)
        privacy.append({'case_id': row['case_id'], 'source_type': row['source_type'],
                        'dataset_canonical_bytes': len(base.canonical(item)), 'observation_io_canonical_bytes': len(base.canonical(io)),
                        'sensitive_pattern_counts': base.scan_sensitive({'item': item, 'observation_io': io}),
                        'answer_hash': row['answer_hash']})
    report = {'status': 'DRY_RUN_PASS', 'dataset_name': NAME, 'cases': len(ledger), 'unique_questions': len(by_qid.keys() & {r['qid'] for r in ledger}),
              'expected_verdict_distribution': dict(Counter(r['expected_verdict'] for r in ledger)),
              'synthetic_answers': sum(r['synthetic_answer'] for r in ledger), 'historical_answers': sum(not r['synthetic_answer'] for r in ledger),
              'label_boundary': 'Only C01 reuses a user-adjudicated core PASS. Curated labels and review-scope decision require human review before accuracy claims.',
              'permission_basis': 'Same question/Gold/source text explicitly approved by user in this chat for US Langfuse project; synthetic non-sensitive test answers created under the current explicit dataset request. No new source documents.',
              'prior_authorization_sha256': base.sha((PREVIOUS / 'authorization.approved.json').read_bytes()),
              'files_sha256': files, 'items': privacy, 'external_model_calls': 0, 'judge_calls': 0, 'cloud_writes': 0,
              'sensitive_review': 'Historical approved answers include project financial amounts; no new personal data introduced. Regex scan is not exhaustive authorization evidence.',
              'coverage': {r['case_id']: r['coverage'] for r in ledger}}
    base.write_json(directory / 'dry-run.json', report)
    lines = ['# Judge 校准样本 dry-run', '', '8个样本、3个原问题；预期标签只保存在本地，不传给Judge。6个构造答案明确标记synthetic_answer=true。', '',
             '仅C01沿用用户原核心PASS；其余为策展候选标签，需人工复核后才能作为已定稿真值。C08不进入自动准确率分母。', '']
    for row in ledger:
        lines += ['## ' + row['case_id'], '', '**问题**：' + row['question'], '', '**原冻结Gold**：' + json.dumps(row['reference_answer'], ensure_ascii=False), '',
                  '**待评估回答**：', '', row['answer'], '', '**预期**：' + row['expected_verdict'], '', '**理由**：' + row['review_reason'], '',
                  '**来源**：' + row['source_type'] + '；synthetic_answer=' + str(row['synthetic_answer']).lower(), '']
    (directory / 'calibration-review.md').write_text('\n'.join(lines), encoding='utf-8')
    return report


def validate(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'dry-run.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files_sha256'].items():
        if base.sha((directory / name).read_bytes()) != expected:
            raise ValueError('frozen calibration bundle hash mismatch')
    ledger, items = base.rows(directory / 'calibration-ledger.jsonl'), base.rows(directory / 'dataset-items.jsonl')
    if len(ledger) != 8 or len(items) != 8 or {r['case_id'] for r in ledger} != {f'C{i:02d}' for i in range(1, 9)}:
        raise ValueError('expected 8 unique calibration cases')
    for row, item in zip(ledger, items):
        assert_no_labels(item)
        if row['answer_hash'] != base.sha(row['answer'].encode()) or item['expected_output']['reference_answer'] != row['reference_answer']:
            raise ValueError('answer/Gold mismatch')
    return ledger, items


def revise_labels(directory):
    """Record a local-only rubric correction without changing frozen Cloud IO."""
    directory = Path(directory)
    ledger, _ = validate(directory)
    path = directory / 'calibration-ledger-v2.jsonl'
    if path.exists():
        raise ValueError('refusing to overwrite revised labels')
    evaluator = json.loads((directory / 'cloud-cli/evaluators-readonly.json').read_text(encoding='utf-8'))['body']['data'][0]
    prompt_hash = base.sha(base.canonical(evaluator['prompt']))
    revised = copy.deepcopy(ledger)
    for row in revised:
        row['curation_version'] = 'v2'
        row['rubric_evaluator_id'] = evaluator['id']
        row['rubric_prompt_sha256'] = prompt_hash
        if row['case_id'] == 'C07':
            row['prior_curated_expected_verdict'] = row['expected_verdict']
            row['expected_verdict'] = 'PASS'
            row['review_reason'] = '现有Judge评价核心问题正确性：7%正确，核心PASS。追加的下周五竣工及终验保证缺乏冻结材料支持，另列支持性问题；不能把支持性与核心正确性混算。'
            row['support_review'] = 'UNSUPPORTED_RELATIVE_TO_FROZEN_MATERIAL; not proof of real-world falsity'
    base.write_rows(path, revised)
    report = {'status': 'LOCAL_LABEL_REVISION_ONLY', 'rows': len(revised),
        'expected_verdict_distribution': dict(Counter(r['expected_verdict'] for r in revised)),
        'reason': 'Align C07 with the existing saved core-correctness evaluator; preserve initial curation and all original human conclusions.',
        'initial_ledger_sha256': base.sha((directory / 'calibration-ledger.jsonl').read_bytes()),
        'revised_ledger_sha256': base.sha(path.read_bytes()), 'evaluator_prompt_sha256': prompt_hash,
        'cloud_io_changed': False, 'model_calls': 0, 'human_label_status': 'Curated proposals require human review; C08 is a workflow hold, not a terminal Judge label.'}
    base.write_json(directory / 'label-revision-v2.json', report)
    return report


def make_task(client, ledger, items):
    lookup = {row['case_id']: (row, item) for row, item in zip(ledger, items)}
    def task(*, item, **kwargs):
        inp = item.input if not isinstance(item, dict) else item['input']
        expected = item.expected_output if not isinstance(item, dict) else item['expected_output']
        row, frozen = lookup[inp['case_id']]
        if inp != frozen['input'] or expected != frozen['expected_output']:
            raise ValueError('Cloud Dataset differs from frozen calibration case')
        meta = frozen['metadata']
        client.update_current_span(metadata=meta)
        io = {'question': row['question'], 'reference_answer': row['reference_answer']}
        output = {'answer': row['answer']}
        assert_no_labels({'input': io, 'output': output, 'metadata': meta})
        with client.start_as_current_observation(name='eval.review_case', as_type='span', input=io, output=output, metadata=meta):
            return output
    return task


def upload(directory):
    directory = Path(directory)
    ledger, items = validate(directory)
    with (directory / 'cloud-import-reserved.json').open('x', encoding='utf-8') as handle:
        json.dump({'state': 'RESERVED_NO_AUTOMATIC_RETRY'}, handle)
    project = base.cli(directory, 'project-check', ['projects', 'list'])
    if base.PROJECT not in [r['id'] for r in project['data']]:
        raise ValueError('credential project mismatch')
    rules = base.cli(directory, 'rules-before-upload', ['evaluation-rules', 'list', '--all'])
    if rules['data'] or rules.get('meta', {}).get('cursor'):
        raise ValueError('existing rules require inspection; no automatic scoring allowed')
    if base.cli(directory, 'dataset-absence', ['datasets', 'get', NAME], allow_404=True) is not None:
        raise ValueError('dataset exists; refusing to overwrite')
    body = {'name': NAME, 'description': 'Curated Judge calibration inputs. Historical and explicitly synthetic answers replayed without any model calls. Expected labels remain in an independent local ledger.',
            'metadata': {'execution_type': 'CALIBRATION_REPLAY', 'external_model_calls': 0, 'labels_stored': 'LOCAL_ONLY'}}
    base.write_json(directory / 'dataset-create.json', body)
    dataset = base.cli(directory, 'dataset-create', ['datasets', 'create', '--body-file', str(directory / 'dataset-create.json')])
    for n, (row, item) in enumerate(zip(ledger, items)):
        body = {'datasetName': NAME, 'input': item['input'], 'expectedOutput': item['expected_output'], 'metadata': item['metadata'], 'status': 'ACTIVE'}
        if not row['synthetic_answer']:
            body['sourceTraceId'] = row['original_trace_id']
        assert_no_labels(body)
        file = directory / f'item-create-{n}.json'
        base.write_json(file, body)
        base.cli(directory, f'item-create-{n}', ['dataset-items', 'create', '--body-file', str(file)])
    result = {'status': 'PASS', 'dataset_id': dataset['id'], 'dataset_name': NAME, 'items': 8,
              'dataset_url': f'{base.HOST}/project/{base.PROJECT}/datasets/{dataset["id"]}', 'external_model_calls': 0}
    base.write_json(directory / 'cloud-import-result.json', result)
    return result


def run_replay(directory):
    import httpx
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    directory = Path(directory)
    ledger, items = validate(directory)
    with (directory / 'replay-reserved.json').open('x', encoding='utf-8') as handle:
        json.dump({'state': 'RESERVED_NO_AUTOMATIC_RETRY'}, handle)
    rules = base.cli(directory, 'rules-before-replay', ['evaluation-rules', 'list', '--all'])
    if rules['data'] or rules.get('meta', {}).get('cursor'):
        raise ValueError('existing scoring rules; replay stopped')
    env = base.cloud_environment()
    os.environ['NO_PROXY'] = 'us.cloud.langfuse.com'
    os.environ['no_proxy'] = 'us.cloud.langfuse.com'
    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with base.deny_network('us.cloud.langfuse.com') as blocked:
        client = Langfuse(public_key=env['LANGFUSE_PUBLIC_KEY'], secret_key=env['LANGFUSE_SECRET_KEY'], base_url=base.HOST,
                          tracer_provider=provider, sample_rate=1.0, environment='judge-calibration-replay',
                          httpx_client=httpx.Client(trust_env=False, timeout=30))
        try:
            dataset = client.get_dataset(NAME)
            actual = {i.input['case_id']: i for i in dataset.items}
            if set(actual) != {r['case_id'] for r in ledger}:
                raise ValueError('dataset membership mismatch')
            for frozen in items:
                row = actual[frozen['input']['case_id']]
                if row.input != frozen['input'] or row.expected_output != frozen['expected_output'] or row.metadata != frozen['metadata']:
                    raise ValueError('dataset field mismatch')
            base.write_json(directory / 'dataset-readback.json', {'id': dataset.id, 'items': [r.model_dump(mode='json') for r in dataset.items]})
            result = dataset.run_experiment(name='Judge calibration input replay', run_name='judge-calibration-input-replay-20261030-v1',
                description='Curated answers only; no RAG generation, no Judge execution, expected labels local only.',
                task=make_task(client, ledger, items), evaluators=[], run_evaluators=[], max_concurrency=1,
                metadata={'execution_type': 'CALIBRATION_REPLAY', 'real_model_run': False, 'external_model_calls': 0})
            client.flush()
            captured = []
            for span in exporter.get_finished_spans():
                if span.name == 'eval.review_case':
                    captured.append({'trace_id': format(span.context.trace_id, '032x'), 'observation_id': format(span.context.span_id, '016x'),
                        'input': json.loads(span.attributes['langfuse.observation.input']), 'output': json.loads(span.attributes['langfuse.observation.output'])})
            base.write_rows(directory / 'observations.local.jsonl', captured)
            if len(result.item_results) != 8 or len(captured) != 8 or blocked:
                raise ValueError('SDK item/observation count or network boundary mismatch')
            rows = []
            for row in result.item_results:
                case_id = row.item.input['case_id']
                original = next(r for r in ledger if r['case_id'] == case_id)
                if row.output != {'answer': original['answer']} or row.evaluations:
                    raise ValueError('unexpected output or evaluation')
                rows.append({'case_id': case_id, 'trace_id': row.trace_id, 'trace_url': f'{base.HOST}/project/{base.PROJECT}/traces/{row.trace_id}'})
            report = {'status': 'SDK_REPLAY_COMPLETED_READBACK_PENDING', 'experiment_id': result.experiment_id,
                      'experiment_url': result.experiment_url, 'items': rows, 'judge_calls': 0, 'external_model_calls': 0,
                      'blocked_network_attempts': len(blocked)}
            base.write_json(directory / 'replay-result.json', report)
            return report
        finally:
            client.shutdown()


def verify_cloud(directory, use_sdk=False):
    directory = Path(directory)
    ledger, items = validate(directory)
    result = json.loads((directory / 'replay-result.json').read_text(encoding='utf-8'))
    by_case = {r['case_id']: r for r in ledger}
    client = None
    if use_sdk:
        import atexit
        import httpx
        from langfuse import Langfuse
        env = base.cloud_environment()
        client = Langfuse(public_key=env['LANGFUSE_PUBLIC_KEY'], secret_key=env['LANGFUSE_SECRET_KEY'],
            base_url=base.HOST, tracing_enabled=False, httpx_client=httpx.Client(trust_env=False, timeout=20))
        atexit.register(client.shutdown)
    def read(label, args):
        if client is not None:
            from datetime import datetime
            options = {args[i][2:].replace('-', '_'): args[i + 1] for i in range(2, len(args) - 1, 2)}
            if args[:2] == ['datasets', 'get']:
                dataset = client.get_dataset(args[2])
                body = {'id': dataset.id, 'name': dataset.name}
            elif args[:2] == ['dataset-items', 'list']:
                dataset = client.get_dataset(options['dataset_name'])
                body = {'data': [i.model_dump(mode='json', by_alias=True) for i in dataset.items], 'meta': {}}
            else:
                if 'from_start_time' in options:
                    options['from_start_time'] = datetime.fromisoformat(options['from_start_time'].replace('Z', '+00:00'))
                if 'limit' in options:
                    options['limit'] = int(options['limit'])
                method = {('experiments', 'list'): client.api.experiments.list,
                          ('experiment-items', 'list'): client.api.experiments.list_items,
                          ('observations', 'list'): client.api.observations.get_many}[tuple(args[:2])]
                response = method(**options)
                body = response.model_dump(mode='json', by_alias=True)
            target = directory / 'cloud-sdk'
            target.mkdir(exist_ok=True)
            base.write_json(target / (label + '.json'), {'transport': 'OFFICIAL_SDK_READ_ONLY_TRUST_ENV_FALSE', 'body': body})
            return body
        try:
            return base.cli(directory, label, args)
        except RuntimeError:
            # Only GET readbacks may retry. Every failed result is preserved.
            return base.cli(directory, label + '-retry1', args)
    dataset = read('dataset-final', ['datasets', 'get', NAME])
    experiment = read('experiment-final', ['experiments', 'list', '--id', result['experiment_id'],
                      '--from-start-time', '2026-10-29T00:00:00Z', '--fields', 'core,metadata,scores'])
    experiment_items = read('experiment-items-final', ['experiment-items', 'list', '--experiment-id', result['experiment_id'],
        '--from-start-time', '2026-10-29T00:00:00Z', '--fields', 'core,dataset,io,metadata,scores', '--limit', '100'])
    if len(experiment['data']) != 1 or experiment['data'][0]['itemCount'] != 8 or experiment['data'][0]['scores']:
        raise ValueError('experiment count or score mismatch')
    if len(experiment_items['data']) != 8 or experiment_items.get('meta', {}).get('cursor'):
        raise ValueError('experiment item count or pagination mismatch')
    def parse(value):
        return json.loads(value) if isinstance(value, str) else value
    for item in experiment_items['data']:
        inp = parse(item['input']); row = by_case[inp['case_id']]
        if parse(item['output']) != {'answer': row['answer']} or parse(item['expectedOutput']) != {'reference_answer': row['reference_answer']} or item['scores']:
            raise ValueError('experiment output/Gold mismatch or unexpected score')
        assert_no_labels(item)
    checks = []
    for n, item in enumerate(result['items']):
        body = read('observation-' + str(n), ['observations', 'list', '--trace-id', item['trace_id'],
                    '--fields', 'core,basic,io,metadata,model,usage', '--limit', '100'])
        observations = body['data']
        if len(observations) != 3 or body.get('meta', {}).get('cursor'):
            raise ValueError('unexpected trace node count or unread pagination')
        if any(o['type'] != 'SPAN' for o in observations):
            raise ValueError('unexpected model/evaluator observation')
        matches = [o for o in observations if o['name'] == 'eval.review_case']
        if len(matches) != 1:
            raise ValueError('expected exactly one review observation')
        observation = matches[0]
        row = by_case[item['case_id']]
        expected_input = {'question': row['question'], 'reference_answer': row['reference_answer']}
        if parse(observation['input']) != expected_input or parse(observation['output']) != {'answer': row['answer']}:
            raise ValueError('review observation not equal to frozen case')
        metadata = observation['metadata']
        assert_no_labels(metadata)
        if metadata['synthetic_answer'] != row['synthetic_answer'] or metadata['source_type'] != row['source_type'] or metadata['answer_hash'] != row['answer_hash']:
            raise ValueError('synthetic/historical origin mismatch')
        checks.append({'case_id': row['case_id'], 'status': 'PASS', 'trace_id': item['trace_id'],
                       'observation_id': observation['id'], 'synthetic_answer': row['synthetic_answer'],
                       'question_gold_answer_equal': True, 'label_leak_keys': [],
                       'observation_url': f'{base.HOST}/project/{base.PROJECT}/traces/{item["trace_id"]}?observation={observation["id"]}'})
    old = read('original-e1-dataset-final', ['dataset-items', 'list', '--dataset-name', base.DATASET, '--all'])
    baseline = json.loads((PREVIOUS / 'dataset-readback.json').read_text(encoding='utf-8'))['items']
    prior = {i['id']: i for i in baseline}
    if len(old['data']) != 5 or {i['id'] for i in old['data']} != set(prior):
        raise ValueError('old E1 dataset membership changed')
    for item in old['data']:
        frozen = prior[item['id']]
        frozen_expected = frozen.get('expectedOutput', frozen.get('expected_output'))
        if item['input'] != frozen['input'] or item['expectedOutput'] != frozen_expected or item['metadata'] != frozen['metadata']:
            raise ValueError('old E1 Dataset content changed')
    report = {'status': 'PASS', 'dataset_id': dataset['id'], 'dataset_cases': 8,
              'experiment_id': result['experiment_id'], 'experiment_items': 8, 'review_observations': 8,
              'historical_answers': 2, 'synthetic_answers': 6, 'checks': checks,
              'external_model_calls': 0, 'judge_calls': 0, 'scores': 0,
              'original_e1_dataset_unchanged': True, 'expected_labels_stored': 'LOCAL_ONLY',
              'ui_automation': 'BLOCKED_URL_CONFIDENCE', 'accuracy_on_new_cases': 'NOT_RUN',
              'readback_transport': 'OFFICIAL_SDK' if use_sdk else 'OFFICIAL_CLI'}
    base.write_json(directory / 'cloud-verification.json', report)
    if client is not None:
        client.shutdown()
    return report


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'upload', 'replay', 'verify', 'verify-sdk', 'revise-labels'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'prepare': prepare, 'upload': upload, 'replay': run_replay, 'verify': verify_cloud,
              'verify-sdk': lambda directory: verify_cloud(directory, True), 'revise-labels': revise_labels}[args.command](args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
