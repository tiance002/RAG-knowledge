"""Version-separated metadata projections; raw model/evidence text never leaves here."""
from .reader import digest
from hashlib import sha256

DIMENSIONS = ('experiment_id', 'epoch', 'role_profile', 'agent_role', 'evidence_mode',
              'parent_id', 'candidate_id', 'compatibility_fingerprint', 'evaluator_version', 'profile')
REQUEST_EVENTS = {'MODEL_REQUEST_STARTED', 'MODEL_REQUEST_COMPLETED', 'MODEL_REQUEST_POSTHOC_COST_BOUND'}
SAFE = {'round_id', 'lineage_id', 'entry_id', 'proposal_id', 'candidate_id', 'parent_id', 'family_id',
        'request_key', 'request_hash', 'model', 'price_version', 'usage_status', 'cost_nano_upper',
        'verdict', 'reason_codes', 'reason', 'status', 'execution_state', 'stop_reason', 'bundle_hash',
        'receipt_sha256', 'receipt_hash', 'result_hash', 'round_result_sha256', 'holdout_freeze_hash',
        'observed_effective_gain', 'complete_trusted_no_gain', 'statistical_denominator', 'auxiliary_cases',
        'association_only', 'production_changed', 'mode', 'slot', 'angle', 'worker_hash', 'config_hash',
        'eligible_count', 'total_questions', 'pre_blocked', 'delta_total', 'reduction_rate',
        'gates_passed', 'family_size', 'independent_holdout', 'adopted_candidate_id', 'adopted_proposal_id',
        'real_eligible', 'static_proposal_instances', 'ast_unique_strategy_count', 'version_id',
        'artifact_hash', 'deployment_revision', 'operation_id', 'source_group_count', 'source_count'}


def _safe(payload):
    # Only scalar metadata and lists of scalar reason codes; never arbitrary nesting.
    return {k: v for k, v in payload.items() if k in SAFE and
            (v is None or isinstance(v, (str, bool, int, float)) or
             k == 'reason_codes' and isinstance(v, list) and all(isinstance(x, str) for x in v))}


def _dimensions(payload):
    result = {k: 'UNKNOWN' for k in DIMENSIONS}
    for k in DIMENSIONS:
        value = payload.get(k)
        if k == 'role_profile' and isinstance(value, dict):
            value = value.get('version')
        if value is not None and isinstance(value, (str, int)):
            result[k] = value
    if result['agent_role'] == 'UNKNOWN':
        result['agent_role'] = payload.get('role', 'UNKNOWN')
    if result['profile'] == 'UNKNOWN':
        result['profile'] = payload.get('seam_profile', 'UNKNOWN')
    if payload.get('mode') == 'SIMULATION_ONLY':
        if result['evidence_mode'] not in {'UNKNOWN', 'ENGINEERING_DEMO'}:
            raise ValueError('ANALYTICS_SIMULATION_MODE_CONFLICT')
        result['evidence_mode'] = 'ENGINEERING_DEMO'
    # Same advertised version with a different manifest is a different cohort.
    profile = payload.get('role_profile')
    result['role_profile_sha256'] = digest(profile) if isinstance(profile, dict) else 'UNKNOWN'
    return result


def _record(kind, identity, payload, refs, event_ids=(), dimensions=None):
    return {'projection_type': kind, 'id': identity, 'event_ids': sorted(set(event_ids)),
            'source_refs': refs, 'dimensions': dimensions or _dimensions(payload), 'payload': _safe(payload)}


def _event_kind(kind):
    if kind.startswith('ROUND_') or kind == 'AUTONOMY_ROUND_FINISHED': return 'Round'
    if 'PROMOT' in kind or 'ROLLBACK' in kind or kind == 'ROLLED_BACK': return 'Promotion'
    if 'HOLDOUT' in kind or kind.startswith('QUERY_SEALED_'): return 'Holdout'
    if kind.startswith('PARENT_'): return 'Parent'
    if kind.startswith('CANDIDATE_') or kind.startswith('QUERY_CANDIDATE_'): return 'Candidate'
    if kind.startswith('PROPOSAL_') and kind != 'PROPOSAL_REFERENCED': return 'Proposal'
    if any(x in kind for x in ('HANDBOOK', 'MEMORY', 'EXPERIENCE', 'ENTRY_', 'LEARNING', 'OUTCOME_', 'PROPOSAL_REFERENCED', 'DEPRECATION')): return 'MemoryEvent'
    if 'REPLAY' in kind: return 'Replay'
    return 'Experiment'


def project_sources(sources, artifacts=()):
    artifacts = tuple(artifacts)
    records, unique_sources, seen_events, requests = [], {}, {}, {}
    for source in sources:
        sid = source['source_id']
        content = {'sha256': source['sha256'], 'events': source['events']}
        if sid in unique_sources:
            if unique_sources[sid] != content: raise ValueError('ANALYTICS_SOURCE_IDENTITY_CONFLICT')
            continue
        unique_sources[sid] = content
        ref = {'source_id': sid, 'sha256': source['sha256'], 'path': source['path']}
        for event in source['events']:
            eid, kind, p = event['event_id'], event['event_type'], event['payload']
            event_identity = (sid, eid)
            if event_identity in seen_events and seen_events[event_identity] != event:
                raise ValueError('ANALYTICS_EVENT_IDENTITY_CONFLICT')
            if event_identity in seen_events: continue
            seen_events[event_identity] = event
            if kind in REQUEST_EVENTS:
                key = p.get('request_key')
                if not key: raise ValueError('ANALYTICS_REQUEST_KEY_MISSING')
                bucket = requests.setdefault((sid, key), {'ref': ref, 'events': {}})
                if kind in bucket['events']: raise ValueError('ANALYTICS_REQUEST_EVENT_CONFLICT')
                bucket['events'][kind] = event
            else:
                safe = dict(p)
                safe['event_type'] = kind
                if kind in {'PROMOTED', 'ROLLED_BACK'}: safe.update(p.get('current', {}))
                result = _record(_event_kind(kind), sid + ':' + eid, safe, [ref], [eid])
                result['payload']['event_type'] = kind
                if result['projection_type'] == 'Promotion':
                    result['payload'].update(status=kind, sequence=event['sequence'], history_source_id=sid)
                    environment = p.get('current', p.get('next', {})).get('environment')
                    result['payload']['history_environment'] = environment if isinstance(environment, str) else 'UNKNOWN'
                    if kind in {'PROMOTED', 'ROLLED_BACK'}:
                        result['dimensions']['candidate_id'] = p.get('current', {}).get('version_id', 'UNKNOWN')
                        result['payload']['candidate_id'] = p.get('current', {}).get('version_id', 'UNKNOWN')
                    if kind == 'ROLLED_BACK':
                        result['payload']['rolled_back_candidate_id'] = p.get('previous', {}).get('version_id', 'UNKNOWN')
                if result['projection_type'] == 'Holdout':
                    result['payload']['completed'] = kind in {'HOLDOUT_PASSED', 'HOLDOUT_FAILED', 'HOLDOUT_UNKNOWN', 'HOLDOUT_INCONCLUSIVE', 'QUERY_SEALED_BUNDLE_FINISHED'}
                    # Independent verification is attested by the sealed protocol,
                    # not by an arbitrary Handbook HOLDOUT_* event name.
                    result['payload']['independent'] = True if kind == 'QUERY_SEALED_BUNDLE_FINISHED' else 'UNKNOWN'
                # Legacy Handbook emits HOLDOUT_FAILED even for UNKNOWN.
                if kind == 'HOLDOUT_FAILED' and p.get('reason') == 'UNKNOWN':
                    result['payload']['verdict'] = 'HOLDOUT_INCONCLUSIVE'
                records.append(result)
    for (sid, key), bucket in sorted(requests.items()):
        events = bucket['events']
        start = events.get('MODEL_REQUEST_STARTED', {}).get('payload', {})
        end = events.get('MODEL_REQUEST_COMPLETED', {}).get('payload', {})
        bound = events.get('MODEL_REQUEST_POSTHOC_COST_BOUND', {}).get('payload', {})
        dims = _dimensions(start)
        payload = {**_safe(start), 'request_key': key}
        invocation = _record('AgentInvocation', sid + ':' + key, payload, [bucket['ref']],
                             [e['event_id'] for e in events.values()], dims)
        invocation['payload'].update(attempted=bool(start) if start else 'UNKNOWN',
            received=end.get('status') == 'RECEIVED' if end else 'UNKNOWN', success='UNKNOWN',
            schema_valid='UNKNOWN', abstained='UNKNOWN', failed='UNKNOWN', failure_origin='UNKNOWN',
            transport_status=end.get('status', 'UNKNOWN'), usage_status=end.get('usage_status', 'UNKNOWN'))
        records.append(invocation)
        if start or end or bound:
            cost = _record('CostEvent', sid + ':cost:' + key, {**start, **end, **bound},
                           [bucket['ref']], [e['event_id'] for e in events.values()], dims)
            usage = end.get('usage') if isinstance(end.get('usage'), dict) else {}
            details = usage.get('input_tokens_details') or {}
            cost['payload'].update(request_key=key, cost_nano_upper=bound.get('cost_nano_upper', end.get('cost_nano_upper')),
                input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                cached_tokens=details.get('cached_tokens'), usage_status=bound.get('usage_status', end.get('usage_status', 'UNKNOWN')),
                bounded=bool(bound), pending=not bool(end), provider_invoiced_charge=None, provider_invoiced_nano=None,
                transport_status=end.get('status', 'UNKNOWN'))
            records.append(cost)
    seen_artifacts = {}
    for artifact in artifacts:
        identity = artifact['source_id']
        if identity in seen_artifacts:
            if seen_artifacts[identity] != artifact['data']: raise ValueError('ANALYTICS_ARTIFACT_IDENTITY_CONFLICT')
            continue
        seen_artifacts[identity] = artifact['data']
        data, kind = artifact['data'], artifact['kind']
        refs = [{k: artifact[k] for k in ('source_id', 'sha256', 'path')}]
        dims = _dimensions(data)
        if kind == 'round':
            records.append(_record('Round', identity + ':round', data, refs, dimensions=dims))
            for slot, proposal in data.get('proposal_instances', {}).items():
                record = _record('Proposal', identity + ':proposal:' + slot,
                    {**proposal, 'slot': slot, 'round_id': data['round_id']}, refs, dimensions={**dims, 'agent_role': data.get('proposal_roles', {}).get(slot, slot)})
                # Presence in an archive proves an instance exists, not that a
                # model output met the complete context-dependent contract.
                record['payload'].update(schema_valid='UNKNOWN',
                    static_pass=slot in data['qualified'] if isinstance(data.get('qualified'), list) else 'UNKNOWN')
                patch = proposal.get('worker_patch')
                record['payload']['patch_sha256'] = digest(patch) if isinstance(patch, dict) and set(patch) == {'old', 'new'} else 'UNKNOWN'
                # A replacement snippet is not the full candidate AST. No code
                # files are followed from archived references by this reader.
                record['payload']['ast_sha256'] = 'UNKNOWN'
                for text_field, hash_field in (('hypothesis', 'hypothesis_id'), ('expected_impact', 'expected_impact_id')):
                    text = proposal.get(text_field)
                    record['payload'][hash_field] = sha256(text.encode('utf-8')).hexdigest() if isinstance(text, str) else 'UNKNOWN'
                records.append(record)
            for decision in data.get('candidate_decisions', []):
                cdims = {**dims, 'candidate_id': decision.get('candidate_id') or 'UNKNOWN',
                         'agent_role': data.get('proposal_roles', {}).get(decision.get('angle'), decision.get('angle', 'UNKNOWN'))}
                records.append(_record('Candidate', identity + ':candidate:' + decision['angle'], decision, refs, dimensions=cdims))
                if decision.get('per_question'):
                    # The row bodies are deliberately not projected.
                    replay = _record('Replay', identity + ':replay:' + decision['angle'], decision, refs, dimensions=cdims)
                    replay['payload'].update(round_id=data['round_id'], entered=decision['angle'] in data.get('attempted_replays', []),
                        completed='UNKNOWN', paired='UNKNOWN')
                    records.append(replay)
        elif kind == 'family':
            family = _record('FamilyDecision', identity + ':family',
                {**data.get('outcome', {}), 'family_id': data.get('family_id')}, refs, dimensions=dims)
            family['payload'].update(complete=data.get('all_qualified_terminal', 'UNKNOWN'),
                decisions=[_safe(d) for d in data.get('outcome', {}).get('candidate_decisions', [])])
            records.append(family)
        elif kind == 'replay':
            records.append(_record('Replay', identity + ':replay', data, refs, dimensions=dims))
        else:
            raise ValueError('ANALYTICS_ARTIFACT_NOT_ALLOWED')
    # Explicit round artifacts enrich existing records through protocol identities.
    # Conflicting archive identities are rejected, never silently version-merged.
    rounds = {}
    for artifact in artifacts:
        if artifact['kind'] == 'round':
            rid = artifact['data']['round_id']
            if rid in rounds and rounds[rid]['sha256'] != artifact['sha256']:
                raise ValueError('ANALYTICS_ROUND_IDENTITY_CONFLICT')
            rounds[rid] = artifact
    for record in records:
        rid = record['payload'].get('round_id')
        if record['projection_type'] == 'FamilyDecision':
            matches = [r for r, a in rounds.items() if a['data'].get('family_id') == record['payload'].get('family_id')]
            if len(matches) > 1: raise ValueError('ANALYTICS_FAMILY_ROUND_IDENTITY_CONFLICT')
            rid = matches[0] if matches else None
        if record['projection_type'] in {'AgentInvocation', 'CostEvent'}:
            key = record['payload']['request_key']
            role = record['dimensions']['agent_role']
            matches = [r for r in rounds if key in {f'{r}-{role}-0', f'{r}-{role}-1'}]
            rid = matches[0] if len(matches) == 1 else None
        if rid in rounds:
            artifact = rounds[rid]
            enrichment = _dimensions(artifact['data'])
            for name, value in enrichment.items():
                if value != 'UNKNOWN':
                    if record['dimensions'][name] not in {'UNKNOWN', value}:
                        raise ValueError('ANALYTICS_DIMENSION_CONFLICT')
                    record['dimensions'][name] = value
            ref = {k: artifact[k] for k in ('source_id', 'sha256', 'path')}
            if ref not in record['source_refs']: record['source_refs'].append(ref)
            record['payload']['round_id'] = rid
    return sorted(records, key=lambda r: (r['projection_type'], r['id']))
