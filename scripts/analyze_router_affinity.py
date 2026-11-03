"""Zero-call replay of captured routing inputs and already reviewed outcomes.

This is counterfactual policy analysis, NOT a new model experiment. Inputs are
read-only. Missing model arms stay UNKNOWN; old failures/Gold are never changed.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.services.model_router import FeatureValue, RouterConfig, apply_selection_policy, measure_target_field_collision
from dynamic_router_freeze12 import selected_citations


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique(rows, key='qid'):
    result = {r[key]: r for r in rows}
    if len(result) != len(rows):
        raise ValueError('duplicate identity: ' + key)
    return result


def replay(question, route, policy, evidence_metadata=None):
    if not route or not route.get('features'):
        return {'role': 'UNKNOWN', 'reason': 'CAPTURED_FEATURES_UNAVAILABLE'}
    config = RouterConfig(mode='DYNAMIC', threshold=route['threshold'],
                          weights=route['measurements']['weights'],
                          unknown_policy=route['measurements']['unknown_policy']['mode'],
                          selection_policy=policy)
    features = {k: FeatureValue(v['value'], v['status'], v['method'])
                for k, v in route['features'].items()}
    metadata = {**route['measurements'], **(evidence_metadata or {})}
    low, high, details = apply_selection_policy(question, features, metadata, config)
    role = ('CHEAP' if high < config.threshold else 'EXPENSIVE' if low >= config.threshold
            else config.unknown_policy)
    return {'role': role, 'score_lower': low, 'score_upper': high, **details}


def outcome(role, cheap, expensive):
    # BASELINE is only interpreted as Cheap by callers after identity checking.
    return expensive if role == 'EXPENSIVE' else cheap if role in ('CHEAP', 'BASELINE') else 'UNKNOWN'


def summarize(rows, key):
    counts = Counter(r[key] for r in rows)
    known = sum(counts[k] for k in ('PASS', 'PARTIAL', 'FAIL'))
    return {'n': len(rows), 'counts': dict(counts), 'known_denominator': known,
            'core_pass_rate_known': counts['PASS'] / known if known else None,
            'confirmed_pass_per_planned': counts['PASS'] / len(rows) if rows else None}


def saved_call_metrics(rows, attempts, policy):
    """Describe saved calls picked by a policy, never predict new-run latency."""
    latencies, token_totals = [], []
    states = Counter()
    for row in rows:
        role = 'expensive' if row[policy]['role'] == 'EXPENSIVE' else ('followup' if row['qid'].startswith('followup:') else 'initial')
        call = attempts[(row['qid'], role)]
        states[call['state']] += 1
        value = call.get('latency_ms')
        if isinstance(value, (int, float)) and math.isfinite(value):
            latencies.append(value)
        value = (call.get('usage') or {}).get('total_tokens')
        if isinstance(value, int):
            token_totals.append(value)
    return {'kind': 'SAVED_REQUEST_METRICS_NOT_NEW_EXPERIMENT', 'n': len(rows), 'states': dict(states),
            'latency_samples': len(latencies), 'latency_p50_ms': statistics.median(latencies) if latencies else None,
            'latency_p95_ms': sorted(latencies)[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
            'total_token_samples': len(token_totals), 'total_tokens': sum(token_totals) if len(token_totals) == len(rows) else None,
            'cost': 'UNKNOWN'}


def run(root, output):
    paths = {name: root / name for name in (
        '36题逐题复核账本.json', '动态164题统一复核账本.json',
        'dynamic166-review-input-private.json', 'full180-v2-review-readonly.sqlite3',
        'gold150-contexts-review-private.json', 'split.json')}
    hashes = {k: sha(v.read_bytes()) for k, v in paths.items()}
    read = lambda name: json.loads(paths[name].read_text(encoding='utf-8-sig'))
    historic = unique(read('36题逐题复核账本.json')['cases'])
    reviews = unique(read('动态164题统一复核账本.json')['records'])
    captured = unique(read('dynamic166-review-input-private.json')['cases'])
    sources = unique(read('gold150-contexts-review-private.json')['cases'])
    splits = unique(read('split.json')['records'])
    db = sqlite3.connect(paths['full180-v2-review-readonly.sqlite3'].resolve().as_uri() + '?mode=ro', uri=True)
    try:
        attempts = {(q, r): {'state': s, **json.loads(i)}
                    for q, r, s, i in db.execute('select qid,role,state,info from attempts')}
    finally:
        db.close()
    pairs, identity_checks = {}, []
    for qid, row in historic.items():
        cheap = attempts[(qid, 'followup' if qid.startswith('followup:') else 'initial')]
        expensive = attempts[(qid, 'expensive')]
        checks = {
            'same_actual_messages': bool(cheap.get('messages_sha256')) and cheap['messages_sha256'] == expensive.get('messages_sha256'),
            'same_route_input': cheap['messages_sha256'] == captured[qid]['messages_sha256'],
            'cheap_answer_exact': cheap['text'] == row['cheap']['answer'],
            'expensive_answer_exact': expensive['text'] == row['expensive']['answer'],
            'cheap_identity': cheap.get('actual_model') == 'Qwen/Qwen3.5-4B',
            'expensive_identity': expensive.get('actual_model') == 'zai-org/GLM-5.3-Flash',
        }
        identity_checks.append({'qid': qid, **checks})
        if not all(checks.values()):
            raise ValueError('pair identity check failed: ' + qid + ' ' + str(checks))
        pairs[qid] = row
    rows = []
    for qid, cap in captured.items():
        route = cap.get('route') or {}
        original = cap['original_qid']
        source = sources[original]
        context, citations = selected_citations(source['messages'][0]['content'])
        assert sha(context.encode('utf-8')) == cap['frozen_context_reference']['context_sha256']
        actual_refs = [(c['citation_label'], c['source_file'], c['content_sha256']) for c in citations]
        captured_refs = [(c['citation_label'], c['source_file'], c['content_sha256']) for c in cap['citations']]
        assert actual_refs == captured_refs, 'delivered citation identity mismatch: ' + qid
        collision = measure_target_field_collision(cap['question'], citations)
        old = replay(cap['question'], route, 'WEIGHTED_V1')
        new = replay(cap['question'], route, 'AFFINITY_V2', {'target_field_collision': collision})
        if old['role'] != 'UNKNOWN' and old['role'] != route['role']:
            raise ValueError('V1 replay differs: ' + qid)
        if old['role'] == 'BASELINE' and route.get('model') != 'Qwen/Qwen3.5-4B':
            raise ValueError('BASELINE is not Cheap: ' + qid)
        review = reviews.get(qid)
        split = splits.get(original, {})
        item = {'qid': qid, 'question': cap['question'], 'original_qid': original,
                'source_group': source['source_group'],
                'historical_split': split.get('split', 'UNKNOWN'),
                'split_is_blind': False, 'messages_sha256': cap['messages_sha256'],
                'old': old, 'candidate': new, 'execution_status': cap['execution_status'],
                'target_field_collision': collision,
                'cheap_core': review['cheap']['core'] if review else 'UNKNOWN',
                'current_dynamic_core': review['dynamic']['core'] if review else 'UNKNOWN',
                'expensive_core': 'UNKNOWN', 'expensive_label_origin': 'MISSING_ARM',
                'current_model': cap.get('dynamic', {}).get('actual_model')}
        if review:
            assert review['input_sha256'] == cap['messages_sha256']
            assert review['dynamic_answer'] == cap['dynamic']['actual_answer']
            assert review['cheap_answer'] == cap['historical_cheap']['actual_answer']
        if qid in pairs:
            pair = pairs[qid]
            item.update(cheap_core=pair['cheap']['coverage'], expensive_core=pair['expensive']['coverage'],
                        expensive_label_origin='HISTORICAL36_AI_REVIEW',
                        cheap_execution_status=pair['cheap']['request_state'])
        elif review and cap.get('dynamic', {}).get('actual_model') == 'zai-org/GLM-5.3-Flash':
            assert cap['dynamic']['messages_sha256'] == cap['historical_cheap']['messages_sha256']
            item.update(expensive_core=review['dynamic']['core'], expensive_label_origin='DYNAMIC_AI_REVIEW')
        item['old_replay_core'] = outcome(old['role'], item['cheap_core'], item['expensive_core'])
        item['candidate_replay_core'] = outcome(new['role'], item['cheap_core'], item['expensive_core'])
        item['missing_selected_arm'] = new['role'] == 'EXPENSIVE' and item['expensive_label_origin'] == 'MISSING_ARM'
        rows.append(item)
    paired = [r for r in rows if r['qid'] in pairs]
    rescue = [r for r in paired if r['cheap_core'] in ('FAIL', 'PARTIAL') and r['expensive_core'] == 'PASS']
    persistent = [r for r in paired if r['current_model'] == 'Qwen/Qwen3.5-4B'
                  and r['current_dynamic_core'] in ('FAIL', 'PARTIAL') and r['expensive_core'] == 'PASS']
    summary = {
        'type': 'OFFLINE_COUNTERFACTUAL_REPLAY_NOT_REAL_GENERATION', 'new_provider_calls': 0,
        'v1_replay_matches_captured': sum(r['old']['role'] != 'UNKNOWN' for r in rows),
        'scope': len(rows), 'completed_dynamic_reviews': len(reviews), 'paired36_identity_pass': len(identity_checks),
        'roles_old': dict(Counter(r['old']['role'] for r in rows)),
        'roles_candidate': dict(Counter(r['candidate']['role'] for r in rows)),
        'new_expensive_missing_arm': sum(r['missing_selected_arm'] for r in rows),
        'paired36': {k: summarize(paired, k) for k in ('cheap_core', 'expensive_core', 'old_replay_core', 'candidate_replay_core')},
        'paired36_saved_call_metrics': {k: saved_call_metrics(paired, attempts, k) for k in ('old', 'candidate')},
        'rescue_coverage': {'n': len(rescue), 'old': sum(r['old']['role'] == 'EXPENSIVE' for r in rescue),
                            'candidate': sum(r['candidate']['role'] == 'EXPENSIVE' for r in rescue)},
        'persistent_qwen_nonpass_glm_pass': {'n': len(persistent),
            'candidate': sum(r['candidate']['role'] == 'EXPENSIVE' for r in persistent),
            'missed_qids': [r['qid'] for r in persistent if r['candidate']['role'] != 'EXPENSIVE']},
        'historical_partition_diagnostics': {
            split: {k: summarize([r for r in paired if r['historical_split'] == split], k)
                    for k in ('old_replay_core', 'candidate_replay_core')}
            for split in sorted(set(r['historical_split'] for r in paired))},
        'matched_rules': dict(Counter(rule for r in rows for rule in r['candidate'].get('matched_rules', []))),
        'limitations': ['Labels were inspected before this candidate; historical HOLDOUT is not blind validation.',
                       'Historical36 is a selected hard cohort, not population accuracy.',
                       'Offline choices reuse stochastic saved outputs; no new measured quality, cost or latency.',
                       'Provider-reported model identity is not independent provider attestation.',
                       'Quality covers core only; citations, extras, missing evidence and UNKNOWN remain separate.',
                       'Recorded input hashes match; not all historical provider settings have independently recoverable snapshots.'],
    }
    for name, path in paths.items():
        assert sha(path.read_bytes()) == hashes[name], 'input mutated: ' + name
    output.mkdir(parents=True, exist_ok=True)
    payload = {'version': 'router-affinity-v2-offline-v1', 'source_hashes': hashes,
               'code_hashes': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
                               (ROOT / 'backend/app/services/model_router.py', Path(__file__).resolve())},
               'candidate_parameters': {'selection_policy': 'AFFINITY_V2', 'task_risk_floor': 0.55,
                                         'threshold_and_weights': 'UNCHANGED_CAPTURED_VALUES'},
               'summary': summary, 'identity_checks': identity_checks, 'rows': rows}
    (output / 'replay.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return payload


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT / 'var/dynamic-routing-20261101')
    parser.add_argument('--out', type=Path, default=ROOT / 'var/dynamic-routing-20261101/affinity-v2')
    args = parser.parse_args()
    run(args.root, args.out)
