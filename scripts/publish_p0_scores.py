"""Publish deterministic, redacted scores; default is a network-free dry run."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def score_payloads(records):
    seen = set()
    payloads = []
    for record in records:
        trace = record['trace_id']
        version = record['experiment_version']
        def add(name, value, kind):
            identity = hashlib.sha256(f'{trace}|{version}|{name}|p0-v1'.encode()).hexdigest()
            if identity in seen:
                raise ValueError('Duplicate score identity')
            seen.add(identity)
            payloads.append(dict(score_id=identity, trace_id=trace, name='p0.' + name,
                                 value=value, data_type=kind,
                                 metadata={'experiment_version': version, 'method': 'deterministic-p0-v1'}))
        for stage, state in record['evidence'].items():
            add('evidence.' + stage, state, 'CATEGORICAL')
        for name, value in record['scores'].items():
            if value is None or isinstance(value, dict):
                continue
            if isinstance(value, bool):
                add(name, float(value), 'BOOLEAN')
            elif isinstance(value, (int, float)):
                add(name, float(value), 'NUMERIC')
        usage = record['scores'].get('tokens') or {}
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if isinstance(usage.get(name), int):
                add('generation.' + name, float(usage[name]), 'NUMERIC')
    return payloads



def e11_score_payloads(records):
    """Build immutable scores; categorical UNKNOWN remains explicit, numeric UNKNOWN is omitted."""
    import math
    import re
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    from app.services.metric_catalog import CATEGORICAL_ENUMS, METRIC_CATALOG, METRIC_CATALOG_VERSION
    out, identities = [], set()
    for record in records:
        for key in ('trace_id', 'answer_sha256', 'rubric_version', 'evaluation_source'):
            if not record.get(key):
                raise ValueError('missing score linkage: ' + key)
        if not re.fullmatch(r'[0-9a-f]{64}', record['answer_sha256']):
            raise ValueError('invalid answer_sha256')
        if record.get('reviewer_type') and not record.get('evidence_scope'):
            raise ValueError('review scores require evidence_scope')
        for name, value in record['metrics'].items():
            if value is None or value == 'NOT_AVAILABLE':
                continue
            catalog_key = name
            if name.startswith('eval.evidence_state.'):
                catalog_key = 'eval.evidence_state.<stage>'
            elif name.startswith('eval.evidence_coverage.'):
                catalog_key = 'eval.evidence_coverage.<stage>'
            if catalog_key not in METRIC_CATALOG:
                raise ValueError('unknown metric: ' + name)
            if name in ('eval.core_correctness', 'eval.core_pass', 'eval.answer_completeness', 'eval.faithfulness', 'eval.citation_support', 'eval.citation_coverage', 'eval.abstention') and not record.get('reviewer_type'):
                raise ValueError('review scores require reviewer_type')
            if name.startswith('retrieval.'):
                if any(record.get(key) is None for key in ('grain', 'gold_grain', 'observed_grain', 'k', 'stage', 'qrel_version', 'gold_completeness')):
                    raise ValueError('ranking scores require qrel metadata')
                if not record['grain'] or record['grain'] != record['gold_grain'] or record['grain'] != record['observed_grain']:
                    raise ValueError('ranking scores require matching explicit grains')
                if record['gold_completeness'] is not True and record['gold_completeness'] is not False:
                    raise ValueError('gold completeness must be boolean')
                if not isinstance(record['k'], int) or isinstance(record['k'], bool) or record['k'] <= 0:
                    raise ValueError('ranking scores require positive k')
                if value not in ('UNKNOWN', 'NOT_RUN'):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError('invalid ranking score')
                    if name == 'retrieval.hit_at_k':
                        if value not in (0, 1): raise ValueError('hit must be 0 or 1')
                        if value == 0 and record['gold_completeness'] is not True:
                            raise ValueError('miss requires complete gold')
                    elif record['gold_completeness'] is not True:
                        raise ValueError('ranking score requires complete gold')
            kind = METRIC_CATALOG[catalog_key][0]
            if kind == 'CATEGORICAL' and (not isinstance(value, str) or value not in CATEGORICAL_ENUMS[catalog_key]):
                raise ValueError('invalid categorical value for ' + name)
            if name == 'eval.core_pass':
                core = record['metrics'].get('eval.core_correctness')
                if not isinstance(value, bool) or core not in ('PASS', 'FAIL') or value != (core == 'PASS'):
                    raise ValueError('core_pass requires matching PASS or FAIL verdict')
            if catalog_key in ('eval.evidence_coverage.<stage>', 'eval.evidence_state.<stage>'):
                needs_gold = value not in ('UNKNOWN', 'NOT_RUN')
                if needs_gold and (record.get('gold_completeness') is not True or not record.get('grain') or record.get('gold_grain') != record.get('grain') or record.get('observed_grain') != record.get('grain')):
                    raise ValueError('evidence score requires complete same-grain gold')
                if catalog_key == 'eval.evidence_coverage.<stage>' and needs_gold and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError('invalid evidence coverage')
            if name == 'eval.citation_coverage' and value not in ('UNKNOWN', 'NOT_RUN'):
                n, d = record.get('supported_claims'), record.get('citable_claims')
                if isinstance(n, bool) or not isinstance(n, int) or isinstance(d, bool) or not isinstance(d, int) or d <= 0 or not 0 <= n <= d:
                    raise ValueError('citation coverage requires valid claim counts')
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not math.isclose(value, n / d, rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError('citation coverage must equal supported claims ratio')
            if value in ('UNKNOWN', 'NOT_RUN') and kind != 'CATEGORICAL': continue
            if kind == 'BOOLEAN':
                if not isinstance(value, bool): raise ValueError('invalid BOOLEAN')
                value = float(value)
            elif kind == 'NUMERIC':
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): raise ValueError('invalid NUMERIC')
                value = float(value)
            elif not isinstance(value, str):
                raise ValueError('invalid text value')
            identity_parts = [record['trace_id'], record.get('observation_id'), record['answer_sha256'], name, record['rubric_version'], record['evaluation_source'], record.get('reviewer_type'), record.get('reviewer_id'), record.get('stage'), record.get('grain'), record.get('k'), record.get('qrel_version'), METRIC_CATALOG_VERSION]
            identity = hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            if identity in identities: raise ValueError('Duplicate score identity')
            identities.add(identity)
            metadata = {k: v for k, v in record.items() if k not in ('metrics', 'trace_id', 'observation_id')}
            metadata['metric_catalog_version'] = METRIC_CATALOG_VERSION
            row = dict(score_id=identity, trace_id=record['trace_id'], name=name, value=value, data_type=kind, metadata=metadata)
            if record.get('observation_id'): row['observation_id'] = record['observation_id']
            out.append(row)
    return out


def verify_score_readback(payloads, rows):
    """Check every identity, typed value, subject and provenance; missing data fails closed."""
    found = {row.id: row for row in rows}
    errors = []
    for expected in payloads:
        row = found.get(expected['score_id'])
        if row is None:
            errors.append((expected['score_id'], 'missing'))
            continue
        subject = getattr(row, 'subject', None)
        kind = getattr(subject, 'kind', None)
        trace_id = getattr(subject, 'trace_id', None) if kind == 'observation' else getattr(subject, 'id', None) if kind == 'trace' else None
        observation_id = getattr(subject, 'id', None) if kind == 'observation' else None
        fields = {'name': getattr(row, 'name', None), 'value': getattr(row, 'value', None), 'data_type': getattr(row, 'data_type', None), 'trace_id': trace_id, 'observation_id': observation_id}
        for key, actual in fields.items():
            if actual != expected.get(key): errors.append((expected['score_id'], key))
        metadata = getattr(row, 'metadata', None) or {}
        for key, value in expected['metadata'].items():
            if metadata.get(key) != value: errors.append((expected['score_id'], 'metadata.' + key))
        if str(getattr(row, 'source', '')) != 'API': errors.append((expected['score_id'], 'source'))
    return errors


def preflight_subjects(client, payloads, local_bindings=None, details=None):
    """Verify root answer linkage and optional observation ownership through SDK reads."""
    errors = []
    for trace in dict.fromkeys(p['trace_id'] for p in payloads):
        related = [p for p in payloads if p['trace_id'] == trace]
        observations, cursor, seen_cursors = [], None, set()
        while True:
            page = client.api.observations.get_many(trace_id=trace, fields='core,basic,io,metadata', limit=100, cursor=cursor)
            observations.extend(page.data)
            cursor = getattr(page.meta, 'cursor', None)
            if not cursor: break
            if cursor in seen_cursors: raise ValueError('observation pagination cursor did not advance')
            seen_cursors.add(cursor)
        observations = [row for row in observations if getattr(row, 'trace_id', None) == trace]
        roots = [row for row in observations if getattr(row, 'is_root_observation', False) is True]
        if len(roots) != 1:
            errors.append((trace, 'missing_or_ambiguous_root'))
            continue
        root = roots[0]
        metadata = getattr(root, 'metadata', None) or {}
        answer = getattr(root, 'output', None)
        for p in related:
            expected_sha = p['metadata']['answer_sha256']
            matching_content = isinstance(answer, str) and hashlib.sha256(answer.encode('utf8')).hexdigest() == expected_sha
            matching_metadata = metadata.get('final_answer_sha256') == expected_sha
            binding = (local_bindings or {}).get(p['metadata'].get('qid'))
            matching_local = bool(binding and binding['trace_id'] == trace and binding['answer_sha256'] == expected_sha and binding['root_observation_id'] == getattr(root, 'id', None))
            if details is not None and p['metadata'].get('qid') not in {d['qid'] for d in details}:
                details.append({'qid': p['metadata'].get('qid'), 'trace_id': trace, 'cloud_root_observation_id': getattr(root, 'id', None), 'answer_sha256': expected_sha, 'linkage_mode': 'immutable_local_case_and_cloud_root_identity' if matching_local else 'cloud_root_content' if matching_content else 'cloud_root_answer_sha256' if matching_metadata else 'UNCONFIRMED', 'cloud_answer_hash_confirmed': bool(matching_content or matching_metadata)})
            if not (matching_content or matching_metadata or matching_local): errors.append((p['score_id'], 'answer_link_not_confirmed'))
            obs_id = p.get('observation_id')
            if obs_id and not any(getattr(row, 'id', None) == obs_id for row in observations):
                errors.append((p['score_id'], 'observation_not_in_trace'))
    return errors

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--e11', action='store_true', help='Use reviewed E1.1 score records')
    parser.add_argument('--payload-output', type=Path, help='Write dry-run score JSONL')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='Only read back existing deterministic score IDs')
    parser.add_argument('--preflight-only', action='store_true', help='Only perform read-only E1.1 subject preflight')
    parser.add_argument('--historical-ledger', type=Path, help='Frozen historical ledger for local case binding')
    parser.add_argument('--historical-root', type=Path, default=Path('.'))
    parser.add_argument('--historical-ledger-sha256', help='Expected frozen ledger SHA256')
    args = parser.parse_args()
    records = [json.loads(x) for x in args.input.read_text(encoding='utf-8').splitlines() if x.strip()]
    payloads = e11_score_payloads(records) if args.e11 else score_payloads(records)
    if args.payload_output:
        args.payload_output.write_text(''.join(json.dumps(p, ensure_ascii=False, sort_keys=True) + '\n' for p in payloads), encoding='utf-8')
    if args.preflight_only and (not args.e11 or args.publish or args.verify_only):
        raise ValueError('--preflight-only requires --e11 and cannot be combined with publish or verify')
    online = args.publish or args.verify_only or args.preflight_only
    report = {'dry_run': not online, 'verify_only': args.verify_only, 'preflight_only': args.preflight_only, 'records': len(records), 'scores': len(payloads), 'verified': False}
    if online:
        if not args.config and not args.preflight_only:
            raise ValueError('--config required for publish or verify')
        for line in (args.config.read_text(encoding='utf-8-sig').splitlines() if args.config else []):
            if '=' in line and not line.strip().startswith('#'):
                key, value = line.split('=', 1)
                if key.strip() in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL'):
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
        from langfuse import Langfuse
        client = Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'], timeout=30)
        try:
            bindings = {}
            if args.historical_ledger:
                import sys
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from import_e1_reviews import historical_local_binding
                ledger_bytes = args.historical_ledger.read_bytes()
                if not args.historical_ledger_sha256 or hashlib.sha256(ledger_bytes).hexdigest() != args.historical_ledger_sha256:
                    raise ValueError('historical ledger hash mismatch')
                ledger = [json.loads(line) for line in ledger_bytes.decode('utf8').splitlines() if line.strip()]
                qids = {p['metadata'].get('qid') for p in payloads}
                for row in ledger:
                    if row.get('qid') in qids:
                        if row['qid'] in bindings: raise ValueError('duplicate historical qid')
                        bindings[row['qid']] = historical_local_binding(row, args.historical_root)
                if set(bindings) != qids: raise ValueError('historical qid missing')
            if args.e11:
                details = []
                errors = preflight_subjects(client, payloads, bindings, details)
                report['subjects'] = details
                report['preflight_errors'] = errors
                report['preflight_status'] = 'BLOCKED' if errors else 'PASS'
                report['local_binding_count'] = len(bindings)
                report['binding_evidence'] = 'see_subjects'
                if errors or args.preflight_only:
                    report['verified'] = not errors
                    args.output.write_text(json.dumps(report, indent=2), encoding='utf8')
                    print(json.dumps(report))
                    if errors: raise SystemExit(1)
                    return
            if not args.verify_only:
                for payload in payloads:
                    client.create_score(**payload)
                client.flush()
            rows = []
            for trace in dict.fromkeys(p['trace_id'] for p in payloads):
                cursor = None
                while True:
                    page = client.api.scores.get_many(trace_id=trace, limit=100, cursor=cursor, fields='details,subject')
                    rows.extend(page.data)
                    next_cursor = getattr(page.meta, 'cursor', None)
                    if not next_cursor: break
                    if next_cursor == cursor: raise ValueError('score pagination cursor did not advance')
                    cursor = next_cursor
            errors = verify_score_readback(payloads, rows)
            report['verified'] = not errors
            report['missing_scores'] = sum(1 for _, error in errors if error == 'missing')
            report['readback_errors'] = errors
        finally:
            client.shutdown()
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if online and not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
