"""Offline, read-only dynamic166 execution audit and human review bundle.

No provider imports/calls, automatic Judge, corpus copies or ledger mutation.
An interrupted service remains interrupted; UNKNOWN is never promoted or retried.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import sqlite3

from finalize_routing150 import atomic, canonical, percentile, sha, usage_summary, wait_service

CHEAP = 'Qwen/Qwen3.5-4B'
EXPENSIVE = 'zai-org/GLM-5.3-Flash'
ATTEMPT_FIELDS = ('model', 'actual_model', 'model_identity_status', 'messages_sha256',
                  'comparison_input_hash', 'request_hash', 'endpoint_sha256', 'answer_sha256',
                  'finish_reason', 'usage', 'latency_ms', 'provider_request_id', 'started_at',
                  'ended_at', 'http_status', 'error_type')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def payload_hash(case, model):
    value = dict(model=model, messages=case['messages'], temperature=.7, max_tokens=4096, stream=False)
    if model == CHEAP:
        value['enable_thinking'] = False
    return sha(canonical(value))


def safe_attempt(state, info):
    result = {k: info.get(k) for k in ATTEMPT_FIELDS}
    result.update(state=state, actual_answer=info.get('text'), core_correctness='UNKNOWN')
    # A model in a request or response is a claimed identifier, not attestation.
    result['model_identity_note'] = 'REQUEST_AND_PROVIDER_REPORTED_IDENTIFIERS_ONLY'
    return result


def load_run(manifest_path, ledger_path):
    manifest = read_json(manifest_path)
    if manifest.get('case_loader') == 'freeze_dynamic138':
        from freeze_dynamic138 import load_frozen_cases
        cases = load_frozen_cases(manifest)
    else:
        cases = manifest['cases']
    qids = [c['qid'] for c in cases]
    if not qids or len(qids) != len(set(qids)):
        raise ValueError('empty or duplicate frozen qids')
    for case in cases:
        if case.get('messages') and sha(canonical(case['messages'])) != case['messages_sha256']:
            raise ValueError('frozen message identity mismatch')
    identity_hash = sha(canonical(manifest))
    with readonly(ledger_path) as db:
        db.execute('begin')
        identity = json.loads(db.execute("select value from metadata where key='identity'").fetchone()[0])
        if identity.get('manifest_sha256') != identity_hash:
            raise ValueError('ledger manifest identity mismatch')
        rows = db.execute('select qid,arm,step,state,info from attempts').fetchall()
        slots = db.execute('select qid,arm,info from slots').fetchall()
        events = db.execute('select qid,arm,step,state from events order by id').fetchall()
    attempts, records = {}, {}
    for qid, arm, step, state, encoded in rows:
        if qid not in qids:
            raise ValueError('ledger attempt outside frozen scope')
        if arm != 'DYNAMIC':
            continue  # Previous12 ledger may also contain other arms.
        if step != 0 or qid in attempts:
            raise ValueError('unexpected dynamic escalation or duplicate attempt')
        info = json.loads(encoded)
        case = cases[qids.index(qid)]
        if (info.get('model') not in (CHEAP, EXPENSIVE)
                or info.get('messages_sha256') != case.get('messages_sha256')
                or info.get('request_hash') != payload_hash(case, info['model'])):
            raise ValueError('dynamic request identity mismatch')
        if info.get('text') is not None and info.get('answer_sha256') != sha(info['text'].encode('utf-8')):
            raise ValueError('dynamic answer hash mismatch')
        pending = sum(q == qid and a == arm and s == step and st == 'UNKNOWN_PENDING'
                      for q, a, s, st in events)
        terminal = [st for q, a, s, st in events
                    if q == qid and a == arm and s == step and st != 'UNKNOWN_PENDING']
        if pending != 1 or terminal != ([] if state == 'UNKNOWN_PENDING' else [state]):
            raise ValueError('dynamic event sequence does not prove single reservation')
        attempts[qid] = (state, info)
    for qid, arm, encoded in slots:
        if arm == 'DYNAMIC':
            if qid not in qids or qid in records:
                raise ValueError('invalid dynamic slot')
            records[qid] = json.loads(encoded)
    return dict(cases=cases, attempts=attempts, slots=records,
                manifest_sha256=identity_hash, manifest_path=str(manifest_path), ledger_path=str(ledger_path))


def historical_rows(path):
    with readonly(path) as db:
        db.execute('begin')
        return [(q, role, state, json.loads(info)) for q, role, state, info
                in db.execute('select qid,role,state,info from attempts')]


def baseline(case, rows, ledger_path):
    expected = payload_hash(case, CHEAP) if case.get('messages') else None
    same = [(q, r, s, i) for q, r, s, i in rows if expected
            and i.get('model') == CHEAP and i.get('messages_sha256') == case.get('messages_sha256')
            and i.get('request_hash') == expected]
    provenance = case.get('cheap_baseline', {})
    if not provenance and case.get('provenance', {}).get('delivered_qid'):
        provenance = dict(source_qid=case['provenance']['delivered_qid'],
                          source_role=case['provenance']['delivered_role'])
    if provenance:
        same = [x for x in same if x[:2] == (provenance.get('source_qid'), provenance.get('source_role'))]
    if not same:
        partial = [(q, r, s, i) for q, r, s, i in rows
                   if i.get('model') == CHEAP and i.get('messages_sha256') == case.get('messages_sha256')
                   and i.get('request_hash') is None]
        if provenance:
            partial = [x for x in partial if x[:2] == (provenance.get('source_qid'), provenance.get('source_role'))]
        if len(partial) == 1:
            q, r, state, info = partial[0]
            result = safe_attempt(state, info)
            result.update(exact_match=False, status='MESSAGES_AND_MODEL_MATCH_REQUEST_CONFIG_UNKNOWN',
                          expected_request_hash=expected, source_ledger=str(ledger_path),
                          source_qid=q, source_role=r, excluded_from_exact_paired_aggregate=True)
            return result
    if len(same) != 1:
        return dict(exact_match=False, status='UNKNOWN_MISSING_OR_AMBIGUOUS_EXACT_BASELINE',
                    candidate_count=len(same), expected_model=CHEAP, expected_request_hash=expected,
                    actual_answer=None, core_correctness='UNKNOWN')
    qid, role, state, info = same[0]
    result = safe_attempt(state, info)
    if (info.get('text') is not None and info.get('answer_sha256') is not None
            and info['answer_sha256'] != sha(info['text'].encode('utf-8'))):
        raise ValueError('historical answer hash mismatch')
    if isinstance(info.get('text'), str):
        result['answer_sha256'] = sha(info['text'].encode('utf-8'))
        result['answer_hash_origin'] = 'RECORDED_AND_VERIFIED' if info.get('answer_sha256') else 'COMPUTED_OFFLINE_FROM_HISTORICAL_TEXT'
    result.update(exact_match=True, source_ledger=str(ledger_path), source_qid=qid, source_role=role)
    return result


def metric_summary(attempts):
    values = [i['latency_ms'] for _, s, i in attempts if type(i.get('latency_ms')) in (int, float)
              and math.isfinite(i['latency_ms']) and i['latency_ms'] >= 0]
    return dict(attempts=len(attempts), states=dict(Counter(s for _, s, _ in attempts)),
                requested_models=dict(Counter(i.get('model', 'UNKNOWN') for _, _, i in attempts)),
                provider_reported_models=dict(Counter(i.get('actual_model') or 'UNKNOWN' for _, _, i in attempts)),
                provider_usage=usage_summary(attempts), latency_ms=dict(n=len(values),
                    missing_attempts=len(attempts)-len(values), p50=percentile(values, .5),
                    p95=percentile(values, .95), method='LINEAR_INTERPOLATION_RECORDED_ATTEMPT_LATENCIES'))


def finalize(manifest_path, ledger_path, historical_ledger, output,
             prior_manifest=None, prior_ledger=None, service=None):
    os.umask(0o077)
    if bool(prior_manifest) != bool(prior_ledger):
        raise ValueError('both prior paths required')
    runs = [load_run(manifest_path, ledger_path)]
    if prior_manifest:
        runs.append(load_run(prior_manifest, prior_ledger))
    cases = [c for run in runs for c in run['cases']]
    if len({c['qid'] for c in cases}) != len(cases):
        raise ValueError('overlapping new and previous scopes')
    rows = historical_rows(historical_ledger)
    evidence, dynamic_attempts, cheap_attempts = [], [], []
    for run in runs:
        for case in run['cases']:
            qid = case['qid']
            slot = run['slots'].get(qid, {})
            state, info = run['attempts'].get(qid, ('NOT_RUN', {}))
            historical = baseline(case, rows, historical_ledger)
            dynamic = safe_attempt(state, info)
            if qid in run['attempts']:
                dynamic_attempts.append((qid, state, info))
            if historical['exact_match']:
                cheap_attempts.append((qid, historical['state'], historical))
            # Preserve the original UNKNOWN_PENDING even if service has exited.
            execution = state if qid in run['attempts'] else slot.get('execution_status', 'NOT_RUN')
            gold = case.get('gold', {})
            gold_view = {k: v for k, v in gold.items() if k not in ('original_case', 'source_record')}
            evidence.append(dict(qid=qid, original_qid=case.get('original_qid'), question=case.get('question'),
                stratum=case.get('stratum'), source_status=case.get('source_status'),
                not_run_reason=case.get('not_run_reason') or slot.get('blocked_reason'),
                execution_status=execution, slot_recorded=bool(slot), route=slot.get('route'),
                dynamic=dynamic, historical_cheap=historical, gold=gold_view,
                gold_status=case.get('gold_status'), gold_sha256=case.get('gold_sha256'),
                gold_full_reference=dict(manifest=run['manifest_path'], qid=qid, field='gold'),
                messages_sha256=case.get('messages_sha256'), comparison_input_hash=case.get('comparison_input_hash'),
                frozen_context_reference=dict(manifest=run['manifest_path'], qid=qid,
                    context_sha256=case.get('context_sha256') or case.get('provenance', {}).get('context_sha256'),
                    messages_sha256=case.get('messages_sha256'), provenance=case.get('provenance')),
                citations=[{k: v for k, v in c.items() if k != 'content'} for c in case.get('citations', [])],
                review=dict(core_correctness='UNKNOWN', source_fidelity='UNKNOWN', citation_support='UNKNOWN',
                            data_or_gold_error='UNKNOWN', ambiguity='UNKNOWN',
                            reviewer=None, reason=None, evidence_refs=[], status='HUMAN_REVIEW_PENDING')))
    counts = dict(Counter(x['execution_status'] for x in evidence))
    incomplete = any(x['execution_status'] in ('NOT_RUN', 'UNKNOWN_PENDING', 'ERROR') or not x['slot_recorded']
                     for x in evidence)
    summary = dict(schema_version='dynamic166-offline-review-v1', scope=len(cases), new_scope=len(runs[0]['cases']),
        prior_scope=len(cases)-len(runs[0]['cases']), status=(
            'EXECUTION_INCOMPLETE_REVIEW_PENDING' if incomplete else 'EXECUTION_RECORDED_REVIEW_PENDING'),
        quality_status='UNKNOWN_HUMAN_REVIEW_PENDING', final_core_accuracy=None, service=service,
        execution_states=counts, dynamic=metric_summary(dynamic_attempts), historical_cheap=metric_summary(cheap_attempts),
        dynamic_by_requested_model={model: metric_summary([a for a in dynamic_attempts if a[2].get('model') == model])
                                    for model in (CHEAP, EXPENSIVE)},
        historical_exact_matches=sum(x['historical_cheap']['exact_match'] for x in evidence),
        historical_unknown_matches=sum(x['historical_cheap'].get('state', '').startswith('UNKNOWN') for x in evidence),
        cost_status='UNKNOWN', savings_status='UNKNOWN', model_calls_by_finalizer=0,
        correctness_denominator=None, scope_denominator=len(cases), sources=[
            {k: run[k] for k in ('manifest_path', 'manifest_sha256', 'ledger_path')} for run in runs],
        historical_ledger=str(historical_ledger), limitations=[
            'Frozen provider replay, not a live retrieval or HTTP/SSE evaluation.',
            'Execution success does not establish answer correctness or citation support.',
            'Requested and provider-reported model identifiers are not independent model attestation.',
            'Missing usage is unknown, never zero; sums with missing usage are partial.',
            'Historical inputs and Gold are preserved; source gaps and ambiguous references require review.',
            'Only DYNAMIC step0 is compared; previous other-arm calls are excluded.'])
    lines = ['# Dynamic 166 与既有 12 题统一人工复核材料', '',
             '状态：' + summary['status'], '',
             '此材料仅汇总真实执行记录。答案正确性、引用支持、费用与节省均为 UNKNOWN；未运行或中断不计为答错。', '',
             '统计分母：' + str(len(cases)) + '；实际新动态尝试：' + str(len(runs[0]['attempts'])) +
             '；语义正确率分母尚未确定。上下文通过冻结 manifest 引用回读，未复制语料或索引。', '']
    for item in evidence:
        lines += ['## ' + item['qid'], '', item.get('question') or '（输入缺失）', '',
                  '- 执行：' + item['execution_status'], '- 正确性与引用支持：UNKNOWN，待人工逐项复核。',
                  '- 历史 Cheap 精确请求匹配：' + str(item['historical_cheap']['exact_match']), '',
                  'Gold：', '```json', json.dumps(item['gold'], ensure_ascii=False, indent=2), '```', '',
                  '动态实际回答：', '', item['dynamic']['actual_answer'] or '（无可用回答）', '',
                  '历史 Cheap 实际回答：', '', item['historical_cheap']['actual_answer'] or '（无精确匹配的可用回答）', '',
                  '来源与上下文定位：', '```json', json.dumps(dict(context=item['frozen_context_reference'],
                  citations=item['citations']), ensure_ascii=False, indent=2), '```', '']
    out = Path(output)
    if out.resolve() in {Path(p).resolve().parent for p in (manifest_path, ledger_path, historical_ledger,
                                                              prior_manifest, prior_ledger) if p}:
        raise ValueError('use separate finalizer output directory')
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts = {'summary.json': summary, 'manual_source_review.json': dict(summary=summary, cases=evidence)}
    for name, value in artifacts.items():
        atomic(out / name, json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    atomic(out / 'review.md', '\n'.join(lines))
    atomic(out / 'completion.json', json.dumps(dict(status=summary['status'],
        artifacts_sha256={name: sha((out/name).read_bytes()) for name in (*artifacts, 'review.md')}), indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'ledger', 'historical-ledger', 'output'):
        parser.add_argument('--' + name, required=True)
    for name in ('prior-manifest', 'prior-ledger', 'wait-service'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    try:
        service = wait_service(args.wait_service) if args.wait_service else None
        result = finalize(args.manifest, args.ledger, args.historical_ledger, args.output,
                          args.prior_manifest, args.prior_ledger, service)
        print(json.dumps(dict(status=result['status'], scope=result['scope'], model_calls=0)))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='FAIL', error_type=type(exc).__name__, model_calls=0)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
