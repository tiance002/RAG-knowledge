"""Trusted adapter over the unchanged candidate/materialize/replay interfaces.

One instance owns both writer locks. Each proposal has its own replay directory,
even if it compiles to the same worker. Partial replays require explicit recovery.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rsi_candidates as registry_api
import run_rsi_offline as executor
from run_rsi_loop import apply_proposal, selector

from .adjudicate import evaluate_candidate, adjudicate_round
from .capsule import Capsule
from .execution_evidence import resolve_policy, check_receipt
from .errors import StageFailure
from .events import EventLog
from .foundation import check_split, verify_boundary
from .locking import WorkspaceLock, atomic_write
from .lineage import LineagePolicy, ParentPool
from .handbook.store import Handbook
from .selector_contract import interface_contract, structural_counts, INTENTS
from .provenance import audit_run
from .redact import LeakScanner, make_visible, project_summary
from .state import StageJournal, canonical, digest
from .workflow import execute_round


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def unit_worker(parent, changed, angle):
    """Frozen invariant probes execute inside the same constrained capsule."""
    functions = []
    for name in ('require', 'duplicate_key'):
        node = next(n for n in ast.parse(parent.decode()).body if isinstance(n, ast.FunctionDef) and n.name == name)
        functions.append(ast.get_source_segment(parent.decode(), node))
    functions.append(selector(parent.decode())[1].replace('def select_member_indices(', 'def parent_select('))
    functions.append(selector(changed.decode())[1])
    probes = '''
import copy,json,sys
angle=ANGLE
cases=0
for size in (0,1,2,6):
    for seed_position in range(max(1,size)):
        for selection in ('all','exact_duplicate'):
            members=[{'id':'synthetic-'+str(i),'file_id':'f','kb_id':'k','content':'x'*(1 if i%2 else 8000)} for i in range(size)]
            chunks={m['id']:{**m,'source_locator':None,'original_index':i} for i,m in enumerate(members)}
            files={'f':{'id':'f','kb_id':'k','name':'synthetic.txt'}}
            seeds=[] if not size else [members[seed_position]['id']]
            args=(members,chunks,files,seeds,selection)
            before=copy.deepcopy(args)
            try:
                indices=select_member_indices(*args)
            except Exception as error:
                raise ValueError('UNIT_SELECTOR_EXCEPTION:'+type(error).__name__) from None
            require(args==before,'UNIT_INPUT_MUTATION')
            require(isinstance(indices,list) and all(type(i) is int and 0<=i<size for i in indices),'UNIT_INVALID_INDICES')
            require(len(set(indices))==len(indices) and indices==sorted(indices),'UNIT_INVALID_ORDER')
            require(all(s in [members[i]['id'] for i in indices] for s in seeds),'UNIT_SEED_LOSS')
            if angle=='P3_ORDER':require(set(indices)==set(parent_select(*args)),'ANGLE_VIOLATION')
            cases+=1
with open(sys.argv[-1],'x') as out:json.dump({'status':'OK','unit_cases':cases,'angle':angle},out)
'''.replace('ANGLE', repr(angle), 1)
    return ('\n\n'.join(functions) + '\n' + probes).encode()


class Experiment:
    def __init__(self, *, registry, fixture, parent_id, source_run, observation_id,
                 directory, business_root, host, identity, round_id, experiment_intent='STRUCTURAL_POLICY_SEARCH',robustness_fixture=None):
        self.registry, self.fixture = Path(registry), Path(fixture)
        self.directory, self.source_run = Path(directory), Path(source_run)
        self.parent_id, self.observation_id, self.round_id = parent_id, observation_id, round_id
        if experiment_intent not in INTENTS:raise ValueError('UNKNOWN_EXPERIMENT_INTENT')
        if experiment_intent.startswith('QUERY_'):raise ValueError('QUERY_INTENT_REQUIRES_INDEPENDENT_PROFILE')
        self.experiment_intent=experiment_intent
        self.robustness_fixture=Path(robustness_fixture) if robustness_fixture else None
        self.robustness_fixtures=[self.robustness_fixture] if self.robustness_fixture else []
        self.business_root = Path(business_root)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = StageJournal(self.directory / 'stages')
        self.events = EventLog(self.directory / 'events.sqlite')
        self.lineage_policy = LineagePolicy(self.registry / 'policy')
        self.capsule = Capsule(host, identity, business_root, self.directory / 'capsule')
        self.deadline = None

    def public_context(self):
        verify_boundary()
        self.freeze = check_split(self.fixture, expected_count=20)
        frozen = load(self.fixture / 'freeze.json')
        for name, expected in frozen['artifact_hashes'].items():
            if bytes_hash((self.fixture / name).read_bytes()) != expected:
                raise StageFailure('INPUT_ERROR', 'FIXTURE_TAMPERED', global_stop=True)
        self.parent = registry_api.candidate(self.registry, self.parent_id)
        if self.parent.get('origin')!='r0':
            ParentPool(self.registry/'policy').require_verified(self.parent_id,self.lineage_policy)
        if (self.parent['cases'] != bytes_hash(canonical(load(self.fixture / 'cases.json')))
                or self.parent['evaluation'] != bytes_hash(canonical(load(self.fixture / 'evaluation.json')))):
            raise StageFailure('REGISTRY_ERROR', 'FIXTURE_TAMPERED')
        self.parent_bytes = registry_api.get(self.registry, self.parent['worker'])
        feedback = load(self.source_run / 'feedback.json')
        from rsi_feedback import build_feedback
        if feedback != build_feedback(self.source_run, feedback['run_id']):
            raise StageFailure('INPUT_ERROR', 'OBSERVATION_INVALID', global_stop=True)
        if sum(o['observation_id'] == self.observation_id for o in feedback['observations']) != 1:
            raise StageFailure('INPUT_ERROR', 'OBSERVATION_INVALID', global_stop=True)
        source_results = load(self.source_run / 'results.json')
        source_manifest = load(self.source_run / 'manifest.json')
        expected_parent = self.parent_id if 'r1' in source_manifest else self.parent.get('original_candidate_id')
        matching_arms = [arm for arm in ('A','B') if source_manifest['arms'][arm]['candidate_id']==expected_parent]
        if len(matching_arms)!=1:
            raise StageFailure('INPUT_ERROR','OBSERVATION_INVALID',global_stop=True)
        observed_arm=matching_arms[0]
        if source_manifest['cases']['sha256']!=self.parent['cases'] or source_manifest['evaluation']['sha256']!=self.parent['evaluation']:
            raise StageFailure('INPUT_ERROR','FIXTURE_TAMPERED',global_stop=True)
        if source_results['summary']['paired'] != 20 or source_results['summary']['failed']:
            raise StageFailure('INPUT_ERROR', 'PARENT_FAULT', global_stop=True)
        position = audit_run(self.source_run, self.fixture)
        if not position['complete'] or not position['all_required_present']:
            raise StageFailure('INPUT_ERROR', 'PARENT_FAULT', global_stop=True)
        counts = {'questions': 20, 'source_groups': 20, 'paired': 20, 'blocked': 0, 'failed': 0,
                  'delivered_members': sum(len(r['arms'][observed_arm]['member_ids']) for r in source_results['rows']),
                  'seed_members': sum(len(r['seed_ids']) for r in source_results['rows']),
                  'context_chars': sum(r['arms'][observed_arm]['context_chars'] for r in source_results['rows'])}
        counts['nonseed_members'] = counts['delivered_members'] - counts['seed_members']
        counts.update(structural_counts(self.source_run,observed_arm))
        cases = load(self.fixture / 'cases.json')
        private = [self.parent_id, self.observation_id, str(self.fixture.resolve()), str(self.registry.resolve())]
        for item in cases:
            private.append(item['case']['question'])
        for predicates in load(self.fixture / 'evaluation.json').values():
            private.extend(f['text'] for f in predicates['facts'])
        self.scanner = LeakScanner(private)
        self.handbook = Handbook(self.registry / 'handbook', scanner=self.scanner)
        public_path = self.directory / 'public-context.json'
        if public_path.exists():
            public = load(public_path)
            self.scanner.check(public)
            if public['parent_code'] != selector(self.parent_bytes.decode())[1] or public['round_id'] != self.round_id:
                raise StageFailure('RECOVERY_ERROR', 'INPUT_CHANGED')
        else:
            compatibility = digest({'seam': 'select_member_indices/body/v1',
                'evaluator': executor.EVALUATOR, 'fixture': self.parent['cases'],
                'config': self.parent['config'], 'protected': verify_boundary()})
            entries = []
            retrieval_features={'format':'OTHER','same_source':all(
                len({f['id'] for f in item['case']['files']})==1 for item in cases)}
            for section in ('dedup','budget','order'):
                entries.extend(self.handbook.retrieve(section,'OTHER',retrieval_features,compatibility,limit=3-len(entries)))
            public = make_visible([project_summary('NONSEED_EXPANSION', counts, include_count_evidence=True)],
                                  parent_code=selector(self.parent_bytes.decode())[1],
                                  round_id=self.round_id, scanner=self.scanner,
                                  handbook_entry_ids=[e['entry_id'] for e in entries],handbook_entries=entries)
            public['parent_selection'] = json.loads(registry_api.get(self.registry, self.parent['config']))['selection']
            public['selector_contract']=interface_contract()
            public['experiment_intent']=self.experiment_intent
            atomic_write(public_path, canonical(public), exclusive=True)
            atomic_write(self.directory / 'private-binding.json', canonical({
                'parent_id': self.parent_id, 'observation_id': self.observation_id,
                'observation_alias': public['observation_refs'][0], 'source_results_sha256': bytes_hash((self.source_run / 'results.json').read_bytes()),
                'freeze_sha256': bytes_hash((self.fixture / 'freeze.json').read_bytes())}), exclusive=True)
        return public

    def ensure_capsule(self):
        if not self.capsule.ready:
            self.capsule.setup()

    def review(self, proposal):
        legacy = {k: proposal[k] for k in ('hypothesis', 'change', 'falsification')}
        legacy.update(old=proposal['worker_patch']['old'], new=proposal['worker_patch']['new'], impact=proposal['expected_impact'])
        try:
            changed = apply_proposal(self.parent_bytes, legacy)
            compile(changed, '<candidate>', 'exec')
        except (ValueError, SyntaxError):
            raise StageFailure('SEAM_ERROR', 'PATCH_INVALID') from None
        slot_dir = self.directory / proposal['angle']
        slot_dir.mkdir(exist_ok=True)
        worker = slot_dir / 'worker.py'
        if worker.exists() and worker.read_bytes() != changed:
            raise StageFailure('RECOVERY_ERROR', 'INPUT_CHANGED')
        if not worker.exists(): atomic_write(worker, changed, exclusive=True)
        tests = slot_dir / 'unit'
        def reconcile(_):
            if (tests / 'capture.json').exists():
                receipt = load(tests / 'capture.json')
                return {'status': 'MATCHED', 'output': receipt} if receipt.get('status') == 'OK' else {'status': 'UNKNOWN'}
            return {'status': 'UNKNOWN'} if tests.exists() else {'status': 'ABSENT', 'safe_retry': True}
        def action(_):
            tests.mkdir()
            atomic_write(tests / 'worker.py', unit_worker(self.parent_bytes, changed, proposal['angle']), exclusive=True)
            atomic_write(tests / 'input.json', b'{}', exclusive=True)
            self.ensure_capsule()
            self.capsule.deadline = min(time.monotonic() + 90, self.deadline) if self.deadline else time.monotonic()+90
            try:
                return self.capsule.child('prepare', [tests / 'input.json', tests / 'capture.json'], tests,
                                          worker=tests / 'worker.py', business_root=self.business_root)
            except ValueError:
                log = (tests / 'worker.log').read_text(encoding='utf-8', errors='replace')
                if 'ANGLE_VIOLATION' in log:
                    raise StageFailure('PATCH_ERROR', 'ANGLE_VIOLATION') from None
                if 'UNIT_' in log or 'max() arg is an empty sequence' in log:
                    raise StageFailure('PATCH_ERROR', 'UNIT_TEST_FAILED') from None
                raise StageFailure('REPLAY_ERROR', 'INFRA_FAILURE') from None
        self.journal.execute(proposal['angle'] + '-unit', {'worker_sha256': bytes_hash(changed), 'angle': proposal['angle']}, action, reconcile)
        return {'worker_path': str(worker), 'worker_hash': bytes_hash(changed)}

    def replay(self, proposal, artifact):
        slot, worker_hash = proposal['angle'], artifact['worker_hash']
        slot_dir = self.directory / slot
        notes = {'observation': 'Frozen aggregate context-length observation; optimization hypothesis only.',
                 'hypothesis': proposal['hypothesis'], 'change': proposal['change'],
                 'impact': proposal['expected_impact'], 'falsification': proposal['falsification']}
        config = slot_dir / 'config.json'
        if not config.exists(): atomic_write(config, registry_api.get(self.registry, self.parent['config']), exclusive=True)
        def derived(_):
            matches = []
            for path in (self.registry / 'objects').iterdir():
                raw = registry_api.get(self.registry, path.name)
                try: value = json.loads(raw)
                except (ValueError, UnicodeError): continue
                if isinstance(value, dict) and value.get('type') == 'rsi-candidate-v1' and value.get('parent') == self.parent_id and value.get('worker') == worker_hash and value.get('notes') == notes:
                    record = registry_api.candidate(self.registry, path.name)
                    if (record['config'] != self.parent['config'] or record.get('observation_source', {}).get('observation_id') != self.observation_id):
                        return {'status': 'CONFLICT'}
                    matches.append(path.name)
            if len(matches) == 1: return {'status': 'MATCHED', 'output': {'candidate_id': matches[0]}}
            return {'status': 'CONFLICT'} if matches else {'status': 'ABSENT', 'safe_retry': True}
        try:
            child, _ = self.journal.execute(slot + '-derive', {'parent': self.parent_id, 'worker_hash': worker_hash,
                'config': self.parent['config'], 'notes': notes, 'observation_id': self.observation_id},
                lambda _: {'candidate_id': registry_api.derive(self.registry, self.parent_id, config,
                    Path(artifact['worker_path']), notes, self.source_run, self.observation_id)}, derived)
        except (OSError, ValueError, RuntimeError):
            raise StageFailure('REGISTRY_ERROR', 'REGISTRY_INTEGRITY_FAILURE') from None
        candidate_id = child['candidate_id']
        bundle = slot_dir / 'bundle'
        def materialized(_):
            if not bundle.exists(): return {'status': 'ABSENT', 'safe_retry': True}
            if not (bundle / 'manifest.json').exists(): return {'status': 'UNKNOWN'}
            manifest = load(bundle / 'manifest.json')
            registry_api.verify_arm_sources(manifest, bundle)
            if manifest['r1']['parent'] != self.parent_id or manifest['r1']['child'] != candidate_id or 'fixture' in manifest['r1']:
                return {'status': 'CONFLICT'}
            return {'status': 'MATCHED', 'output': {'manifest_sha256': bytes_hash((bundle / 'manifest.json').read_bytes())}}
        def materialize(_):
            registry_api.materialize(self.registry, self.parent_id, candidate_id, bundle)
            return materialized(None)['output']
        try:
            self.journal.execute(slot + '-materialize', {'parent': self.parent_id, 'child': candidate_id}, materialize, materialized)
        except (OSError, ValueError, RuntimeError):
            raise StageFailure('MATERIALIZE_ERROR', 'MATERIALIZE_INTEGRITY_FAILURE', global_stop=True) from None
        run_dir = slot_dir / 'run'
        def replayed(_):
            if not run_dir.exists(): return {'status': 'ABSENT', 'safe_retry': True}
            if not (run_dir / 'results.json').exists(): return {'status': 'UNKNOWN'}
            # All result/capture bytes enter the stage receipt. Reuse validates
            # every preserved evidence file, never just an exit status.
            files = {p.relative_to(run_dir).as_posix(): bytes_hash(p.read_bytes()) for p in run_dir.rglob('*') if p.is_file()}
            return {'status': 'MATCHED', 'output': {'files': files}}
        def replay_action(_):
            self.ensure_capsule()
            self.capsule.deadline = min(time.monotonic()+300, self.deadline) if self.deadline else time.monotonic()+300
            with self.capsule.bind(executor):
                executor.run(bundle / 'manifest.json', run_dir, business_root=self.business_root)
            return replayed(None)['output']
        try:
            self.journal.execute(slot + '-replay', {'manifest_sha256': bytes_hash((bundle / 'manifest.json').read_bytes())}, replay_action, replayed)
            decision = read_decision(run_dir, self.fixture, candidate_id, proposal, worker_hash, self.parent['config'])
            if self.robustness_fixtures and decision.get('gates_passed'):
                from .robustness import replay_robustness,gate_training,gate_training_suite
                receipts=[]
                for number,fixture in enumerate(self.robustness_fixtures):
                    suffix='robustness' if number==0 else 'robustness-'+str(number+1)
                    if not (self.directory/'stages'/(slot+'-'+suffix+'-replay')/'checkpoint.json').is_file():
                        self.ensure_capsule()
                    receipt=replay_robustness(self.registry,self.parent_id,candidate_id,fixture,
                        slot_dir/suffix,self.capsule,self.journal,slot+'-'+suffix,self.deadline or time.monotonic()+1800)
                    receipts.append(receipt)
                    if not receipt['passed']:break
                decision=gate_training_suite(decision,receipts) if len(self.robustness_fixtures)>1 else gate_training(decision,receipts[0])
        except StageFailure:
            raise
        except (OSError, ValueError, RuntimeError, KeyError):
            raise StageFailure('REPLAY_ERROR', 'INFRA_FAILURE') from None
        if (slot_dir / 'decision.json').exists():
            if load(slot_dir / 'decision.json') != decision:
                raise StageFailure('RECOVERY_ERROR', 'DECISION_CHANGED')
        else:
            atomic_write(slot_dir / 'decision.json', canonical(decision), exclusive=True)
        return decision

    def reconcile_completed(self):
        """Read and revalidate completed side effects; no role or worker calls."""
        result = load(self.directory / 'round.json')
        events = [e['payload'] for e in self.lineage_policy.log.read()
                  if e['event_type']=='ROUND_FINISHED' and e['payload']['round_id']==self.round_id]
        if len(events)!=1 or events[0]['result_hash']!=bytes_hash((self.directory/'round.json').read_bytes()):
            raise StageFailure('RECOVERY_ERROR','ROUND_RECEIPT_UNKNOWN')
        decisions = list(result.get('candidate_decisions', []))
        for slot in result['qualified']:
            # A completed round never repairs absent checkpoints by doing work.
            if not all((self.directory/'stages'/(slot+'-'+stage)/'checkpoint.json').is_file()
                       for stage in ('unit','derive','materialize','replay')):
                raise StageFailure('RECOVERY_ERROR','COMPLETED_CHECKPOINT_MISSING')
            proposal=result['proposal_instances'][slot]
            archived=load(self.directory/slot/'decision.json')
            if 'robustness' in archived:
                archived_receipts=archived.get('robustness_suite',[archived['robustness']])
                path=Path(archived_receipts[0]['fixture_path'])
                if self.robustness_fixture and self.robustness_fixture.resolve()!=path.resolve():
                    raise StageFailure('RECOVERY_ERROR','ROBUSTNESS_FIXTURE_CHANGED')
                self.robustness_fixture=path
                self.robustness_fixtures=[Path(r['fixture_path']) for r in archived_receipts]
                if not all((self.directory/'stages'/(slot+'-'+('robustness' if i==0 else 'robustness-'+str(i+1))+'-'+stage)/'checkpoint.json').is_file()
                           for i,_ in enumerate(archived_receipts) for stage in ('materialize','replay')):
                    raise StageFailure('RECOVERY_ERROR','COMPLETED_CHECKPOINT_MISSING')
            artifact=self.review(proposal)
            decision=self.replay(proposal,artifact)
            decisions=[decision if d['angle']==slot else d for d in decisions]
        if decisions:
            outcome=adjudicate_round(decisions,all_qualified_terminal=set(result['qualified'])==set(result['attempted_replays']))
            if any(result.get(k)!=v for k,v in outcome.items()):
                raise StageFailure('RECOVERY_ERROR','ROUND_DECISION_CHANGED')
        verify_boundary()
        return result

    def run(self, roles, *, enabled_slots, resume=False):
        with WorkspaceLock(self.registry / 'writer.lock', self.round_id), WorkspaceLock(self.directory / 'run.lock', self.round_id):
            self.deadline = time.monotonic()+1800
            roles.deadline = self.deadline
            public = self.public_context()
            if (self.directory/'round.json').exists():
                if not resume:raise StageFailure('RECOVERY_ERROR','EXPLICIT_RESUME_REQUIRED')
                return self.reconcile_completed()
            required=[e['payload'] for e in self.lineage_policy.log.read() if e['event_type']=='REQUIRED_ROBUSTNESS_FIXTURE'
                      and e['payload']['primary_cases_sha256']==self.parent['cases']]
            if required:
                paths=[]
                for requirement in required:
                    required_path=Path(requirement['fixture_path'])
                    if bytes_hash((required_path/'freeze.json').read_bytes())!=requirement['freeze_sha256']:
                        raise StageFailure('INPUT_ERROR','ROBUSTNESS_FIXTURE_CHANGED',global_stop=True)
                    if required_path.resolve() not in [p.resolve() for p in paths]:paths.append(required_path)
                if self.robustness_fixture and self.robustness_fixture.resolve() not in [p.resolve() for p in paths]:
                    raise StageFailure('INPUT_ERROR','ROBUSTNESS_FIXTURE_CHANGED',global_stop=True)
                self.robustness_fixtures=paths;self.robustness_fixture=paths[0]
            ancestor, visited = self.parent_id, set()
            while True:
                if ancestor in visited:
                    raise StageFailure('REGISTRY_ERROR', 'LINEAGE_CYCLE')
                visited.add(ancestor)
                record = registry_api.candidate(self.registry, ancestor)
                if record.get('origin') == 'r0': break
                ancestor = record['parent']
            self.lineage_policy.start_round(ancestor, self.round_id, input_hash=digest(public))
            for entry_id in public['handbook_entry_ids']:
                self.handbook.log.append('matched:'+self.round_id+':'+entry_id,'HANDBOOK_MATCHED',
                    {'entry_id':entry_id,'round_id':self.round_id,'parent_id':self.parent_id})
            try:
                result = execute_round(public, roles, self.review, self.replay, enabled_slots=enabled_slots, deadline=self.deadline)
                result.update(production_changed=False, source_boundary=verify_boundary(),
                    evidence_mode=self.capsule.evidence_policy.mode,
                    real_eligible=self.capsule.evidence_policy.real_eligible,
                    execution_policy=self.capsule.evidence_policy.to_dict())
                atomic_write(self.directory / 'round.json', canonical(result), exclusive=True)
                self.lineage_policy.finish_round(ancestor, self.round_id, result,
                    result_hash=bytes_hash((self.directory / 'round.json').read_bytes()))
                for decision in result.get('candidate_decisions',[]):
                    cid=decision.get('candidate_id')
                    if not cid:continue
                    proposal=result['proposal_instances'][decision['angle']]
                    candidate=registry_api.candidate(self.registry,cid)
                    equivalent=digest({'worker':candidate['worker'],'config':candidate['config']})
                    for entry_id in proposal['handbook_entry_ids']:
                        self.handbook.log.append('referenced:'+self.round_id+':'+decision['angle']+':'+entry_id,
                            'PROPOSAL_REFERENCED',{'entry_id':entry_id,'round_id':self.round_id,
                                'candidate_id':cid,'proposal_id':proposal['proposal_id'],'equivalence_hash':equivalent})
                    kind=('OUTCOME_INVALIDATED' if result['verdict']=='ROUND_ABORTED' else
                          'CANDIDATE_ADOPTED' if cid==result.get('adopted_candidate_id') else
                          'CANDIDATE_REJECTED' if decision['verdict']=='REJECTED' else
                          'CANDIDATE_INCONCLUSIVE' if decision['verdict']=='INCONCLUSIVE' else 'CANDIDATE_NOT_SELECTED')
                    self.handbook.log.append('outcome:'+self.round_id+':'+cid,kind,
                        {'candidate_id':cid,'round_id':self.round_id,'result_hash':bytes_hash((self.directory/'round.json').read_bytes())})
                if result.get('adopted_candidate_id'):
                    self.events.append(self.round_id + ':adoption', 'CANDIDATE_ADOPTED', {
                        'candidate_id': result['adopted_candidate_id'], 'parent_id': self.parent_id,
                        'round_result_sha256': bytes_hash((self.directory / 'round.json').read_bytes())})
                    chosen=next(d for d in result['candidate_decisions'] if d.get('candidate_id')==result['adopted_candidate_id'])
                    run_dir=self.directory/chosen['angle']/'run'
                    receipts=[load(p) for p in run_dir.rglob('execution-receipt.json')]
                    seconds=sum(r.get('elapsed_seconds',0) for r in receipts)
                    if seconds>0:
                        ParentPool(self.registry/'policy').admit(result['adopted_candidate_id'],ancestor,self.directory/'round.json',
                            kind='EXPLORATION',chars=sum(r['chars_B'] for r in chosen['per_question']),replay_seconds=seconds, registry=self.registry, fixture=self.fixture,
                            evidence=self.capsule.evidence_policy)
                return result
            finally:
                self.capsule.close()


def read_decision(run_dir, fixture, candidate_id, proposal, worker_hash, config_hash, *, evidence=None):
    """Recompute protected assessments from hash-bound captures, then add provenance."""
    run_dir, fixture = Path(run_dir), Path(fixture)
    results, manifest = load(run_dir / 'results.json'), load(run_dir / 'manifest.json')
    cases, rubric = load(fixture / 'cases.json'), load(fixture / 'evaluation.json')
    if (manifest['arms']['B']['candidate_id'] != candidate_id
            or manifest['arms']['B']['worker_sha256'] != worker_hash
            or manifest['arms']['B']['sha256'] != config_hash):
        raise StageFailure('EVALUATION_ERROR', 'INVALID_PAIR', global_stop=True)
    if manifest['cases']['sha256'] != bytes_hash(canonical(cases)) or manifest['evaluation']['sha256'] != bytes_hash(canonical(rubric)):
        raise StageFailure('EVALUATION_ERROR', 'FIXTURE_TAMPERED', global_stop=True)
    proof = audit_run(run_dir, fixture)
    if evidence is None and any(not (run_dir/f'case-{i:03}'/'execution-receipt.json').is_file()
                                for i in range(1, len(cases)+1)):
        raise StageFailure('EVALUATION_ERROR', 'EXECUTION_EVIDENCE_MISSING', global_stop=True)
    policy = resolve_policy(evidence)
    runtime_identity = None
    by_qid = {r['qid']: r for r in results['rows']}
    if len(by_qid) != len(results['rows']):
        raise StageFailure('EVALUATION_ERROR', 'INVALID_PAIR', global_stop=True)
    rows = []
    for index, (item, position) in enumerate(zip(cases, proof['rows'], strict=True), 1):
        qid = item['qid']; raw = by_qid.get(qid, {})
        row = {'qid': qid, 'source_group': position['source_group'], 'chars_A': None, 'chars_B': None,
            'parent_facts': {}, 'candidate_facts': {}, 'parent_faults': [], 'candidate_faults': [],
            'pair_status': raw.get('status', 'MISSING'), 'pair_identity': False,
            'execution': 'COMPLETED' if raw.get('status') == 'PAIRED' else 'FAILED', 'fault_origin': 'INFRA'}
        if policy.real_eligible:
            if set(raw.get('arms', {})) != {'A', 'B'}:
                raise StageFailure('EVALUATION_ERROR', 'EXECUTION_EVIDENCE_MISSING', global_stop=True)
            directory = run_dir / f'case-{index:03}'
            runtime_identity = check_receipt(policy, directory/'execution-receipt.json',
                stage='prepare', worker_hash=bytes_hash((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),
                input_path=directory/'input.json', capture_path=directory/'snapshot.json',
                runtime_identity=runtime_identity)
            if executor.execution_view(load(directory/'snapshot.json')) != load(directory/'arm-input.json'):
                raise StageFailure('EVALUATION_ERROR', 'PREPARE_OUTPUT_MISMATCH', global_stop=True)
        for arm, prefix in [('A', 'parent'), ('B', 'candidate')]:
            row['provenance_'+arm] = position['arms'][arm]['status']
            row[prefix+'_facts'] = {f['fact_id']: f['status'] for f in position['arms'][arm]['facts']}
            score = raw.get('arms', {}).get(arm)
            if score:
                path = run_dir / score['receipt_path']
                if not path.resolve().is_relative_to(run_dir.resolve()) or bytes_hash(path.read_bytes()) != score['receipt_sha256']:
                    raise StageFailure('EVALUATION_ERROR', 'INVALID_PAIR', global_stop=True)
                capture = load(path)
                config_path = run_dir/'implementation'/(arm+'.json')
                if not config_path.is_file(): config_path=run_dir/(arm+'.json')
                runtime_identity = check_receipt(policy, path.parent/'execution-receipt.json',
                    stage='deliver', worker_hash=manifest['arms'][arm]['worker_sha256'],
                    input_path=run_dir/f'case-{index:03}'/'arm-input.json', capture_path=path,
                    config_path=config_path, canonical_config_hash=manifest['arms'][arm]['sha256'],
                    runtime_identity=runtime_identity)
                recomputed = executor.assess(item['case'], capture, rubric[qid], arm)
                if any(recomputed[k] != score[k] for k in recomputed):
                    raise StageFailure('EVALUATION_ERROR', 'ASSESSMENT_MISMATCH', global_stop=True)
                row['chars_'+arm] = len(capture['context'])
                row['context_'+arm+'_sha256'] = bytes_hash(capture['context'].encode())
                row[prefix+'_faults'] = score['faults']
        if raw.get('status') == 'PAIRED':
            executor.compare_pair(raw['arms']['A'], raw['arms']['B'])
            snapshot = load(run_dir / f'case-{index:03}' / 'arm-input.json')
            checked = executor.checked_case(item['case'])
            row['pair_identity'] = (raw['input_sha256'] == executor.fingerprint(checked)
                                    and raw['upstream_sha256'] == executor.fingerprint(snapshot))
            row['upstream_hash'] = raw['upstream_sha256']
            if runtime_identity: row['execution_environment_hash'] = digest(runtime_identity)
            row['fault_origin'] = 'CANDIDATE' if row['candidate_faults'] else 'NONE'
            if proposal['angle'] == 'P3_ORDER' and set(raw['arms']['A']['member_ids']) != set(raw['arms']['B']['member_ids']):
                decision = evaluate_candidate({'angle': proposal['angle'], 'proposal_id': proposal['proposal_id'],
                    'candidate_id': candidate_id, 'worker_hash': worker_hash, 'config_hash': config_hash},
                    rows+[row], [i['qid'] for i in cases], total_questions=len(cases))
                decision.update(verdict='REJECTED', reason_codes=['ANGLE_VIOLATION'], gates_passed=False)
                decision.update(evidence_mode=policy.mode, real_eligible=policy.real_eligible, execution_policy=policy.projection(),
                    execution_evidence_hash=digest({p.relative_to(run_dir).as_posix():bytes_hash(p.read_bytes())
                        for p in run_dir.rglob('execution-receipt.json')}))
                return decision
        rows.append(row)
    decision = evaluate_candidate({'angle': proposal['angle'], 'proposal_id': proposal['proposal_id'],
        'candidate_id': candidate_id, 'worker_hash': worker_hash, 'config_hash': config_hash},
        rows, [i['qid'] for i in cases], total_questions=len(cases))
    decision.update(evidence_mode=policy.mode, real_eligible=policy.real_eligible, execution_policy=policy.projection(),
                    execution_evidence_hash=digest({p.relative_to(run_dir).as_posix():bytes_hash(p.read_bytes())
                        for p in run_dir.rglob('execution-receipt.json')}))
    return decision
