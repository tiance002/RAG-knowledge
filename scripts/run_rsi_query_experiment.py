"""One cost-first query-aware engineering experiment on the original USD ledger."""
import argparse,json,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.query_aware import QueryRegistry,adapt_legacy,apply_patch,invariant_probe_source,interface_contract
from rsik.experiment import registry_api,load,bytes_hash,read_decision
from rsik.foundation import check_split,verify_boundary
from rsik.capsule import Capsule
from rsik.contracts import unwrap_response,validate_diagnosis,validate_proposal
from rsik.selector_contract import INTENTS
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.redact import LeakScanner,make_visible,project_summary
from rsik.locking import WorkspaceLock,atomic_write,restrict_directory
from rsik.state import canonical
from rsik.holdout import replay_artifacts
from rsik.adjudicate import adjudicate_round
from run_rsi_loop import selector


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','query-registry','directory','primary','query-stress','next-stress','previous-stress','identity','business-root','key-file','budget-ledger','repair-receipt'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--repair-id',required=True);p.add_argument('--host',required=True)
    p.add_argument('--parent-query-id')
    p.add_argument('--parent-query-receipt',type=Path)
    p.add_argument('--proposal-preview',type=Path,help='replay one archived proposal without resending model requests')
    p.add_argument('--test-freeze',type=Path);p.add_argument('--test-freeze-hash')
    p.add_argument('--intent',choices=sorted(i for i in INTENTS if i.startswith('QUERY_') or i=='STRUCTURAL_POLICY_SEARCH'),required=True)
    a=p.parse_args();verify_boundary()
    def guard():
        if a.test_freeze:
            from rsik.test_phase import assert_frozen
            if (a.test_freeze.parent/'stop-request.json').exists():raise ValueError('OWNER_REQUESTED_STOP')
            if (a.test_freeze.parent/'invalidation-request.json').exists():raise ValueError('TEST_INVALIDATED_BY_ENGINEER')
            assert_frozen(a.test_freeze,a.test_freeze_hash)
    guard()
    contract=interface_contract
    if a.test_freeze:
        from rsik.query_aware import autonomy_interface_contract
        contract=autonomy_interface_contract
    if a.directory.exists():raise ValueError('FRESH_QUERY_EXPERIMENT_DIRECTORY_REQUIRED')
    fixtures=[a.primary,a.query_stress,a.next_stress,a.previous_stress]
    freezes={str(f.resolve()):check_split(f,expected_count=20) for f in fixtures}
    qreg=QueryRegistry(a.query_registry)
    with WorkspaceLock(a.query_registry/'writer.lock','query-experiment'):
        if a.parent_query_id:
            qreg.require_not_quarantined(a.parent_query_id)
            if not a.parent_query_receipt:raise ValueError('QUERY_PARENT_ADOPTION_RECEIPT_REQUIRED')
            parent=qreg.get(a.parent_query_id);previous=load(a.parent_query_receipt)
            if (a.parent_query_receipt.parent/'probes/worker.py').read_bytes()!=invariant_probe_source(parent['worker']):
                raise ValueError('QUERY_PARENT_UNIT_PROFILE_STALE')
            if previous.get('adopted_candidate_id')!=a.parent_query_id or previous.get('suite_expected')!=4 or len(previous.get('suite_decisions',[]))!=4:
                raise ValueError('QUERY_PARENT_NOT_VALIDATED')
            decisions=[]
            for index,f in enumerate(fixtures,1):
                d=read_decision(a.parent_query_receipt.parent/('suite-'+str(index)),f,a.parent_query_id,
                    {'angle':'P2_BUDGET','proposal_id':'rsi-query-aware-engineering/v1'},
                    parent['record']['worker_sha256'],parent['record']['config_sha256'])
                if d!=previous['suite_decisions'][index-1] or not all(d['gates']['G'+str(i)] is True for i in range(1,5)):
                    raise ValueError('QUERY_PARENT_EVIDENCE_CHANGED')
                decisions.append(d)
            if adjudicate_round([decisions[0]],all_qualified_terminal=True).get('adopted_candidate_id')!=a.parent_query_id:
                raise ValueError('QUERY_PARENT_ADOPTION_NOT_REPRODUCIBLE')
        else:
            # New profile bootstrap, not a legacy VERIFIED-pool override.
            receipt=load(a.repair_receipt)
            if receipt['candidate_id']!=a.repair_id or receipt['paired']!=20:raise ValueError('REPAIR_ANCHOR_UNBOUND')
            archived=load(a.repair_receipt.parent/'run/results.json')
            if bytes_hash((a.repair_receipt.parent/'run/results.json').read_bytes())!=receipt['results_sha256'] or any(
                r['status']!='PAIRED' or r['arms']['B']['faults'] or any(f['status']!='PRESENT' for f in r['arms']['B']['facts']) for r in archived['rows']):
                raise ValueError('UNSAFE_REPAIR_ANCHOR')
            legacy=registry_api.candidate(a.registry,a.repair_id)
            audited=read_decision(a.repair_receipt.parent/'run',a.query_stress,a.repair_id,
                {'angle':'P2_BUDGET','proposal_id':'engineering-repair-anchor'},legacy['worker'],legacy['config'])
            if len(audited['per_question'])!=20 or any(row['pair_status']!='PAIRED' or not row['pair_identity']
                    or row['provenance_B']!='PRESENT' or row['candidate_faults'] or not row['candidate_facts']
                    or any(status!='PRESENT' for status in row['candidate_facts'].values()) for row in audited['per_question']):
                raise ValueError('REPAIR_ANCHOR_SOURCE_AUDIT_FAILED')
            worker=adapt_legacy(registry_api.get(a.registry,legacy['worker']))
            cid=qreg.put(worker,registry_api.get(a.registry,legacy['config']),parent_id=a.repair_id,
                origin='OWNER_AUTHORIZED_QUERY_PROFILE_BOOTSTRAP',evidence={'repair_receipt_sha256':bytes_hash(a.repair_receipt.read_bytes()),
                    'legacy_parent_id':legacy['parent'],'scope':'safety restoration anchor; not VERIFIED'})
            parent=qreg.get(cid)
        a.directory.mkdir(parents=True);restrict_directory(a.directory)
        scanner=LeakScanner([str(f.resolve()) for f in fixtures]+[parent['candidate_id']])
        public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20,'source_groups':20,'paired':20,'failed':0},include_count_evidence=True)],
            parent_code=selector(parent['worker'].decode())[1],round_id='round-'+uuid.uuid4().hex,scanner=scanner)
        public.update(selector_contract=contract(),experiment_intent=a.intent,parent_selection='all')
        if a.proposal_preview:
            baseline=load(a.proposal_preview/'baseline.json');archived=load(a.proposal_preview/'public-context.json')
            if (baseline['parent_id']!=parent['candidate_id'] or baseline['parent_worker_sha256']!=bytes_hash(parent['worker'])
                    or baseline['parent_config_sha256']!=bytes_hash(parent['config'])
                    or load(a.proposal_preview/'fixture-freezes.restricted.json')!=freezes
                    or archived['parent_code']!=selector(parent['worker'].decode())[1]
                    or archived['selector_contract']!=contract() or archived['experiment_intent']!=a.intent):
                raise ValueError('PREVIEW_PROPOSAL_PARENT_OR_FIXTURE_CHANGED')
            scanner.check(archived);public=archived
            atomic_write(a.directory/'proposal-preview-source.json',canonical({'path':str(a.proposal_preview.resolve()),
                'proposal_sha256':bytes_hash((a.proposal_preview/'proposal.json').read_bytes()),
                'preview_sha256':bytes_hash((a.proposal_preview/'preview.json').read_bytes()),'new_model_calls':0}),exclusive=True)
        atomic_write(a.directory/'public-context.json',canonical(public),exclusive=True)
        atomic_write(a.directory/'fixture-freezes.restricted.json',canonical(freezes),exclusive=True)
        atomic_write(a.directory/'implementation.json',canonical({str(p.relative_to(ROOT)):bytes_hash(p.read_bytes()) for p in
            [Path(__file__),ROOT/'rsik/query_aware.py',ROOT/'rsik/holdout.py',ROOT/'rsik/experiment.py',ROOT/'rsik/adjudicate.py',ROOT/'rsik/provenance.py',ROOT/'rsik/openai_roles.py']}),exclusive=True)
        budget=Budget(a.budget_ledger)
        roles=OpenAIRoles(budget,a.directory/'roles',a.key_file,scanner,seam_profile='query-aware-v1')
        diagnosis_raw=load(a.proposal_preview/'diagnosis.json') if a.proposal_preview else roles.diagnose(public)
        atomic_write(a.directory/'diagnosis.json',canonical(diagnosis_raw),exclusive=True)
        diagnosis=unwrap_response(diagnosis_raw)
        if diagnosis is None:
            atomic_write(a.directory/'round.json',canonical({'verdict':'NO_ADOPTION','reason':'DIAGNOSIS_ABSTAIN','budget':budget.snapshot()}),exclusive=True);return
        validate_diagnosis(diagnosis,public)
        proposal_raw=load(a.proposal_preview/'proposal.json') if a.proposal_preview else roles.propose('P2_BUDGET',public,diagnosis)
        atomic_write(a.directory/'proposal.json',canonical(proposal_raw),exclusive=True)
        proposal=unwrap_response(proposal_raw)
        if proposal is None:
            atomic_write(a.directory/'round.json',canonical({'verdict':'NO_ADOPTION','reason':'PROPOSAL_ABSTAIN','budget':budget.snapshot()}),exclusive=True);return
        validate_proposal(proposal,public,'P2_BUDGET')
        try:changed=apply_patch(parent['worker'],proposal['worker_patch'])
        except (ValueError,SyntaxError):
            atomic_write(a.directory/'round.json',canonical({'verdict':'REJECTED','reason':'QUERY_AST_PATCH_INVALID','budget':budget.snapshot()}),exclusive=True);return
        child_id=qreg.put(changed,parent['config'],parent_id=parent['candidate_id'],origin='OPENAI_MODEL_PROPOSAL' if a.intent=='STRUCTURAL_POLICY_SEARCH' else 'OPENAI_LUNA_PROPOSAL',
            evidence={'proposal_sha256':bytes_hash((a.directory/'proposal.json').read_bytes()),'fixture_freezes_sha256':bytes_hash((a.directory/'fixture-freezes.restricted.json').read_bytes())})
        child=qreg.get(child_id)
        capsule=Capsule(a.host,a.identity,a.business_root,a.directory/'capsule')
        decisions=[]
        try:
            guard()
            capsule.setup();deadline=time.monotonic()+1500
            probes=a.directory/'probes';probes.mkdir()
            atomic_write(probes/'worker.py',invariant_probe_source(changed),exclusive=True);atomic_write(probes/'input.json',b'{}',exclusive=True)
            capsule.deadline=time.monotonic()+90
            try:
                unit=capsule.child('prepare',[probes/'input.json',probes/'capture.json'],probes,worker=probes/'worker.py',business_root=a.business_root)
                if unit.get('status')!='PASS':raise ValueError('QUERY_UNIT_FAILURE')
            except ValueError:
                log=(probes/'worker.log').read_text(encoding='utf-8',errors='replace')
                if 'QUERY_' not in log:raise
                atomic_write(a.directory/'round.json',canonical({'verdict':'REJECTED','reason':'QUERY_UNIT_FAILED',
                    'candidate_id':child_id,'parent_id':parent['candidate_id'],'budget':budget.snapshot(),
                    'production_changed':False,'worker_log_sha256':bytes_hash((probes/'worker.log').read_bytes())}),exclusive=True)
                print('REJECTED: QUERY_UNIT_FAILED');return
            for index,f in enumerate(fixtures):
                guard()
                if check_split(f,expected_count=20)!=freezes[str(f.resolve())]:raise ValueError('FIXTURE_CHANGED_DURING_PROPOSAL')
                decision=replay_artifacts(capsule,f,{'A':parent,'B':child},a.directory/('suite-'+str(index+1)),
                    deadline=deadline,schema='rsi-query-aware-engineering/v1',scope='query-aware training engineering; not sealed verification')
                atomic_write(a.directory/('decision-'+str(index+1)+'.json'),canonical(decision),exclusive=True);decisions.append(decision)
                if not all(decision['gates']['G'+str(i)] is True for i in range(1,5)):break
            result=adjudicate_round([decisions[0]],all_qualified_terminal=True)
            if len(decisions)!=4 or any(not all(d['gates']['G'+str(i)] is True for i in range(1,5)) for d in decisions):
                result.update(verdict='REJECTED',adopted_candidate_id=None,reason='SOURCE_SUITE_HARD_GATE_FAILED')
            result.update(schema='rsi-query-aware-engineering/v1',candidate_id=child_id,parent_id=parent['candidate_id'],
                suite_decisions=decisions,suite_expected=4,independent_holdout=False,production_changed=False,budget=budget.snapshot())
            guard()
            atomic_write(a.directory/'round.json',canonical(result),exclusive=True)
            print(json.dumps({'verdict':result['verdict'],'candidate_id':child_id,'suite_complete':len(decisions),'budget':budget.snapshot()}))
        finally:capsule.close()
    verify_boundary()


if __name__=='__main__':main()
