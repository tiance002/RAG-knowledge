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
    """Freeze memory and recover only declared, input-bound learning operations.

    An arbitrary callback remains usable for legacy controls, but is never
    deserialized or retried after interruption. No recovery path sends a model
    request or accepts an unclassified event tail.
    """
    def __init__(self, directory, handbook, *, recovery=False):
        self.directory, self.handbook = Path(directory), handbook
        self.directory.mkdir(parents=True, exist_ok=True)
        self.initial = self.directory / 'initial-memory.json'
        self.frontier = self.directory / 'learning-frontier.json'
        if not self.initial.exists():
            value = self.snapshot()
            atomic_write(self.initial, canonical(value), exclusive=True)
            atomic_write(self.frontier, canonical(value), exclusive=True)
        if not recovery:self.verify()

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

    def checkpoint(self, point):
        """No-op host crash-injection seam; never part of model inputs."""

    def learn(self, evidence_id, operation):
        # Legacy arbitrary callbacks are deliberately nonrecoverable.
        self.verify()
        intent = self.directory / 'learning-intent.json'
        atomic_write(intent, canonical({'evidence_id': evidence_id, 'before': load(self.frontier)}), exclusive=True)
        result = operation()
        after = self.snapshot()
        atomic_write(self.directory / ('learning-' + evidence_id + '.json'),
                     canonical({'evidence_id': evidence_id, 'after': after}))
        atomic_write(self.frontier, canonical(after))
        intent.unlink()
        return result

    def learn_round(self, *, record_path, public_path, compatibility_fingerprint):
        record_path,public_path=Path(record_path),Path(public_path)
        record,public=load(record_path),load(public_path)
        class Recorder:
            evidence_mode=self.handbook.evidence_mode
            def append(_self,key,kind,payload,**kwargs):commands.append({'key':key,'kind':kind,'payload':payload})
        commands=[];book=Recorder();book.log=book
        record_query_round_learning(handbook=book,record=record,public=public,compatibility_fingerprint=compatibility_fingerprint)
        descriptor={'kind':'ROUND_EVENTS','commands':commands,'result':None,
            'bindings':file_bindings([record_path,public_path]),
            'record_path':str(record_path.resolve()),'public_path':str(public_path.resolve()),
            'compatibility_fingerprint':compatibility_fingerprint}
        return self._start(digest(record),descriptor)

    def learn_delta(self, evidence_id, *, response_path, public_path, context_path,
                    prefix_events=(), result=None, result_path=None, verification=None, request_proof=None):
        from datetime import datetime,timezone
        response_path,public_path,context_path=map(Path,(response_path,public_path,context_path))
        response,public,context=map(load,(response_path,public_path,context_path))
        delta=unwrap_response(response)
        if delta is not None:validate_readonly(delta,public,'curator')
        if context.get('evidence_mode',self.handbook.evidence_mode)!=self.handbook.evidence_mode:
            raise ValueError('HANDBOOK_EVIDENCE_MODE_MISMATCH')
        created_at=datetime.now(timezone.utc).isoformat()
        commands=list(deepcopy(prefix_events));commands2,entry_id=_plan_delta(self.handbook,delta,context,evidence_id,created_at)
        commands.extend(commands2)
        value=deepcopy(result or {'evidence_id':evidence_id,'entries':[] if entry_id is None else [entry_id]})
        if 'entries' in value:value['entries']=[] if entry_id is None else [entry_id]
        descriptor={'kind':'CURATOR_DELTA','commands':commands,'result':value,
            'bindings':file_bindings([response_path,public_path,context_path]),
            'response_path':str(response_path.resolve()),'public_path':str(public_path.resolve()),
            'context_path':str(context_path.resolve()),'created_at':created_at,
            'result_path':str(Path(result_path).resolve()) if result_path else None,
            'verification':verification,'request_proof':request_proof,'prefix_events':list(deepcopy(prefix_events))}
        return self._start(evidence_id,descriptor)

    def _start(self,evidence_id,descriptor):
        self.verify()
        if not isinstance(evidence_id,str) or not evidence_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.' for c in evidence_id):
            raise ValueError('INVALID_LEARNING_ID')
        if descriptor['kind']=='CURATOR_DELTA' and self.handbook.evidence_mode=='REAL_REPLAY':
            _validate_request_proof(descriptor.get('request_proof'),load(descriptor['response_path']))
        descriptor={**descriptor,'schema':'rsi-learning-operation/v1','evidence_id':evidence_id,
            'handbook':str(self.handbook.directory.resolve()),'evidence_mode':self.handbook.evidence_mode,
            'implementation':recovery_implementation()}
        path=self.directory/('operation-'+evidence_id+'.json')
        data=canonical(descriptor)
        if path.exists():
            old=load(path)
            # A completed idempotent call has its original plan/timestamp.
            if (old['bindings']!=descriptor['bindings'] or old['kind']!=descriptor['kind']
                    or old['implementation']!=descriptor['implementation']):raise ValueError('LEARNING_OPERATION_CHANGED')
            receipt=self.directory/('learning-'+evidence_id+'.json')
            if not receipt.exists():raise ValueError('RECOVERY_BLOCKED_OPERATION_WITHOUT_RECEIPT')
            prior=load(receipt)
            if (prior.get('operation_hash')!=bytes_hash(path.read_bytes()) or prior.get('result')!=old['result']):
                raise ValueError('LEARNING_RECEIPT_CHANGED')
            events=self.handbook.log.read();n=prior['after']['sequence']
            if n>len(events) or (events[n-1]['event_hash'] if n else '0'*64)!=prior['after']['event_hash']:
                raise ValueError('LEARNING_COMMITTED_EFFECTS_MISSING')
            return prior['result']
        atomic_write(path,data,exclusive=True)
        intent={'schema':'rsi-learning-intent/v2','evidence_id':evidence_id,'before':load(self.frontier),
                'operation_path':path.name,'operation_hash':bytes_hash(data)}
        atomic_write(self.directory/'learning-intent.json',canonical(intent),exclusive=True)
        self.checkpoint('intent')
        return self.recover()

    def _blocked(self,reason):
        atomic_write(self.directory/'recovery-status.json',canonical({'status':'RECOVERY_BLOCKED','reason':reason}))
        raise ValueError('RECOVERY_BLOCKED_'+reason)

    def recover(self):
        try:return self._recover()
        except (ValueError,RuntimeError,OSError,KeyError,TypeError) as error:
            reason=str(error)
            if reason.startswith('RECOVERY_BLOCKED_'):reason=reason[len('RECOVERY_BLOCKED_'):]
            elif reason not in {'LEARNING_TRANSACTION_RECONCILIATION_REQUIRED'}:reason='EVIDENCE_OR_EVENT_VALIDATION_FAILED'
            return self._blocked(reason)

    def _recover(self):
        """Reconcile a trusted event prefix without external calls or blind redo."""
        intent_path=self.directory/'learning-intent.json'
        if not intent_path.exists():
            self.verify();return {'status':'NO_PENDING_TRANSACTION'}
        intent=load(intent_path)
        if intent.get('schema')!='rsi-learning-intent/v2':return self._blocked('UNDECLARED_OPERATION')
        op_path=(self.directory/intent['operation_path']).resolve()
        if not op_path.is_relative_to(self.directory.resolve()):return self._blocked('OPERATION_PATH')
        data=op_path.read_bytes()
        if bytes_hash(data)!=intent['operation_hash']:return self._blocked('OPERATION_CHANGED')
        op=load(op_path)
        if (op.get('schema')!='rsi-learning-operation/v1' or op.get('kind') not in {'CURATOR_DELTA','ROUND_EVENTS'}
                or op['evidence_id']!=intent['evidence_id'] or op['implementation']!=recovery_implementation()
                or op['handbook']!=str(self.handbook.directory.resolve()) or op['evidence_mode']!=self.handbook.evidence_mode):
            return self._blocked('IDENTITY_CHANGED')
        for path,expected in op['bindings'].items():
            if not Path(path).is_file() or bytes_hash(Path(path).read_bytes())!=expected:return self._blocked('INPUT_CHANGED')
        # Revalidate host evidence, not only the JSON hashes.
        if op['kind']=='CURATOR_DELTA':
            response,public,context=map(load,(op['response_path'],op['public_path'],op['context_path']))
            delta=unwrap_response(response)
            if delta is not None:validate_readonly(delta,public,'curator')
            if self.handbook.evidence_mode=='REAL_REPLAY':
                if not op.get('verification'):return self._blocked('REAL_EVIDENCE_REQUIRED')
                _validate_request_proof(op.get('request_proof'),response)
                args=op['verification'];from .execution_evidence import ExecutionEvidence
                bound=VerifiedCandidateEvidence.from_archives(**args,evidence=ExecutionEvidence.real())
                expected_public,expected_context=bound.projection(self.handbook.scanner,context['compatibility_fingerprint'])
                if expected_public!=public or expected_context!=context:return self._blocked('VERIFIED_CONTEXT_CHANGED')
        else:
            record,public=load(op['record_path']),load(op['public_path'])
            if self.handbook.evidence_mode=='REAL_REPLAY':
                from .family_decision import validate_family
                family_path=Path(op['record_path']).parent/'family-decision.json'
                if record.get('candidate_decisions'):
                    outcome=validate_family(load(family_path))
                    if any(record.get(k)!=outcome.get(k) for k in ('verdict','adopted_candidate_id','candidate_decisions')):
                        return self._blocked('ROUND_EVIDENCE_CHANGED')
        events=self.handbook.log.read();before=intent['before'];n=before['sequence']
        if n>len(events) or (events[n-1]['event_hash'] if n else '0'*64)!=before['event_hash']:
            return self._blocked('FRONTIER_CHANGED')
        initial=load(self.initial);m=initial['sequence']
        if m>n or (events[m-1]['event_hash'] if m else '0'*64)!=initial['event_hash']:
            return self._blocked('INITIAL_MEMORY_CHANGED')
        if any(bytes_hash((self.handbook.directory/'entries'/k).read_bytes())!=v for k,v in before['entries'].items()):
            return self._blocked('BASE_ENTRY_CHANGED')
        tail=events[n:];commands=op['commands']
        frontier=load(self.frontier)
        if frontier!=before and (len(tail)!=len(commands) or frontier!=self.snapshot()):
            return self._blocked('FRONTIER_CHANGED')
        if op['kind']=='CURATOR_DELTA':
            from .handbook.projection import project
            class BaselineBook:
                evidence_mode=self.handbook.evidence_mode
                directory=self.handbook.directory
                def get(_self,key):return self.handbook.get(key)
                def view(_self):return project(events[:n],evidence_mode=self.handbook.evidence_mode)
            planned,_=_plan_delta(BaselineBook(),delta,context,op['evidence_id'],op['created_at'])
            prefix=op['prefix_events']
            if op.get('verification'):
                expected_prefix=[{'key':'query-verified:'+op['evidence_id'],'kind':'HOLDOUT_PASSED',
                    'payload':{'candidate_id':context['refs'][next(iter(context['refs']))]['candidate_id'],
                        'evidence_version':op['evidence_id'],'evidence_mode':self.handbook.evidence_mode,
                        'receipt_hash':bytes_hash((Path(op['verification']['verification'])/'receipt.restricted.json').read_bytes())}}]
                if prefix!=expected_prefix:return self._blocked('VERIFIED_EVENT_CHANGED')
            elif prefix and self.handbook.evidence_mode!='SYNTHETIC_CONTROL':return self._blocked('EVENT_PROVENANCE_UNKNOWN')
            if commands!=prefix+planned:return self._blocked('EFFECT_PLAN_CHANGED')
        else:
            expected=[]
            class Recorder:
                evidence_mode=self.handbook.evidence_mode
                def append(_self,key,kind,payload,**kwargs):expected.append({'key':key,'kind':kind,'payload':payload})
            recorder=Recorder();recorder.log=recorder
            record_query_round_learning(handbook=recorder,record=record,public=public,
                compatibility_fingerprint=op['compatibility_fingerprint'])
            if commands!=expected:return self._blocked('EFFECT_PLAN_CHANGED')
        if len(tail)>len(commands):return self._blocked('FOREIGN_EVENT_TAIL')
        for event,command in zip(tail,commands):
            if (event['idempotency_key']!=command['key'] or event['event_type']!=command['kind']
                    or event['payload']!=command['payload'] or event['actor']!='workflow' or event['evidence_hashes']):
                return self._blocked('FOREIGN_EVENT_TAIL')
        if op['kind']=='CURATOR_DELTA':
            # Fixed timestamp makes an entry file written immediately before a
            # crash safe to match byte-for-byte, even before ENTRY_CREATED.
            if delta is not None:
                _validate_planned_delta_files(self.handbook,delta,context,op['created_at'])
        # Replay only whitelisted commands. Idempotency keys retain one event and
        # compare payloads; entry/evidence files are installed before references.
        for index,command in enumerate(commands):
            if op['kind']=='CURATOR_DELTA' and command['kind'] in {'ENTRY_CREATED','ENTRY_REVISED'}:
                _install_delta_files(self.handbook,delta,context,op['created_at'])
            self.handbook.log.append(command['key'],command['kind'],command['payload'])
            if index==0:self.checkpoint('partial_events')
            if command['kind'] in {'ENTRY_CREATED','ENTRY_REVISED'}:self.checkpoint('entry_created')
            if command['kind']=='ENTRY_ACTIVATED':self.checkpoint('entry_activated')
        if op['kind']=='CURATOR_DELTA' and op.get('verification') and op['result'].get('entries'):
            registry=Path(op['verification']['registry']);cid=context['refs'][next(iter(context['refs']))]['candidate_id']
            EventLog(registry/'learning-links.sqlite').append('book:'+op['evidence_id']+':'+digest(str(self.handbook.directory.resolve())),
                'CANDIDATE_HANDBOOK_BOUND',{'candidate_id':cid,'evidence_id':op['evidence_id'],
                    'handbook':str(self.handbook.directory.resolve()),'evidence_mode':self.handbook.evidence_mode})
        after=self.snapshot();result=op['result'];receipt={'schema':'rsi-learning-recovery-receipt/v1',
            'evidence_id':op['evidence_id'],'operation_hash':intent['operation_hash'],'after':after,'result':result}
        if op.get('result_path'):
            target=Path(op['result_path'])
            if target.exists() and target.read_bytes()!=canonical(result):return self._blocked('RESULT_CHANGED')
            if not target.exists():atomic_write(target,canonical(result),exclusive=True)
        atomic_write(self.directory/('learning-'+op['evidence_id']+'.json'),canonical(receipt))
        self.checkpoint('completed_receipt')
        atomic_write(self.frontier,canonical(after))
        self.checkpoint('frontier_updated')
        self.checkpoint('before_intent_cleanup')
        intent_path.unlink()
        atomic_write(self.directory/'recovery-status.json',canonical({'status':'RECOVERED','evidence_id':op['evidence_id']}))
        self.verify()
        return result


def _validate_request_proof(proof,response):
    if not isinstance(proof,dict):raise ValueError('RECOVERY_BLOCKED_REQUEST_IDENTITY_UNKNOWN')
    from .budgets import Budget
    request_path,receipt_path=Path(proof['request_path']),Path(proof['receipt_path'])
    if (bytes_hash(request_path.read_bytes())!=proof['request_sha256'] or bytes_hash(receipt_path.read_bytes())!=proof['receipt_sha256']):
        raise ValueError('RECOVERY_BLOCKED_REQUEST_IDENTITY_CHANGED')
    request,receipt=load(request_path),load(receipt_path)
    starts=[e['payload'] for e in Budget(proof['budget_ledger']).log.read() if e['event_type']=='MODEL_REQUEST_STARTED' and e['payload']['request_key']==proof['request_key']]
    ends=[e['payload'] for e in Budget(proof['budget_ledger']).log.read() if e['event_type']=='MODEL_REQUEST_COMPLETED' and e['payload']['request_key']==proof['request_key']]
    state=Budget(proof['budget_ledger']).snapshot()
    if (len(starts)!=1 or len(ends)!=1 or starts[0]['role']!='curator' or starts[0]['request_hash']!=digest(request)
            or state['pending_request_keys'] or state['unbounded_usage_requests'] or receipt.get('transport_status')!='RECEIVED'):
        raise ValueError('RECOVERY_BLOCKED_REQUEST_OR_BILLING_UNKNOWN')
    parts=[p['text'] for m in receipt.get('response',{}).get('output',[]) if m.get('type')=='message'
        and m.get('phase') in (None,'final_answer') for p in m.get('content',[]) if p.get('type')=='output_text']
    import json
    if len(parts)!=1 or json.loads(parts[0])!=response:raise ValueError('RECOVERY_BLOCKED_MODEL_RESPONSE_CHANGED')


def file_bindings(paths):
    return {str(Path(p).resolve()):bytes_hash(Path(p).read_bytes()) for p in paths}


def recovery_implementation():
    root=Path(__file__).resolve().parent
    return {str(p.relative_to(root)):bytes_hash(p.read_bytes()) for p in
            (root/'query_learning.py',root/'handbook/store.py',root/'handbook/contracts.py',root/'contracts.py',root/'events.py')}


def _delta_files(handbook,delta,context,created_at):
    body={**delta['proposed_entry'],'evidence_mode':handbook.evidence_mode,'created_at':created_at}
    data=canonical(body);entry_id=bytes_hash(data)
    bundle={'entry_id':entry_id,'refs':{k:context['refs'][k] for k in body['support_refs']},
            'verified_candidates':context.get('verified_candidates',{}),'compatibility_fingerprint':context['compatibility_fingerprint']}
    evidence=canonical(bundle)
    return entry_id,data,bytes_hash(evidence),evidence


def _validate_planned_delta_files(handbook,delta,context,created_at):
    if delta['op']=='DEPRECATE':return
    entry_id,data,bundle_hash,bundle=_delta_files(handbook,delta,context,created_at)
    for path,expected in [(handbook.directory/'entries'/entry_id,data),(handbook.directory/'evidence'/bundle_hash,bundle)]:
        if path.exists() and path.read_bytes()!=expected:raise ValueError('RECOVERY_BLOCKED_ENTRY_CHANGED')
    for ref in context['refs'].values():
        if ref.get('result_path') and bytes_hash(Path(ref['result_path']).read_bytes())!=ref['result_hash']:
            raise ValueError('RECOVERY_BLOCKED_EVIDENCE_CHANGED')


def _install_delta_files(handbook,delta,context,created_at):
    _validate_planned_delta_files(handbook,delta,context,created_at)
    entry_id,data,bundle_hash,bundle=_delta_files(handbook,delta,context,created_at)
    for path,value in [(handbook.directory/'entries'/entry_id,data),(handbook.directory/'evidence'/bundle_hash,bundle)]:
        if not path.exists():atomic_write(path,value,exclusive=True)


def _plan_delta(handbook,delta,context,evidence_id,created_at):
    if delta is None:return [],None
    from .handbook.contracts import validate_entry,conflicting,conditions_match
    if not delta['evidence_refs'] or set(delta['evidence_refs'])-set(context['refs']):raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
    for ref in context['refs'].values():
        if any(not isinstance(ref.get(k),str) or not ref[k] for k in ('observation_id','candidate_id','parent_id','run_id','result_hash','source_group')):
            raise ContractError('UNTRACEABLE_HANDBOOK_EVIDENCE')
    if delta['op']=='DEPRECATE':
        handbook.get(delta['target_entry_id'])
        return [{'key':'delta:'+evidence_id,'kind':'DEPRECATION_REQUESTED','payload':{'entry_id':delta['target_entry_id'],
            'delta_hash':digest(delta),'reason':delta['reason'],'evidence_refs':delta['evidence_refs']}}],delta['target_entry_id']
    entry=validate_entry(delta['proposed_entry'])
    if ('allowed_actions' in context and entry['action'] not in context['allowed_actions']):raise ContractError('UNSUPPORTED_HANDBOOK_INFERENCE')
    if entry['compatibility_fingerprint']!=context['compatibility_fingerprint'] or set(entry['applicable_format'])-set(context['allowed_formats']):
        raise ContractError('INCOMPATIBLE_HANDBOOK_SCOPE')
    if not entry['support_refs'] or set(entry['support_refs'])-set(delta['evidence_refs']):raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
    if delta['op']=='ADD' and (delta['target_entry_id'] is not None or entry['supersedes']):raise ContractError()
    if delta['op']=='REVISE':
        handbook.get(delta['target_entry_id'])
        if entry['supersedes']!=[delta['target_entry_id']]:raise ContractError()
    entry_id,data,bundle_hash,bundle=_delta_files(handbook,delta,context,created_at)
    commands=[{'key':'delta:'+evidence_id,'kind':'ENTRY_CREATED' if delta['op']=='ADD' else 'ENTRY_REVISED',
        'payload':{'entry_id':entry_id,'delta_hash':digest(delta),'evidence_refs':delta['evidence_refs'],'supersedes':entry['supersedes']}},
        {'key':'evidence:'+entry_id+':'+bundle_hash,'kind':'ENTRY_EVIDENCE_BOUND',
         'payload':{'entry_id':entry_id,'evidence_bundle_sha256':bundle_hash}}]
    conflict=False
    for other_id,state in handbook.view()['states'].items():
        if state=='DEPRECATED' or not conflicting(entry,handbook.get(other_id)):continue
        conflict=True;pair=sorted([entry_id,other_id])
        for target in pair:commands.append({'key':'conflict:'+':'.join(pair)+':'+target,'kind':'ENTRY_QUARANTINED',
            'payload':{'entry_id':target,'conflicting_entries':pair,'reason':'FINITE_ACTION_CONFLICT'}})
    refs=[context['refs'][k] for k in entry['support_refs'] if conditions_match(entry,context['refs'][k].get('features',{}))]
    groups={r['source_group'] for r in refs};candidates={r['candidate_id'] for r in refs}
    verified={c for c in candidates if isinstance(context.get('verified_candidates',{}).get(c),str) and len(context['verified_candidates'][c])==64}
    if not conflict and len(groups)>=2 and verified:
        commands.append({'key':'activate:'+entry_id+':'+digest(sorted(verified)),'kind':'ENTRY_ACTIVATED',
            'payload':{'entry_id':entry_id,'source_group_count':len(groups),'verified_candidate_ids':sorted(verified),'evidence_refs':entry['support_refs']}})
    return commands,entry_id


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
        safe['evidence_ref'] = 'evidence-' + digest({'id': self.evidence_id, 'aggregate': True})[:32]
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
    receipt = output / 'learning-receipt.json'
    if receipt.exists() and load(receipt).get('handbook_binding')!=book_binding:
        raise ValueError('LEARNING_HANDBOOK_BINDING_CHANGED')
    # Deterministic aliases and receipt binding make restart idempotent. Never
    # call the model again once a response has been archived, including ABSTAIN.
    public_path=output/'curator-public.json';context_path=output/'curator-context.restricted.json'
    for path,value in [(public_path,public),(context_path,context)]:
        if path.exists():
            if path.read_bytes()!=canonical(value):raise ValueError('LEARNING_CONTEXT_CHANGED')
        else:atomic_write(path,canonical(value),exclusive=True)
    response_path = output / 'curator.json'
    if response_path.exists():
        response = load(response_path)
    else:
        response = roles.call('curator', public)
        atomic_write(response_path, canonical(response), exclusive=True)
    delta = unwrap_response(response)
    if delta is not None:
        validate_readonly(delta, public, 'curator')
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
    if session:
        proof=None
        if bound.mode=='REAL_REPLAY':
            request_key=public['round_id']+'-curator-0'
            # A repaired envelope is tied to the attempt that actually produced
            # this archived response, never guessed from a cached return value.
            for attempt in (1,0):
                key=public['round_id']+'-curator-'+str(attempt)
                request=roles.directory/(key+'.request.json');received=roles.directory/(key+'.json')
                if request.is_file() and received.is_file():
                    candidate_proof={'request_key':key,'request_path':str(request.resolve()),
                        'receipt_path':str(received.resolve()),'request_sha256':bytes_hash(request.read_bytes()),
                        'receipt_sha256':bytes_hash(received.read_bytes()),'budget_ledger':str(roles.budget.log.path.resolve())}
                    try:_validate_request_proof(candidate_proof,response)
                    except ValueError:continue
                    proof=candidate_proof;break
            if proof is None:raise ValueError('RECOVERY_BLOCKED_REQUEST_IDENTITY_UNKNOWN')
        prefix=[{'key':'query-verified:'+bound.evidence_id,'kind':'HOLDOUT_PASSED',
            'payload':{'candidate_id':bound.candidate['candidate_id'],'evidence_version':bound.evidence_id,
                'evidence_mode':bound.mode,'receipt_hash':bytes_hash(bound.receipt_path.read_bytes())}}]
        result=session.learn_delta(bound.evidence_id,response_path=response_path,public_path=public_path,
            context_path=context_path,prefix_events=prefix,result={'schema':'query-learning/v1','evidence_id':bound.evidence_id,
                'response_hash':digest(response),'entries':[],'evidence_mode':bound.mode,'handbook_binding':book_binding,
                'real_eligible':bound.mode=='REAL_REPLAY','association_only':True},result_path=receipt,
            verification={'registry':str(Path(registry).resolve()),'training':str(Path(training).resolve()),
                'train_fixtures':[str(Path(f).resolve()) for f in train_fixtures],'verification':str(Path(verification).resolve())},
            request_proof=proof)
    else:result=commit()
    if receipt.exists():
        if receipt.read_bytes()!=canonical(result):raise ValueError('LEARNING_RECEIPT_CHANGED')
    else:atomic_write(receipt, canonical(result), exclusive=True)
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
