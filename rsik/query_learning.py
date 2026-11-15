"""Trusted archive-to-handbook bridge. No model can assert qualification.

Counts describe verified associations, never causal contribution. Synthetic
controls use a distinct handbook namespace and never create real eligibility.
"""
from copy import deepcopy
from pathlib import Path

from .adjudicate import adjudicate_holdout
from .contracts import ContractError, unwrap_response, validate_readonly
from .events import EventLog
from .experiment import load, bytes_hash, read_decision
from .foundation import check_split
from .locking import atomic_write
from .query_aware import QueryRegistry, PROFILE
from .redact import make_visible, project_summary
from .state import canonical, digest


def record_query_round_learning(*, handbook, record, public, compatibility_fingerprint):
    """Record matched, referenced and actually replayed uses separately."""
    if not hasattr(handbook, 'log'):
        raise ValueError('TRUSTED_HANDBOOK_REQUIRED')
    mode = handbook.evidence_mode
    common = {'round_id': record['round_id'], 'evidence_version': 'query-learning/v1',
              'evidence_mode': mode, 'compatibility_fingerprint': compatibility_fingerprint}
    def append(kind, payload):
        payload = {**common, **payload}
        handbook.log.append('query:' + kind + ':' + digest(payload), kind, payload)
    for entry in public['handbook_entry_ids']:
        append('EXPERIENCE_MATCHED', {'entry_id': entry})
    decisions = {d['angle']: d for d in record.get('candidate_decisions', [])}
    for slot, proposal in record.get('proposal_instances', {}).items():
        decision = decisions.get(slot, {})
        cid = decision.get('candidate_id')
        if not cid:
            continue
        equivalence = digest(proposal['worker_patch'])
        # AST normalization is separately reported by the controller. The use
        # episode key is candidate code equivalence when a worker hash exists.
        equivalence = decision.get('worker_hash') or equivalence
        for entry in proposal['handbook_entry_ids']:
            if entry not in public['handbook_entry_ids']:
                raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
            payload = {'entry_id': entry, 'candidate_id': cid, 'proposal_id': proposal['proposal_id'],
                       'equivalence_hash': equivalence}
            append('PROPOSAL_REFERENCED', payload)
            if slot in record.get('attempted_replays', []):
                append('EXPERIENCE_USED', payload)
            kind = ('CANDIDATE_ADOPTED' if record.get('adopted_candidate_id') == cid else
                    'CANDIDATE_INCONCLUSIVE' if decision.get('verdict') == 'INCONCLUSIVE' else
                    'CANDIDATE_NOT_SELECTED')
            append(kind, {**payload, 'reason_codes': decision.get('reason_codes', [])})


class ExperienceSession:
    """Freeze initial memory and reject any writes outside a learning transaction.

    A crash during a transaction is intentionally not auto-approved. Archived
    evidence must be reconciled before a new request, rather than accepting the
    current event tail as though it belonged to the original experiment.
    """
    def __init__(self, directory, handbook):
        self.directory, self.handbook = Path(directory), handbook
        self.directory.mkdir(parents=True, exist_ok=True)
        self.initial = self.directory / 'initial-memory.json'
        self.frontier = self.directory / 'learning-frontier.json'
        if not self.initial.exists():
            value = self.snapshot()
            atomic_write(self.initial, canonical(value), exclusive=True)
            atomic_write(self.frontier, canonical(value), exclusive=True)
        self.verify()

    def snapshot(self):
        events = self.handbook.log.read()
        return {'schema': 'rsi-learning-frontier/v1', 'evidence_mode': self.handbook.evidence_mode,
                'sequence': len(events), 'event_hash': events[-1]['event_hash'] if events else '0' * 64,
                'entries': {k: bytes_hash((self.handbook.directory / 'entries' / k).read_bytes())
                            for k in self.handbook.view()['states']}}

    def verify(self):
        if (self.directory / 'learning-intent.json').exists():
            raise ValueError('LEARNING_TRANSACTION_RECONCILIATION_REQUIRED')
        initial, frontier, current = load(self.initial), load(self.frontier), self.snapshot()
        events = self.handbook.log.read()
        n = initial['sequence']
        if (current != frontier or n > len(events)
                or (events[n-1]['event_hash'] if n else '0' * 64) != initial['event_hash']
                or any(current['entries'].get(k) != v for k, v in initial['entries'].items())):
            raise ValueError('EXTERNAL_EXPERIENCE_CHANGE')
        return frontier

    def learn(self, evidence_id, operation):
        self.verify()
        intent = self.directory / 'learning-intent.json'
        atomic_write(intent, canonical({'evidence_id': evidence_id, 'before': load(self.frontier)}), exclusive=True)
        result = operation()
        after = self.snapshot()
        atomic_write(self.directory / ('learning-' + evidence_id + '.json'),
                     canonical({'evidence_id': evidence_id, 'after': after}))
        atomic_write(self.frontier, canonical(after))
        intent.unlink()  # one owned file, after durable reconciliation receipt
        return result


class VerifiedCandidateEvidence:
    """Recompute training family and sealed comparisons from pinned execution.

    Construction is an archive reader, never a deserializer for model fields.
    Caller supplies the trusted host execution policy; serialized mode is only
    compared with it. No sealed query, answer, clause, or source ID is exported.
    """
    @classmethod
    def from_archives(cls, *, registry, training, train_fixtures, verification, evidence):
        from .execution_evidence import resolve_policy
        from .query_verification import verify_training
        policy = resolve_policy(evidence)
        verification, training = Path(verification), Path(training)
        context = load(verification / 'verification-context.restricted.json')
        receipt_path = verification / 'receipt.restricted.json'
        receipt = load(receipt_path)
        expected = 'VERIFIED' if policy.real_eligible else 'CONTROL_VERIFIED'
        if (receipt.get('verdict') != expected or receipt.get('evidence_mode') != policy.mode
                or receipt.get('real_eligible') is not policy.real_eligible
                or context['execution_evidence_policy'] != policy.to_dict()
                or Path(context['training']).resolve() != training.resolve()
                or [Path(p).resolve() for p in context['train_fixtures']] != [Path(p).resolve() for p in train_fixtures]):
            raise ValueError('VERIFIED_EVIDENCE_SCOPE_MISMATCH')
        cid = receipt['candidate_id']
        candidate, parent = verify_training(registry, training, train_fixtures, cid, evidence=policy)
        qreg = QueryRegistry(registry)
        qreg.require_not_quarantined(cid)
        if (receipt['parent_id'] != parent['candidate_id'] or context['candidate_id'] != cid or context['trusted_id']!=receipt['trusted_id']
                or receipt['training_receipt_sha256'] != bytes_hash((training / 'round.json').read_bytes())):
            raise ValueError('VERIFIED_EVIDENCE_LINEAGE_MISMATCH')
        finish = [e for e in EventLog(Path(registry) / 'sealed.sqlite').read()
                  if e['event_type'] == 'QUERY_SEALED_BUNDLE_FINISHED'
                  and e['payload'].get('bundle_hash') == receipt['bundle_hash']]
        if len(finish) != 1 or finish[0]['payload']['receipt_sha256'] != bytes_hash(receipt_path.read_bytes()):
            raise ValueError('VERIFIED_EVIDENCE_NOT_ARCHIVED')
        fixtures = [Path(p) for p in context['sealed_fixtures']]
        if len(fixtures) != 3:
            raise ValueError('VERIFIED_EVIDENCE_SUITE_SHAPE')
        def check_lineage(run, anchor):
            manifest=load(run/'manifest.json')
            for arm, artifact in [('A',anchor),('B',candidate)]:
                spec=manifest['arms'][arm]
                if (spec['candidate_id']!=artifact['candidate_id'] or spec['worker_sha256']!=bytes_hash(artifact['worker'])
                        or spec['sha256']!=bytes_hash(artifact['config'])):raise ValueError('VERIFIED_REPLAY_LINEAGE_CHANGED')
        decisions = []
        for index, fixture in enumerate(fixtures, 1):
            freeze = check_split(fixture, expected_count=receipt['statistical_denominator'])
            if (load(fixture / 'freeze.json') != freeze
                    or bytes_hash((fixture / 'freeze.json').read_bytes()) != context['fixture_freeze_hashes'][index-1]):
                raise ValueError('VERIFIED_FIXTURE_CHANGED')
            run = verification / ('vs-parent-' + str(index))
            check_lineage(run,parent)
            d = read_decision(run, fixture, cid,
                {'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1'},
                bytes_hash(candidate['worker']), bytes_hash(candidate['config']), evidence=policy)
            if d != receipt['suite_decisions'][index-1]:
                raise ValueError('VERIFIED_REPLAY_CHANGED')
            decisions.append(d)
        trusted = decisions[0]
        if not receipt['shared_primary_comparison']:
            anchor = qreg.get(receipt['trusted_id'])
            qreg.require_not_quarantined(anchor['candidate_id'])
            check_lineage(verification/'vs-trusted',anchor)
            trusted = read_decision(verification / 'vs-trusted', fixtures[0], cid,
                {'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1'},
                bytes_hash(candidate['worker']), bytes_hash(candidate['config']), evidence=policy)
        recomputed = adjudicate_holdout(decisions[0], trusted)
        if (recomputed['verdict'] != 'VERIFIED' or recomputed['comparisons'] != receipt['comparisons']
                or not all(all(d['gates']['G'+str(i)] is True for i in range(1,5)) for d in decisions[1:])):
            raise ValueError('VERIFIED_JUDGMENT_CHANGED')
        self = cls()
        self.candidate, self.parent, self.receipt = candidate, parent, receipt
        self.training, self.receipt_path, self.mode = training, receipt_path, policy.mode
        self.family = load(training / 'family-decision.json')
        self.evidence_id = digest({'receipt': bytes_hash(receipt_path.read_bytes()),
                                   'family': self.family['family_id'], 'mode': self.mode, 'version': 'verified-evidence/v1'})
        return self

    def projection(self, scanner, fingerprint):
        from run_rsi_loop import selector
        code = selector(self.candidate['worker'].decode())[1]
        primary = self.receipt['suite_decisions'][0]
        rows = primary['per_question']
        counts = {'questions': len(rows), 'source_groups': primary['source_group_count'], 'verified': 1,
                  **primary['group_counts']}
        observation = project_summary('REPLAY_SUMMARY', counts, include_count_evidence=True)
        observation['observation_ref']='observation-'+self.evidence_id[:32]
        # One alias per verified source group, same safe aggregate quote only.
        refs = {}
        safe = observation['safe_evidence'][0]
        for index, group in enumerate(sorted({r['source_group'] for r in rows})):
            alias = 'evidence-' + digest({'id': self.evidence_id, 'index': index})[:32]
            observation['safe_evidence'].append({'evidence_ref': alias, 'text': safe['text']})
            refs[alias] = {'observation_id': observation['observation_ref'],
                'candidate_id': self.candidate['candidate_id'], 'parent_id': self.parent['candidate_id'],
                'run_id': self.evidence_id, 'result_hash': bytes_hash(self.receipt_path.read_bytes()),
                'result_path': str(self.receipt_path.resolve()), 'source_group': group,
                'features': {'same_source': True, 'format': 'OTHER'}}
        public = make_visible([observation], parent_code=code, scanner=scanner,round_id='round-'+self.evidence_id[:32])
        public['parent_alias']='parent-'+digest({'parent':self.parent['candidate_id'],'evidence':self.evidence_id})[:32]
        action = {'name': 'REUSE_VERIFIED_SELECTOR', 'target': 'SELECTED_MEMBERS',
                  'params': {'code_sha256': bytes_hash(code.encode())}}
        public['compatibility_fingerprint'] = fingerprint
        public['completed_candidate'] = {'candidate_alias': 'candidate-' + self.evidence_id[:32],
            'code_sha256': bytes_hash(code.encode()), 'quality_status': self.receipt['verdict'],
            'evidence_mode':self.mode,'real_eligible':self.mode=='REAL_REPLAY',
            'evidence_refs': list(refs), 'implemented_action': action,
            'chars_before': sum(r['chars_A'] for r in rows), 'chars_after': sum(r['chars_B'] for r in rows),
            'robustness_passed': True, 'scope': 'FROZEN_SOURCE_CLAUSE_PRESERVATION_ONLY' if self.mode=='REAL_REPLAY' else 'SYNTHETIC_CONTROL_SOURCE_CLAUSE_TEST_ONLY'}
        context = {'refs': refs, 'verified_candidates': {self.candidate['candidate_id']: bytes_hash(self.receipt_path.read_bytes())},
                   'allowed_actions': [action], 'allowed_formats': ['OTHER'],
                   'compatibility_fingerprint': fingerprint, 'evidence_mode': self.mode}
        return scanner.check(public), context


def curate_verified_candidate(*, registry, training, train_fixtures, verification, handbook,
                             roles, compatibility_fingerprint, evidence, session=None):
    if session:session.verify()
    bound = VerifiedCandidateEvidence.from_archives(registry=registry, training=training,
        train_fixtures=train_fixtures, verification=verification, evidence=evidence)
    if handbook.evidence_mode != bound.mode:
        raise ValueError('HANDBOOK_EVIDENCE_MODE_MISMATCH')
    if not compatibility_fingerprint:
        raise ValueError('HANDBOOK_COMPATIBILITY_REQUIRED')
    public, context = bound.projection(roles.scanner, compatibility_fingerprint)
    book_binding={'directory':str(handbook.directory.resolve()),'evidence_mode':handbook.evidence_mode,
                  'compatibility_fingerprint':compatibility_fingerprint}
    output = Path(verification) / 'learning'
    output.mkdir(exist_ok=True)
    # Deterministic aliases and receipt binding make restart idempotent. Never
    # call the model again once a response has been archived, including ABSTAIN.
    response_path = output / 'curator.json'
    if response_path.exists():
        response = load(response_path)
    else:
        response = roles.call('curator', public)
        atomic_write(response_path, canonical(response), exclusive=True)
    delta = unwrap_response(response)
    if delta is not None:
        validate_readonly(delta, public, 'curator')
    receipt = output / 'learning-receipt.json'
    if receipt.exists():
        previous = load(receipt)
        if previous['evidence_id'] != bound.evidence_id or previous['response_hash'] != digest(response):
            raise ValueError('LEARNING_RECEIPT_CHANGED')
        if previous.get('handbook_binding')!=book_binding:raise ValueError('LEARNING_HANDBOOK_BINDING_CHANGED')
        events=handbook.log.read()
        if not any(e['idempotency_key']=='query-verified:'+bound.evidence_id and e['payload'].get('candidate_id')==bound.candidate['candidate_id'] for e in events):
            raise ValueError('LEARNING_COMMITTED_EFFECTS_MISSING')
        if delta is not None:
            commits=[e for e in events if e['idempotency_key']=='delta:'+bound.evidence_id]
            if (len(commits)!=1 or commits[0]['payload'].get('delta_hash')!=digest(delta)
                    or previous['entries']!=[commits[0]['payload']['entry_id']]):raise ValueError('LEARNING_COMMITTED_EFFECTS_MISSING')
            for entry_id in previous['entries']:
                handbook.get(entry_id)
                links=[e for e in events if e['event_type']=='ENTRY_EVIDENCE_BOUND' and e['payload']['entry_id']==entry_id]
                valid=False
                for link in links:
                    key=link['payload']['evidence_bundle_sha256'];raw=(handbook.directory/'evidence'/key).read_bytes()
                    if bytes_hash(raw)!=key:raise ValueError('LEARNING_EVIDENCE_CHANGED')
                    bundle=load(handbook.directory/'evidence'/key)
                    valid=valid or bundle.get('verified_candidates')==context['verified_candidates'] and bundle.get('compatibility_fingerprint')==compatibility_fingerprint
                if not valid:raise ValueError('LEARNING_COMMITTED_EFFECTS_MISSING')
        elif previous['entries']:
            raise ValueError('LEARNING_COMMITTED_EFFECTS_MISSING')
        return previous
    def commit():
        cid = bound.candidate['candidate_id']
        handbook.log.append('query-verified:' + bound.evidence_id, 'HOLDOUT_PASSED',
            {'candidate_id': cid, 'evidence_version': bound.evidence_id, 'evidence_mode': bound.mode,
             'receipt_hash': bytes_hash(bound.receipt_path.read_bytes())})
        entries = []
        if delta is not None:
            entries.append(handbook.commit_delta(delta, context, idempotency_key=bound.evidence_id))
            EventLog(Path(registry)/'learning-links.sqlite').append('book:'+bound.evidence_id+':'+digest(str(handbook.directory.resolve())),
                'CANDIDATE_HANDBOOK_BOUND',{'candidate_id':cid,'evidence_id':bound.evidence_id,'handbook':str(handbook.directory.resolve()),'evidence_mode':bound.mode})
        return {'schema': 'query-learning/v1', 'evidence_id': bound.evidence_id,
                'response_hash': digest(response), 'entries': entries, 'evidence_mode': bound.mode,'handbook_binding':book_binding,
                'real_eligible': bound.mode == 'REAL_REPLAY', 'association_only': True}
    result = session.learn(bound.evidence_id, commit) if session else commit()
    atomic_write(receipt, canonical(result), exclusive=True)
    return result


def quarantine_candidate_learning(handbook, candidate_id, *, evidence_hash, reason):
    """Invalidate associated rules without manufacturing harmful credit."""
    events = handbook.log.read()
    supported = set()
    for event in events:
        if event['event_type'] == 'ENTRY_EVIDENCE_BOUND':
            bundle = load(handbook.directory / 'evidence' / event['payload']['evidence_bundle_sha256'])
            if any(r['candidate_id'] == candidate_id for r in bundle['refs'].values()):
                supported.add(event['payload']['entry_id'])
    for entry in supported:
        payload = {'entry_id': entry, 'candidate_id': candidate_id, 'reason': reason,
                   'evidence_mode': handbook.evidence_mode, 'evidence_version': evidence_hash}
        key = 'invalidate:' + digest(payload)
        handbook.log.append(key, 'ENTRY_QUARANTINED', payload)
        handbook.log.append(key + ':outcome', 'OUTCOME_INVALIDATED', payload)
    return sorted(supported)
