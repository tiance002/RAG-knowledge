"""Version-separated metadata projections; raw model/evidence text never leaves here."""
from .reader import digest
from .schemas import GROUP_DIMENSIONS
from pathlib import Path
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


def _project_legacy_sources(sources, artifacts=()):
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


OBSERVATION_SCHEMA = 'rsi-observation/v1'
# An allowlist of metadata, never prompt, response, patch, or evidence bodies.
OBSERVATION_SAFE = SAFE | {
    'schema_version', 'metric_contract_version', 'observation_source', 'invocation_id', 'attempt_id', 'attempt',
    'cost_request_key', 'contract_result', 'outcome', 'failure_origin',
    'proposal_instance_id', 'patch_sha256', 'patch_hash', 'ast_sha256', 'normalized_ast_sha256', 'worker_sha256', 'schema_valid', 'static_pass',
    'hypothesis_hash', 'expected_impact_hash',
    'retrieval_id', 'selection_id', 'rank', 'eligible', 'admitted', 'selected',
    'entry_fingerprint', 'current_fingerprint', 'conflicting', 'equivalence_hash',
    'stage_attempt_id', 'stage', 'recovery_attempt_id', 'actual_execution_id',
    'action_id', 'execution_id', 'evidence_contract', 'evidence_complete', 'cost_source',
    'stage_id', 'run_id', 'recovery_operation_id', 'original_operation_id',
    'input_hash', 'output_hash', 'checkpoint_id', 'failure_category',
    'expected_count', 'known_count', 'missing_count', 'terminal_required',
    'success', 'failed', 'raw_retrieval', 'condition_conflict', 'stale',
    'policy_version', 'observation_id', 'assessment_id', 'entity_id',
    'population_count', 'returned_count', 'filtered_count', 'pair_entry_ids',
    'token_budget_profile', 'token_budget_sha256', 'requested_output_tokens', 'output_classification',
    'provider_response_status', 'provider_finish_reason', 'provider_incomplete_reason',
    'reasoning_tokens', 'visible_output_tokens',
    'visible_output_tokens_upper', 'visible_message_received', 'structured_json_complete', 'repair_of'}


def _observation_payload(payload):
    return {k: v for k, v in payload.items() if k in OBSERVATION_SAFE and
            (v is None or isinstance(v, (str, bool, int, float)) or
             k in {'reason_codes', 'pair_entry_ids'} and isinstance(v, list) and all(isinstance(x, str) for x in v))}


def _observation_dimensions(payload):
    dims = _dimensions(payload)
    dims['role_profile_sha256'] = payload.get('role_profile_hash', payload.get('role_profile_sha256', dims['role_profile_sha256']))
    return dims


def _project_observations(items):
    buckets, singles, seen = {}, [], {}
    for sid, ref, event in items:
        p, kind = event['payload'], event['event_type']
        event_key = (sid, event['event_id'])
        if event_key in seen:
            if seen[event_key] != event: raise ValueError('ANALYTICS_EVENT_IDENTITY_CONFLICT')
            continue
        seen[event_key] = event
        if kind.startswith('INVOCATION_') or kind in {'RESPONSE_RECEIVED', 'CONTRACT_VALIDATED'}:
            typ, identity = 'AgentInvocation', (p.get('invocation_id'), p.get('attempt_id', p.get('attempt', 0)))
        elif kind.startswith('PROPOSAL_') and kind != 'PROPOSAL_REFERENCED':
            typ, identity = 'Proposal', p.get('proposal_instance_id')
        elif kind.startswith('STAGE_'):
            typ, identity = 'Stage', p.get('stage_attempt_id')
        elif kind.startswith('RECOVERY_'):
            typ, identity = 'Recovery', p.get('recovery_attempt_id')
        elif kind in {'EXECUTION_STARTED', 'EXECUTION_FINISHED', 'EXECUTION_COMPLETED'}:
            typ, identity = 'Execution', p.get('actual_execution_id', p.get('execution_id'))
        elif kind in {'PARENT_SELECTION_STARTED', 'PARENT_SELECTION_COMPLETED'} and p.get('metric_contract_version') == 'rsi-metrics/m1a1':
            typ, identity = 'Selection', p.get('selection_id')
        elif kind == 'PARENT_SELECTED':
            typ, identity = 'Parent', (p.get('selection_id'), p.get('parent_id'))
        else:
            singles.append((sid, ref, event)); continue
        if not identity or isinstance(identity, tuple) and not identity[0]:
            raise ValueError('ANALYTICS_OBSERVATION_IDENTITY_MISSING')
        # An identity is local to a frozen event stream and compatibility cohort.
        cohort = {k: _observation_dimensions(p).get(k, 'UNKNOWN') for k in GROUP_DIMENSIONS}
        if p.get('metric_contract_version'): cohort['metric_contract_version'] = p['metric_contract_version']
        key = (sid, digest(cohort), typ, str(identity))
        buckets.setdefault(key, []).append((ref, event))
    records = []
    for (sid, cohort, typ, identity), events in sorted(buckets.items()):
        events.sort(key=lambda pair: pair[1].get('sequence', 0))
        merged = {}
        for ref, event in events:
            merged.update(_observation_payload(event['payload']))
        merged['observation_source'] = OBSERVATION_SCHEMA
        record = _record(typ, sid + ':observation:' + typ + ':' + cohort + ':' + identity, {},
                         [events[0][0]], [e['event_id'] for _, e in events], _observation_dimensions(events[0][1]['payload']))
        kinds = {e['event_type'] for _, e in events}
        if typ == 'AgentInvocation':
            terminal = [e['payload'] for _, e in events if e['event_type'] == 'INVOCATION_FINISHED']
            outcomes = {(p.get('outcome'), p.get('contract_result'), p.get('failure_origin')) for p in terminal}
            end = terminal[-1] if len(outcomes) == 1 else {}
            outcome, contract = end.get('outcome'), end.get('contract_result')
            received = bool(kinds & {'INVOCATION_RESPONSE_RECEIVED', 'RESPONSE_RECEIVED'})
            merged.update(attempted=True if 'INVOCATION_STARTED' in kinds else 'UNKNOWN',
                received=received if terminal or received else 'UNKNOWN',
                success=(outcome == 'SUCCESS' and contract == 'VALID') if end else 'UNKNOWN',
                schema_valid=contract == 'VALID' if contract in {'VALID', 'INVALID'} else 'UNKNOWN',
                abstained=outcome == 'ABSTAINED' if end else 'UNKNOWN',
                failed=outcome not in {'SUCCESS', 'ABSTAINED'} or contract == 'INVALID' if end else 'UNKNOWN',
                failure_origin=end.get('failure_origin', 'UNKNOWN'))
        elif typ == 'Proposal':
            for source_field, target in [('patch_hash', 'patch_sha256'), ('hypothesis_hash', 'hypothesis_id'), ('expected_impact_hash', 'expected_impact_id')]:
                if merged.get(source_field): merged[target] = merged[source_field]
            if merged.get('contract_result') in {'VALID', 'INVALID'}:
                merged['schema_valid'] = merged['contract_result'] == 'VALID'
            if 'PROPOSAL_STATIC_PASSED' in kinds: merged['static_pass'] = True
            if 'PROPOSAL_STATIC_REJECTED' in kinds: merged['static_pass'] = False
        elif typ in {'Stage', 'Recovery'}:
            terminal = [e['payload'] for _, e in events if e['event_type'] in {typ.upper() + '_FINISHED', typ.upper() + '_COMPLETED', typ.upper() + '_FAILED', typ.upper() + '_BLOCKED', typ.upper() + '_CANCELLED'}]
            end = terminal[-1] if terminal else {}
            field = 'failed' if typ == 'Stage' else 'success'
            terminal_states = {e['event_type'] for _, e in events if e['event_type'] in {
                'STAGE_COMPLETED', 'STAGE_FAILED', 'STAGE_CANCELLED',
                'RECOVERY_COMPLETED', 'RECOVERY_FAILED', 'RECOVERY_BLOCKED'}}
            if len(terminal_states) > 1: merged[field] = 'UNKNOWN'
            elif field in end: merged[field] = end[field]
            elif typ == 'Stage' and 'STAGE_FAILED' in kinds: merged[field] = True
            elif typ == 'Stage' and 'STAGE_COMPLETED' in kinds: merged[field] = False
            elif typ == 'Recovery' and 'RECOVERY_COMPLETED' in kinds: merged[field] = True
            elif typ == 'Recovery' and kinds & {'RECOVERY_FAILED', 'RECOVERY_BLOCKED'}: merged[field] = False
            elif typ == 'Stage' and 'STAGE_CANCELLED' in kinds: merged[field] = False
            else: merged[field] = 'UNKNOWN'
        elif typ == 'Execution':
            merged['action_id'] = merged.get('action_id', merged.get('operation_id'))
        elif typ == 'Selection':
            merged.update(started='PARENT_SELECTION_STARTED' in kinds, completed='UNKNOWN',
                          valid='UNKNOWN', selected_parent_id=None)
        elif typ == 'Parent':
            merged['selected'] = True
        if typ in {'AgentInvocation', 'Stage', 'Recovery'}:
            terminals = {'AgentInvocation': {'INVOCATION_FINISHED'},
                         'Stage': {'STAGE_COMPLETED', 'STAGE_FAILED', 'STAGE_CANCELLED', 'STAGE_FINISHED'},
                         'Recovery': {'RECOVERY_COMPLETED', 'RECOVERY_FAILED', 'RECOVERY_BLOCKED', 'RECOVERY_FINISHED'}}[typ]
            start_kind = {'AgentInvocation': 'INVOCATION_STARTED', 'Stage': 'STAGE_STARTED', 'Recovery': 'RECOVERY_STARTED'}[typ]
            known = int(start_kind in kinds) + int(bool(kinds & terminals))
            merged.update(evidence_contract='rsi-observation-completeness/v1:' + typ,
                          evidence_complete=known == 2, expected_count=2, known_count=known, missing_count=2-known)
        for _, event in events:
            for name, value in _observation_dimensions(event['payload']).items():
                if value != 'UNKNOWN': record['dimensions'][name] = value
        record['payload'] = merged
        records.append(record)
    for sid, ref, event in singles:
        p, kind = event['payload'], event['event_type']
        # Ranking is a candidate observation, not an actual retrieval. A completed
        # retrieval names its returned entries explicitly.
        entries = p.get('entry_ids') if kind in {'MEMORY_RETRIEVAL_COMPLETED', 'MEMORY_RETRIEVED', 'MEMORY_INJECTED', 'MEMORY_USED', 'PROPOSAL_REFERENCED'} else None
        bodies = [{**p, 'entry_id': eid} for eid in entries] if isinstance(entries, list) else [p]
        if not bodies and kind == 'MEMORY_RETRIEVAL_COMPLETED': bodies = [p]
        for index, body in enumerate(bodies):
            payload = _observation_payload(body)
            mapped = {'MEMORY_RETRIEVAL_COMPLETED': 'MEMORY_RETRIEVED', 'MEMORY_USED': 'EXPERIENCE_USED',
                      'MEMORY_CO_RETRIEVED': 'MEMORY_CO_RETRIEVED_PAIR'}.get(kind, kind)
            payload.update(event_type=mapped, observation_source=OBSERVATION_SCHEMA)
            if mapped == 'MEMORY_RETRIEVED':
                payload['raw_retrieval'] = body.get('entry_id') is not None
                if not payload['raw_retrieval']: payload['event_type'] = 'MEMORY_RETRIEVAL_COMPLETED'
            rec = _record(_event_kind(kind), sid + ':' + event['event_id'] + ':' + str(index), {}, [ref], [event['event_id']], _observation_dimensions(body))
            rec['payload'] = payload
            if kind.startswith('EVIDENCE_'): rec['projection_type'] = 'EvidenceAssessment'
            records.append(rec)
    # A selected event is evidence of a returned parent only after its own
    # operation completes consistently. Ranking/rejection never supply a parent.
    for selection in [r for r in records if r['projection_type'] == 'Selection']:
        sid = selection['source_refs'][0]['source_id']
        selection_id = selection['payload']['selection_id']
        lifecycle = [e for source, _, e in items if source == sid and
                     e['payload'].get('selection_id') == selection_id and
                     e['payload'].get('metric_contract_version') == selection['payload'].get('metric_contract_version') and
                     e['event_type'] in {'PARENT_SELECTION_STARTED', 'PARENT_SELECTION_COMPLETED', 'PARENT_SELECTED'} and
                     all(_observation_dimensions(e['payload']).get(k) == selection['dimensions'].get(k)
                         for k in GROUP_DIMENSIONS)]
        starts = {digest(e['payload']) for e in lifecycle if e['event_type'] == 'PARENT_SELECTION_STARTED'}
        ends = {digest(e['payload']): e['payload'] for e in lifecycle if e['event_type'] == 'PARENT_SELECTION_COMPLETED'}
        selected = {digest(e['payload']): e['payload'] for e in lifecycle if e['event_type'] == 'PARENT_SELECTED'}
        end = next(iter(ends.values())) if len(ends) == 1 else {}
        chosen = next(iter(selected.values())) if len(selected) == 1 else {}
        returned = end.get('returned_count')
        consistent_context = all(len({digest(e['payload'][field]) for e in lifecycle if field in e['payload']}) <= 1
            for field in ('round_id', 'attempt', 'policy_version', 'seed', 'exploration', 'allow_bootstrap'))
        order = {kind: min(e.get('sequence', 0) for e in lifecycle if e['event_type'] == kind)
                 for kind in {e['event_type'] for e in lifecycle}}
        ordered = (order.get('PARENT_SELECTION_STARTED', float('inf')) <
                   order.get('PARENT_SELECTION_COMPLETED', float('-inf')) and
                   (not selected or order.get('PARENT_SELECTION_STARTED', float('inf')) <
                    order.get('PARENT_SELECTED', float('-inf')) < order.get('PARENT_SELECTION_COMPLETED', float('-inf'))))
        valid = (ordered and len(starts) == 1 and len(ends) == 1 and consistent_context and
                 type(returned) is int and returned in (0, 1) and len(selected) == returned and
                 (returned == 0 or chosen.get('parent_id') not in (None, '', 'UNKNOWN') and
                  chosen.get('candidate_id', chosen.get('parent_id')) == chosen.get('parent_id')))
        expected_count = 2 + int(type(returned) is int and returned == 1)
        known_count = int(bool(starts)) + int(bool(ends)) + int(bool(selected) and expected_count == 3)
        selection['payload'].update(completed=True if valid else 'UNKNOWN', valid=True if valid else 'UNKNOWN',
            selected_parent_id=chosen.get('parent_id') if valid and returned else None,
            evidence_contract='rsi-observation-completeness/v1:Selection', evidence_complete=valid,
            expected_count=expected_count, known_count=known_count,
            missing_count=expected_count-known_count)
        selection['event_ids'] = sorted({e['event_id'] for e in lifecycle})
    proposals = {(r['source_refs'][0]['source_id'], r['payload'].get('proposal_instance_id')): r
                 for r in records if r['projection_type'] == 'Proposal'}
    for record in records:
        if record['projection_type'] != 'MemoryEvent': continue
        proposal = proposals.get((record['source_refs'][0]['source_id'], record['payload'].get('proposal_instance_id')))
        if proposal:
            for field in ('candidate_id', 'family_id', 'round_id'):
                if field == 'round_id' and record['payload'].get('metric_contract_version') == 'rsi-metrics/m1a1':
                    continue
                if proposal['payload'].get(field): record['payload'][field] = proposal['payload'][field]
    return records


def project_sources(sources, artifacts=()):
    """Keep M0 byte-for-byte semantics; opt into semantic joins only for v1 payloads."""
    sources, artifacts = tuple(sources), tuple(artifacts)
    legacy, observations = [], []
    for source in sources:
        old = []
        ref = {k: source[k] for k in ('source_id', 'sha256', 'path')}
        for event in source['events']:
            if event['payload'].get('schema_version') == OBSERVATION_SCHEMA:
                observations.append((source['source_id'], ref, event))
            else: old.append(event)
        legacy.append({**source, 'events': old})
    records = _project_legacy_sources(legacy, artifacts)
    if not observations: return records
    new = _project_observations(observations)
    # An archived round slot and an explicit runtime proposal instance describe
    # one attempt only when their round/slot/proposal identities agree uniquely.
    for proposal in [r for r in new if r['projection_type'] == 'Proposal']:
        p = proposal['payload']
        matches = [r for r in records if r['projection_type'] == 'Proposal' and
                   all(p.get(k) not in (None, '', 'UNKNOWN') and p.get(k) == r['payload'].get(k)
                       for k in ('round_id', 'slot', 'proposal_id'))]
        if len(matches) != 1: continue
        archive = matches[0]
        if any(archive['dimensions'].get(k, 'UNKNOWN') not in ('UNKNOWN', v) and v != 'UNKNOWN'
               for k, v in proposal['dimensions'].items()): continue
        for name in ('patch_sha256', 'ast_sha256'):
            left, right = p.get(name, 'UNKNOWN'), archive['payload'].get(name, 'UNKNOWN')
            if left != 'UNKNOWN' and right != 'UNKNOWN' and left != right:
                raise ValueError('ANALYTICS_PROPOSAL_BINDING_CONFLICT')
            if left == 'UNKNOWN': p[name] = right
        proposal['source_refs'] += [ref for ref in archive['source_refs'] if ref not in proposal['source_refs']]
        records.remove(archive)
    # Match exact budget keys, never infer an invocation from a transport event.
    for invocation in [r for r in new if r['projection_type'] == 'AgentInvocation']:
        key = invocation['payload'].get('cost_request_key')
        if not key: continue
        candidates = [r for r in records if r['projection_type'] == 'AgentInvocation' and r['payload'].get('request_key') == key]
        cost_source = invocation['payload'].get('cost_source')
        if cost_source:
            candidates = [r for r in candidates if any(
                digest(str(Path(ref['path']).resolve())) == cost_source
                for ref in r['source_refs'])]
        if len(candidates) != 1: continue
        old = candidates[0]
        # Explicit known compatibility disagreement forbids a cross-source join.
        if any(old['dimensions'].get(k, 'UNKNOWN') not in ('UNKNOWN', v) and v != 'UNKNOWN'
               for k, v in invocation['dimensions'].items()): continue
        invocation['source_refs'] += [ref for ref in old['source_refs'] if ref not in invocation['source_refs']]
        invocation['event_ids'] = sorted(set(invocation['event_ids'] + old['event_ids']))
        records.remove(old)
        for cost in records:
            if cost['projection_type'] == 'CostEvent' and cost['payload'].get('request_key') == key and cost['source_refs'] == old['source_refs']:
                cost['dimensions'] = dict(invocation['dimensions'])
                cost['source_refs'] += [ref for ref in invocation['source_refs'] if ref not in cost['source_refs']]
    return sorted(records + new, key=lambda r: (r['projection_type'], r['id']))
