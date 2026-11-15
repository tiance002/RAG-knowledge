"""Two authorized single-question reviews of frozen answers; never executes RAG."""
import argparse
import json
from pathlib import Path

import rsi_quality_loop as quality
import rsi_quality_registry as registry
from rsi_candidates import canonical, read_json, sha

SCHEMA = 'rsi-saved-answer-review/v1'
RATIONALE = 'Targeted judge calibration: selected wrong-quarter baseline and improved candidate; not random held-out validation.'


def file_ref(path):
    return registry.frozen_ref(path)


def read_file(ref):
    path = Path(ref['path'])
    if not path.is_absolute() or sha(path.read_bytes()) != ref['sha256']:
        raise ValueError('saved-answer file fingerprint mismatch')
    return read_json(path)


def selected_records(worker, qid):
    rows = [r for r in worker['records'] if r.get('qid') == qid]
    if (len(rows) != 2 or {r.get('repeat_id') for r in rows} != {1, 2} or
            any(r.get('status') != 'RETURNED' or not r.get('answer', '').strip() for r in rows)):
        raise ValueError('exactly two returned saved answers required; missing output is not reexecuted')
    return sorted(rows, key=lambda r: r['repeat_id'])


def compact_judge_input(records, cases, gold):
    """Losslessly share citation metadata as well as the helper's evidence text."""
    value = quality.judge_input(records, cases, gold)
    citations, questions = {}, {}
    for sample in value['samples']:
        qid = sample['qid']
        questions[qid] = {k: sample.pop(k) for k in ('question', 'preferences', 'history')}
        sample['question_context_ref'] = qid
        for field in ('citations', 'cited_evidence'):
            refs = []
            for item in sample[field]:
                digest = sha(canonical(item))
                citations[digest] = item
                refs.append(digest)
            sample[field] = refs
    value.update(citations_by_sha256=citations, question_contexts=questions)
    value['evidence_resolution'] += ' Resolve ordered citations/cited_evidence digests using citations_by_sha256; resolve question_context_ref using question_contexts. No original cited text or citation metadata is omitted.'
    return value


def make_plan(baseline_review, candidate_review, previous_ledger, qid):
    specs = []
    for label, directory in (('baseline', baseline_review), ('candidate', candidate_review)):
        directory = Path(directory).resolve()
        worker = read_json(directory / 'worker.json')
        rows = selected_records(worker, qid)
        specs.append({'label': label, 'manifest': file_ref(directory / 'manifest.json'),
            'worker': file_ref(directory / 'worker.json'), 'source_results': file_ref(worker['source_record']),
            'job': file_ref(Path(worker['source_record']).parent.parent / 'job.json'),
            'records': [{'qid': r['qid'], 'repeat_id': r['repeat_id'], 'sha256': sha(canonical(r)),
                         'raw': file_ref(r['raw_record'])} for r in rows]})
    plan = {'schema': SCHEMA, 'rationale': RATIONALE, 'qid': qid,
            'evaluator_version': quality.JUDGE_VERSION,
            'previous_ledger': file_ref(previous_ledger), 'previous_model_calls': 12,
            'approved_extra_ceiling': 2, 'cumulative_ceiling': 14, 'reviews': specs}
    verify_plan(plan)
    return plan


def verify_plan(plan):
    if (plan.get('schema') != SCHEMA or plan.get('previous_model_calls') != 12 or
            plan.get('evaluator_version') != quality.JUDGE_VERSION or
            plan.get('approved_extra_ceiling') != 2 or plan.get('cumulative_ceiling') != 14 or
            not isinstance(plan.get('qid'), str) or not plan['qid'].strip() or len(plan.get('reviews', [])) != 2 or
            [r.get('label') for r in plan.get('reviews', [])] != ['baseline', 'candidate']):
        raise ValueError('unsupported two-review calibration plan')
    previous = read_file(plan['previous_ledger'])
    if (previous.get('model_calls') != 12 or len(previous.get('role_calls', [])) != 12 or
            {r.get('call') for r in previous['role_calls']} != set(range(1, 13))):
        raise ValueError('previous immutable twelve-call identity ledger required')
    qid, checked, common, baseline_id = plan['qid'], [], None, None
    from rsi_quality_execute import verify_source
    for spec in plan['reviews']:
        manifest, worker = read_file(spec['manifest']), read_file(spec['worker'])
        if manifest.get('schema') != registry.SCHEMA:
            raise ValueError('saved-answer manifest schema mismatch')
        if spec['label'] == 'baseline':
            if set(manifest['arms']) != {'B'} or manifest['arms']['B']['sha256'] != sha(canonical(registry.BASELINE)):
                raise ValueError('selected baseline is not the actual root artifact')
            baseline_id = manifest['arms']['B']['candidate_id']
        elif (set(manifest['arms']) != {'A', 'B'} or manifest['arms']['A']['candidate_id'] != baseline_id or
                manifest['arms']['B']['parent_id'] != baseline_id):
            raise ValueError('selected candidate does not descend from this baseline')
        binding = {k: manifest[k] for k in ('source_fingerprint', 'fixed', 'cases', 'evaluation')}
        if common is not None and common != binding:
            raise ValueError('reviews do not share the same frozen product, cases and Gold')
        common = binding
        verify_source(manifest['source_fingerprint'])
        cases = registry.read_ref(manifest['cases'])
        cases = cases['cases'] if isinstance(cases, dict) else cases
        cases = [c for c in cases if c['qid'] == qid]
        if len(cases) != 1:
            raise ValueError('selected QID missing or ambiguous in frozen inputs')
        evaluation = registry.read_ref(manifest['evaluation'])
        gold = quality.evaluation_gold(evaluation)
        if qid not in gold:
            raise ValueError('selected QID missing in frozen Gold')
        source = read_file(spec['source_results'])
        job = read_file(spec['job'])
        if sha(canonical(job['artifact'])) != manifest['arms']['B']['sha256']:
            raise ValueError('actual executed artifact differs from saved manifest')
        if (worker.get('source_record') != spec['source_results']['path'] or
                worker.get('source_sha256') != spec['source_results']['sha256']):
            raise ValueError('worker source-results binding mismatch')
        rows = selected_records(worker, qid)
        if len(spec['records']) != 2:
            raise ValueError('record references must cover both repetitions')
        judge_rows = []
        for row, ref in zip(rows, sorted(spec['records'], key=lambda x: x['repeat_id'])):
            if ((ref['qid'], ref['repeat_id']) != (qid, row['repeat_id']) or ref['sha256'] != sha(canonical(row)) or
                    row.get('raw_record') != ref['raw']['path']):
                raise ValueError('selected worker record hash mismatch')
            raw = read_file(ref['raw'])
            if (raw.get('case_id') != qid or raw.get('repeat_id') != row['repeat_id'] or
                    any(raw.get(k) != row.get(k) for k in ('answer', 'citations', 'status'))):
                raise ValueError('normalized answer differs from actual raw worker record')
            matching = [r for r in source.get('results', []) if r.get('case_id') == qid and r.get('repeat_id') == row['repeat_id']]
            if len(matching) != 1 or any(matching[0].get(k) != raw.get(k) for k in ('answer', 'citations', 'status', 'preference_resolution')):
                raise ValueError('raw answer differs from source-results archive')
            if 'preference_resolution' in row and row['preference_resolution'] != raw.get('preference_resolution'):
                raise ValueError('normalized preference resolution differs from actual raw record')
            # Historical normalized records remain immutable. Only the verified
            # evaluation view projects the captured effective values and sources.
            judge_rows.append({**row, 'preference_resolution': raw.get('preference_resolution')})
        info = compact_judge_input(judge_rows, cases, quality.EvaluationGold({qid: gold[qid]}, gold.rubric))
        checked.append({'spec': spec, 'records': rows, 'info': info})
    return checked


def run(plan_path, out, *, execute=False, approved_model_calls=0):
    plan_path, out = Path(plan_path).resolve(), Path(out).resolve()
    plan = read_json(plan_path)
    groups = verify_plan(plan)
    cfg = {'budget': {'max_output_tokens': 16384, 'max_input_bytes': 32768}}
    requests = [quality.role_request('judge', group['info'], cfg) for group in groups]
    if out.exists() or any(out.is_relative_to(Path(g['spec']['worker']['path']).parent) for g in groups):
        raise ValueError('review output must be a new directory outside saved answer archives')
    preflight = {'status': 'PREFLIGHT', 'rationale': plan['rationale'], 'qid': plan['qid'],
        'evaluator_version': quality.JUDGE_VERSION,
        'subsets': [2, 2], 'planned_new_model_calls': 2, 'previous_model_calls': 12, 'cumulative_ceiling': 14,
        'actual_new_rag_calls': 0, 'actual_new_model_calls': 0,
        'request_bytes': [len(canonical(r)) for r in requests], 'plan_sha256': sha(plan_path.read_bytes())}
    if not execute:
        return preflight
    if approved_model_calls != 2:
        raise ValueError('explicit two-call authorization required; cumulative cap remains fourteen')
    out.mkdir(parents=True, exist_ok=False)
    quality.save(out / 'plan.json', plan)
    quality.save(out / 'preflight.json', preflight)
    # This state is explicitly local to the two authorized new review calls.
    # The immutable prior ledger supplies the offset; it is never reset.
    stage = {'model_calls': 0, 'new_model_calls': 0, 'role_calls': []}
    results = [{'label': g['spec']['label'], 'status': 'NOT_RUN',
        'scores': quality.score_rows({'scores': []}, g['records']),
        'metrics': quality.metrics(g['records'], quality.score_rows({'scores': []}, g['records']))} for g in groups]
    status, error = 'REVIEW_COMPLETE', None
    try:
        for index, group in enumerate(groups, 1):
            verify_plan(plan)
            if 12 + stage['model_calls'] >= 14:
                raise ValueError('cumulative fourteen-call ceiling reached')
            quality.save(out / f'budget-binding-{index}.json', {'previous_ledger': plan['previous_ledger'],
                'previous_model_calls': 12, 'stage_call': index, 'cumulative_call': 12 + index,
                'cumulative_ceiling': 14, 'new_rag_calls': 0})
            judged = quality.call_json('judge', group['info'], cfg, out / f'review-{index}', stage, 2)
            scores = quality.score_rows(judged, group['records'])
            results[index - 1].update(status='AI_REVIEW', scores=scores, metrics=quality.metrics(group['records'], scores))
            quality.save(out / f'scores-{index}.json', results[index - 1])
            if any(row[d] == 'UNKNOWN' for row in scores for d in quality.DIMENSIONS):
                raise ValueError('review UNKNOWN; stop without replay')
    except Exception as exc:
        status, error = 'STOPPED', f'{type(exc).__name__}: {exc}'
    result = {'status': status, 'error': error, 'scope': RATIONALE, 'plan_sha256': preflight['plan_sha256'],
        'evaluator_version': quality.JUDGE_VERSION,
        'previous_ledger': plan['previous_ledger'], 'previous_model_calls': 12,
        'stage_model_calls': stage['model_calls'], 'model_calls': 12 + stage['model_calls'],
        'role_calls': stage['role_calls'], 'actual_new_rag_calls': 0, 'reviews': results,
        'formal_promotion': 'NOT_RUN', 'reviewer': 'AI_REVIEW'}
    quality.save(out / 'results.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approved-model-calls', type=int, default=0)
    args = parser.parse_args()
    result = run(args.plan, args.out, execute=args.execute, approved_model_calls=args.approved_model_calls)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['status'] in ('PREFLIGHT', 'REVIEW_COMPLETE') else 1)
