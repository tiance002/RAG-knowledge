"""Offline finalizer for the frozen routing pilot; no provider or Judge calls."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temp, 'x', encoding='utf-8', newline='\n') as f:
        os.chmod(temp, 0o600)
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)


def wait_service(unit, poll_seconds=30):
    """Wait on local systemd only; never opens SSH or network connections."""
    while True:
        result = subprocess.run(['systemctl', 'show', unit, '--property=ActiveState', '--value'],
                                capture_output=True, text=True, check=True)
        state = result.stdout.strip()
        if state in ('inactive', 'failed'):
            details = subprocess.run(['systemctl', 'show', unit, '--property=Result,ExecMainStatus'],
                                     capture_output=True, text=True, check=True)
            return {'unit': unit, 'active_state': state, 'result': details.stdout.strip()}
        if state not in ('active', 'activating', 'reloading', 'deactivating', 'refreshing'):
            raise ValueError('unrecognized local service state')
        time.sleep(poll_seconds)


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def usage_summary(attempts):
    result = {}
    for label, keys in {'input': ('prompt_tokens', 'input_tokens', 'input'),
                        'output': ('completion_tokens', 'output_tokens', 'output'),
                        'total': ('total_tokens', 'total')}.items():
        known = []
        for _, _, info in attempts:
            usage = info.get('usage')
            value = next((usage[k] for k in keys if k in usage), None) if isinstance(usage, dict) else None
            if type(value) is int and value >= 0:
                known.append(value)
        result[label] = {'reported_subtotal': sum(known), 'reported_attempts': len(known),
                         'unknown_attempts': len(attempts) - len(known),
                         'exact_all_attempts_total': sum(known) if len(known) == len(attempts) else None}
    return result


def lexical_signal(gold, answer):
    """Literal matching is a diagnostic only, never semantic correctness."""
    strings = []
    def walk(value):
        if isinstance(value, str) and value.strip():
            strings.append(value.strip())
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(gold.get('answer_points', gold.get('answer')) if isinstance(gold, dict) else gold)
    return {'label': 'ONLY_DIAGNOSTIC_NOT_CORRECTNESS', 'gold_string_count': len(strings),
            'literal_substring_matches': sum(s.casefold() in answer.casefold() for s in strings),
            'limitation': 'Only answer-reference strings are tested; substring matches do not prove correctness.'}


def trace_url(base, trace_id):
    if not base or not trace_id:
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('trace base must be a public UI URL without credentials or query')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(trace_id)):
        raise ValueError('invalid trace identifier')
    return base.rstrip('/') + '/traces/' + str(trace_id)


def load_reviews(path):
    records = {}
    if path:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if line.strip():
                item = json.loads(line)
                if item['qid'] in records:
                    raise ValueError('duplicate reviewed qid')
                records[item['qid']] = item
    return records


def finalize(manifest_path, ledger_path, output, reviews_path=None, trace_base_url=None, service=None):
    os.umask(0o077)
    raw_manifest = Path(manifest_path).read_bytes()
    manifest = json.loads(raw_manifest.decode('utf-8-sig'))
    identity = sha(canonical(manifest))
    cases = manifest['cases']
    qids = [c['qid'] for c in cases]
    if len(qids) != len(set(qids)) or len(qids) > 150 or not qids:
        raise ValueError('invalid frozen scope')
    for case in cases:
        if sha(canonical(case['messages'])) != case['messages_sha256']:
            raise ValueError('frozen message hash mismatch')
    uri = Path(ledger_path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        db.execute('begin')
        row = db.execute("select value from metadata where key='manifest_sha256'").fetchone()
        if not row or row[0] != identity:
            raise ValueError('ledger manifest identity mismatch')
        rows = db.execute('select qid,role,state,info from attempts').fetchall()
        events = db.execute('select qid,role,state from events order by id').fetchall()
    attempts = {}
    for qid, role, state, info in rows:
        if qid not in qids or role not in ('cheap', 'expensive') or (qid, role) in attempts:
            raise ValueError('invalid or duplicate ledger attempt')
        attempts[qid, role] = (state, json.loads(info))
    pending_counts = {}
    final_counts = {}
    for qid, role, state in events:
        if (qid, role) not in attempts:
            raise ValueError('orphan attempt event')
        target = pending_counts if state == 'UNKNOWN_PENDING' else final_counts
        target[qid, role] = target.get((qid, role), 0) + 1
    no_replay = all(pending_counts.get(key) == 1 and final_counts.get(key, 0) <= 1
                    and (final_counts.get(key, 0) == 0) == (attempts[key][0] == 'UNKNOWN_PENDING') for key in attempts)
    if not no_replay:
        raise ValueError('event ledger does not verify one send per attempt')
    reviews = load_reviews(reviews_path)
    if not set(reviews).issubset(qids):
        raise ValueError('review outside frozen scope')
    evidence = []
    judgments = {'PASS': 0, 'FAIL': 0, 'UNKNOWN': 0}
    excluded = []
    for case in cases:
        qid = case['qid']
        state, info = attempts.get((qid, 'cheap'), ('UNVISITED', {}))
        answer = info.get('text', '')
        review = reviews.get(qid)
        judgment = 'UNKNOWN'
        if review:
            if not (state == 'COMPLETED' and info.get('finish_reason') == 'stop'
                    and review.get('core_correctness') in ('PASS', 'FAIL')
                    and review.get('manifest_sha256') == identity
                    and review.get('answer_sha256') == info.get('answer_sha256')
                    and review.get('reviewer') and review.get('reason') and review.get('evidence_refs')):
                raise ValueError('invalid semantic review')
            judgment = review['core_correctness']
        if case.get('excluded_from_core_accuracy', False):
            excluded.append(qid)
        else:
            judgments[judgment] += 1
        gold = case.get('gold', case.get('gold_refs'))
        evidence.append({'qid': qid, 'question': case['question'], 'gold': gold,
            'excluded_from_core_accuracy': bool(case.get('excluded_from_core_accuracy', False)),
            'cheap_state': state, 'actual_answer': answer, 'answer_sha256': info.get('answer_sha256'),
            'finish_reason': info.get('finish_reason'), 'provider_request_id': info.get('provider_request_id'),
            'model': info.get('actual_model'), 'messages_sha256': case['messages_sha256'],
            'context_sha256': case.get('context_sha256'),
            'source_refs': case.get('source_refs', case.get('context_refs', case.get('retrieved_top10'))),
            'frozen_messages_reference': {'manifest': str(manifest_path), 'qid': qid,
                                          'messages_sha256': case['messages_sha256']},
            'trace_id': info.get('trace_id'),
            'trace_url': trace_url(trace_base_url, info.get('trace_id')),
            'core_correctness': judgment, 'semantic_review': review,
            'lexical_gold_coverage': lexical_signal(gold, answer)})
    lane_summaries = {}
    for role in ('cheap', 'expensive'):
        subset = [(qid, state, info) for (qid, lane), (state, info) in attempts.items() if lane == role]
        counts = {state: sum(s == state for _, s, _ in subset) for state in
                  ('COMPLETED', 'EMPTY', 'TRUNCATED', 'HTTP_FAILURE', 'PROVIDER_FAILURE', 'UNKNOWN', 'UNKNOWN_PENDING')}
        known_states = set(counts)
        if any(state not in known_states for _, state, _ in subset):
            raise ValueError('unrecognized attempt state')
        counts['UNVISITED'] = len(cases) - len(subset)
        latency = [info['latency_ms'] for _, _, info in subset if number(info.get('latency_ms'))]
        lane_summaries[role] = {'counts': counts, 'attempts': len(subset),
            'provider_usage': usage_summary(subset), 'attempt_latency_ms':
            {'n': len(latency), 'p50': percentile(latency, .5), 'p95': percentile(latency, .95),
             'method': 'linear interpolation across recorded nonnegative attempt latencies'}}
    denominator = len(cases) - len(excluded)
    incomplete = (lane_summaries['cheap']['counts']['UNVISITED'] > 0
                  or lane_summaries['cheap']['counts']['UNKNOWN_PENDING'] > 0)
    status = ('EXECUTION_INCOMPLETE_WITH_REVIEW_PENDING' if incomplete else
              'COMPLETED_WITH_REVIEW_PENDING' if judgments['UNKNOWN'] else 'COMPLETED_WITH_REVIEWED_RESULTS')
    nonempty_eligible = sum(not c.get('excluded_from_core_accuracy', False)
                           and attempts.get((c['qid'], 'cheap'), (None, {}))[0] == 'COMPLETED'
                           and bool(attempts[c['qid'], 'cheap'][1].get('text', '').strip()) for c in cases)
    summary = {'status': status, 'scope': len(cases), 'dataset_version': manifest.get('dataset_version'),
        'manifest_sha256': identity, 'manifest_file_sha256': sha(raw_manifest),
        'verification': {'manifest_identity': True, 'unique_attempts': True,
                         'one_pending_event_per_attempt_no_replay': no_replay, 'event_count': len(events)},
        'lanes': lane_summaries, 'excluded_qids': excluded, 'core_accuracy_maximum_eligible': denominator,
        'nonempty_completed_eligible': nonempty_eligible,
        'core_accuracy_denominator': None if judgments['UNKNOWN'] else nonempty_eligible,
        'semantic_review_counts': judgments, 'reviewed_core_count': judgments['PASS'] + judgments['FAIL'],
        'final_core_accuracy': judgments['PASS'] / denominator if denominator and not judgments['UNKNOWN'] else None,
        'quality_status': 'SEMANTIC_REVIEW_PENDING' if judgments['UNKNOWN'] else 'REVIEWED',
        'cost_status': 'UNKNOWN', 'savings_status': 'UNKNOWN',
        'limitations': ['Cheap describes the selected tier; it does not prove cost savings.',
                       'Lexical Gold coverage is diagnostic only and is not correctness.',
                       'No provider, Judge, or automatic expensive escalation calls are made by this finalizer.'],
        'service': service}
    lines = ['# Routing150 unified review', '',
             '状态：' + status + '。此文件已生成，不代表模型质量验收完成。', '',
             '核心题最大可选数：' + str(denominator) + '；非空完整可审数：' + str(nonempty_eligible)
             + '；最终正确率分母待复核，空答案、跳过及UNKNOWN单列；排除题：' + json.dumps(excluded, ensure_ascii=False) + '。',
             '语义审查计数：' + json.dumps(judgments, ensure_ascii=False) + '。',
             '成本与节省金额 UNKNOWN；cheap 只是层级名称。Gold 字面覆盖仅为诊断，不是正确性判定。', '',
             '来源定位与哈希见 source_evidence.json；完整上下文通过其中的冻结manifest引用回读，避免复制。所有未审查题的 core_correctness 为 UNKNOWN。', '']
    for item in evidence:
        lines += ['## ' + item['qid'], '', item['question'], '',
                  '- 执行状态：' + item['cheap_state'], '- 核心正确性：' + item['core_correctness'],
                  '- 统计排除：' + str(item['excluded_from_core_accuracy']),
                  '- 消息哈希：' + item['messages_sha256'],
                  '- 上下文哈希：' + str(item['context_sha256']),
                  '- Trace：' + str(item['trace_url'] or item['trace_id'] or 'UNKNOWN'), '',
                  'Gold（仅作人工审查参考）：', '', '```json', json.dumps(item['gold'], ensure_ascii=False, indent=2), '```', '',
                  '实际回答：', '', item['actual_answer'] or '（无可用回答）', '',
                  '来源引用：', '', '```json', json.dumps(item['source_refs'], ensure_ascii=False, indent=2), '```', '']
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    artifacts = {'summary.json': json.dumps(summary, ensure_ascii=False, indent=2),
                 'source_evidence.json': json.dumps({'manifest_sha256': identity, 'cases': evidence}, ensure_ascii=False, indent=2),
                 'review.md': '\n'.join(lines)}
    for name, content in artifacts.items():
        atomic(output / name, content)
    completion = {'status': status, 'manifest_sha256': identity,
                  'artifacts_sha256': {name: sha(text.encode('utf-8')) for name, text in artifacts.items()}}
    atomic(output / 'completion.json', json.dumps(completion, ensure_ascii=False, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--wait-service')
    p.add_argument('--reviews')
    p.add_argument('--trace-base-url')
    args = p.parse_args()
    try:
        service = wait_service(args.wait_service) if args.wait_service else None
        result = finalize(args.manifest, args.ledger, args.output, args.reviews, args.trace_base_url, service)
        print(json.dumps({'status': result['status'], 'scope': result['scope'],
                          'quality_status': result['quality_status']}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
