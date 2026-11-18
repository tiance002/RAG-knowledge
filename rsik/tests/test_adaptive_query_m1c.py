"""Full synthetic Adaptive round and read-only completed checkpoint recovery."""
import json

import pytest

from rsik import query_workflow as q
from rsik.adjudicate import SLOTS, evaluate_candidate
from rsik.budgets import Budget
from rsik.collaboration import _ScriptedRoles
from rsik.execution_evidence import ExecutionEvidence
from rsik.handbook.store import Handbook
from rsik.openai_roles import runtime_profile_identity
from rsik.query_aware import QueryRegistry
from rsik.query_learning import ExperienceSession
from rsik.redact import LeakScanner
from rsik.role_profiles import M1_ADAPTIVE_V1, roles_for
from rsik.state import canonical, digest
from rsik.tests.test_adaptive_runtime_m1c import diagnosis
from rsik.tests.test_collaboration_pair import Capsule, fixture


def test_adaptive_query_coordinator_and_checkpoint(tmp_path, monkeypatch):
    args, old_script, replays, seen, _ = fixture(tmp_path, monkeypatch)
    profile = M1_ADAPTIVE_V1
    budget = Budget(tmp_path/'test-budget.sqlite', limit_usd='0.20')
    registry = QueryRegistry(tmp_path/'registry')
    parent = registry.get(registry.put(args['worker'], args['config'], parent_id=None,
                                      origin='SYNTHETIC_CONTROL', evidence={}))
    scanner = LeakScanner()
    book = Handbook(tmp_path/'memory', scanner=scanner, evidence_mode='SYNTHETIC_CONTROL')
    session = ExperienceSession(tmp_path/'learning', book)
    reverse = {role:slot for slot,role in roles_for(profile).items()}
    received = {}
    def script(role, context, body):
        if role == 'diagnose':
            return {'status':'OK', 'result':diagnosis(context['public']), 'reason':None}
        if role in reverse:
            received[role] = context['assignment']
            assert context['assignment']['slot'] == reverse[role]
            assert 'dispatch_plan' not in context
        return old_script(profile, reverse.get(role, role), context, body)
    seam = 'query-autonomous-v1'
    models = {role:'gpt-6-luna' for role in ('diagnose','curator','meta',*reverse)}
    identity = runtime_profile_identity(profile, models, seam)
    def client(name):
        return _ScriptedRoles(budget, tmp_path/name, scanner, script, role_profile=profile,
                              seam_profile=seam, models=models, runtime_identity=identity)
    roles, advisory = client('roles'), client('advisory')
    root = tmp_path/'round'
    evidence = ExecutionEvidence.synthetic_control()
    result = q.run_query_round(public=args['public'], roles=roles, advisory_roles=advisory,
        parent=parent, fixtures=args['fixtures'], registry=registry, directory=root,
        capsule_factory=lambda _: Capsule(), scanner=scanner, handbook=book,
        experience_session=session, compatibility_fingerprint=digest('synthetic-m1c'),
        role_profile=profile, runtime_identity=identity, evidence=evidence)
    assert result['qualified'] == result['attempted_replays'] == list(SLOTS)
    assert result['family_size'] == 3 and len(replays) == 12
    assert set(received) == set(roles_for(profile).values())
    assert {entry['task_type'] for entry in received.values()} == {'BUDGET'}
    assert budget.snapshot()['attempts'] == budget.snapshot()['completed'] == 6
    assert result['evidence_mode'] == 'SYNTHETIC_CONTROL' and not result['real_eligible']
    assert {slot for _,slot in seen} == {*SLOTS,'curator','meta'}
    events = roles.observer.log.read()
    assert sum(e['event_type']=='DISPATCH_SLOT_ASSIGNED' for e in events) == 3

    def audited(run, fixture, candidate, proposal, worker, config, *, evidence):
        assert not evidence.real_eligible
        expected_rows = json.loads((fixture/'synthetic.json').read_bytes())['rows']
        rows = json.loads((run/'results.json').read_bytes())['rows']
        assert rows == expected_rows
        item = evaluate_candidate({**proposal, 'proposal_id':'synthetic', 'candidate_id':candidate,
                                   'worker_hash':worker, 'config_hash':config},
                                  rows, [r['qid'] for r in rows], total_questions=20)
        return {**item, 'evidence_mode':'SYNTHETIC_CONTROL', 'real_eligible':False}

    monkeypatch.setattr(q, 'read_decision', audited)
    recovered = q.resume_query_round(directory=root, registry=registry, fixtures=args['fixtures'], evidence=evidence)
    assert recovered == result and roles.observer.log.read() == events
    assert budget.snapshot()['attempts'] == 6 and len(replays) == 12
    path = root/'dispatch-plan.json'
    tampered = json.loads(path.read_bytes())
    tampered['assignments'][0]['task_type'] = 'GENERALIST'
    path.write_bytes(canonical(tampered))
    with pytest.raises(ValueError, match='QUERY_COMPLETED_ARTIFACT_CHANGED'):
        q.resume_query_round(directory=root, registry=registry, fixtures=args['fixtures'], evidence=evidence)
