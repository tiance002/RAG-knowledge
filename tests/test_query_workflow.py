"""Synthetic coordinator tests; no model, network, business source or containers."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsik import query_workflow as q
from rsik.adjudicate import SLOTS, evaluate_candidate
from rsik.errors import StageFailure
from rsik.redact import LeakScanner
from rsik.state import canonical
from rsik.tests.test_workflow import FakeRoles, fake_rows, public_context


class Registry:
    def __init__(self):
        self.parent = {'candidate_id': 'parent', 'worker': b'parent', 'config': b'{}'}
        self.children = {}

    def require_not_quarantined(self, cid):
        assert cid == 'parent'

    def get(self, cid):
        return self.parent if cid == 'parent' else self.children[cid]

    def put(self, worker, config, **kwargs):
        cid = kwargs['evidence']['slot']
        self.children[cid] = {'candidate_id': cid, 'worker': worker, 'config': config, 'record': {'parent_id': 'parent'}}
        return cid


class Capsule:
    business_root = 'SYNTHETIC'
    def setup(self): pass
    def close(self): self.closed = True
    def child(self, stage, args, directory, **kwargs):
        Path(args[-1]).write_bytes(canonical({'status': 'PASS'}))
        return {'status': 'PASS'}


class Advisory:
    scanner = LeakScanner()
    def __init__(self): self.calls = []
    def call(self, role, public):
        assert 'selector_contract' not in public and 'experiment_intent' not in public
        self.calls.append(role)
        return {'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'}


def setup(monkeypatch, tmp_path, *, p3_change=False, failed_slot=None):
    registry, advisory = Registry(), Advisory()
    monkeypatch.setattr(q, 'check_split', lambda *a, **k: {'frozen': True})
    monkeypatch.setattr(q, 'apply_patch', lambda *a: b'changed')
    monkeypatch.setattr(q, 'selector', lambda *a: (None, 'SYNTHETIC public selector'))
    monkeypatch.setattr(q, 'invariant_probe_source', lambda worker: b'SYNTHETIC probe')
    calls = []
    def replay(capsule, fixture, artifacts, output, **kwargs):
        slot = artifacts['B']['candidate_id']; calls.append((slot, str(fixture)))
        if slot == failed_slot:
            raise StageFailure('REPLAY_ERROR', 'SYNTHETIC_INFRA_FAILURE')
        output.mkdir()
        (output / 'manifest.json').write_bytes(canonical({'arms': {
            arm: {'candidate_id': artifact['candidate_id'], 'worker_sha256': q.bytes_hash(artifact['worker']),
                  'sha256': q.bytes_hash(artifact['config'])} for arm, artifact in artifacts.items()}}))
        (output / 'results.json').write_bytes(canonical({'rows': [{'status': 'PAIRED'}]}))
        for arm in ('A', 'B'):
            path = output / 'case-001' / arm
            path.mkdir(parents=True)
            (path / 'capture.json').write_bytes(canonical({'members': ['one'] if arm == 'A' or not p3_change else ['two']}))
        rows = fake_rows()
        return evaluate_candidate({'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1',
            'candidate_id': slot, 'worker_hash': 'same', 'config_hash': 'same'}, rows,
            [r['qid'] for r in rows], total_questions=20)
    monkeypatch.setattr(q, 'replay_artifacts', replay)
    kwargs = dict(public=public_context(), roles=FakeRoles(same_patch=True, barrier=True), advisory_roles=advisory,
        parent=registry.parent, fixtures=[tmp_path / str(i) for i in range(4)], registry=registry,
        directory=tmp_path / 'round', scanner=LeakScanner(), capsule_factory=lambda output: Capsule(),
        evidence=__import__('rsik.execution_evidence',fromlist=['ExecutionEvidence']).ExecutionEvidence.synthetic_control())
    return kwargs, calls, advisory


def test_all_instances_ordinary_replayed_and_advisory_follows_archive(monkeypatch, tmp_path):
    kwargs, calls, advisory = setup(monkeypatch, tmp_path)
    before = deepcopy(kwargs['public'])
    result = q.run_query_round(**kwargs)
    assert len(calls) == 12 and {s for s, _ in calls} == set(SLOTS)
    assert result['family_size'] == 3 and result['adopted_proposal_id'] == 'SYNTHETIC-P1_DEDUP'
    assert len(result['candidate_decisions']) == 3
    assert result['static_proposal_instances'] == 3 and result['ast_unique_strategy_count'] == 1
    assert Path(result['selected_training_directory'], 'round.json').exists()
    assert advisory.calls == ['curator', 'meta'] and kwargs['public'] == before


def test_order_role_cannot_change_selected_members(monkeypatch, tmp_path):
    kwargs, calls, advisory = setup(monkeypatch, tmp_path, p3_change=True)
    result = q.run_query_round(**kwargs)
    p3 = next(d for d in result['candidate_decisions'] if d['angle'] == 'P3_ORDER')
    assert p3['reason_codes'] == ['ANGLE_VIOLATION']
    assert result['adopted_candidate_id'] != 'P3_ORDER'
    assert advisory.calls == ['curator', 'meta']


def test_local_replay_failure_does_not_skip_other_instances(monkeypatch, tmp_path):
    kwargs, calls, _ = setup(monkeypatch, tmp_path, failed_slot='P2_BUDGET')
    result = q.run_query_round(**kwargs)
    assert result['attempted_replays'] == list(SLOTS)
    assert any(s == 'P3_ORDER' for s, _ in calls)
    assert next(d for d in result['candidate_decisions'] if d['angle'] == 'P2_BUDGET')['verdict'] == 'INCONCLUSIVE'


def test_single_profile_retains_fixed_three_comparison_slots(monkeypatch, tmp_path):
    kwargs, calls, _ = setup(monkeypatch, tmp_path)
    kwargs.update(roles=FakeRoles(same_patch=True), enabled_slots=SLOTS[:1])
    result = q.run_query_round(**kwargs)
    assert len(calls) == 4 and result['family_size'] == 3
    assert len(result['candidate_decisions']) == 3 and result['enabled_slots'] == list(SLOTS[:1])


def test_compatible_handbook_sections_retrieved_without_mutating_public(monkeypatch, tmp_path):
    from rsik import query_learning
    # Retrieval-only synthetic control; the real event bridge has separate end-to-end tests.
    monkeypatch.setattr(query_learning, 'record_query_round_learning', lambda **kw: None)
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    sections = []
    class Handbook:
        def retrieve(self, section, format, features, fingerprint):
            sections.append((section, fingerprint))
            return []
    kwargs.update(handbook=Handbook(), compatibility_fingerprint='SYNTHETIC-frozen-profile')
    q.run_query_round(**kwargs)
    assert sections == [(s, 'SYNTHETIC-frozen-profile') for s in ('dedup', 'budget', 'order', 'format')]


def test_registry_failure_is_global_and_cannot_select_partial_winner(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    def broken(*a, **k): raise ValueError('SYNTHETIC corrupt registry')
    kwargs['registry'].put = broken
    result = q.run_query_round(**kwargs)
    assert result['verdict'] == 'ROUND_ABORTED' and result['adopted_candidate_id'] is None


def test_curator_local_failure_does_not_skip_meta_or_rewrite_decision(monkeypatch, tmp_path):
    kwargs, _, advisory = setup(monkeypatch, tmp_path)
    original = advisory.call
    def call(role, public):
        if role == 'curator':
            advisory.calls.append(role)
            raise StageFailure('AGENT_ERROR', 'AGENT_UNAVAILABLE')
        return original(role, public)
    advisory.call = call
    result = q.run_query_round(**kwargs)
    assert result['verdict'] == 'ADOPT_CANDIDATE' and advisory.calls == ['curator', 'meta']
    assert q.load(kwargs['directory'] / 'advisory-summary.json')['role_failures'] == [
        {'role': 'curator', 'reason_code': 'AGENT_UNAVAILABLE'}]


def test_matched_comparison_normalizes_aliases_but_preserves_counts_and_code():
    a, b = public_context(), public_context()
    assert a != b and q.public_semantics(a) == q.public_semantics(b)
    b['observations'][0]['counts']['questions'] += 1
    assert q.public_semantics(a) != q.public_semantics(b)


def test_original_budget_path_required_and_cannot_create_fresh_ledger(tmp_path):
    query_registry = tmp_path / 'query-aware-v1' / 'registry'
    expected = tmp_path / 'task-model-budget.sqlite'
    with pytest.raises(ValueError, match='ORIGINAL_TASK_BUDGET'):
        q.bind_budget_path(query_registry, expected)
    expected.write_bytes(b'SYNTHETIC existing ledger')
    assert q.bind_budget_path(query_registry, expected) == expected
    with pytest.raises(ValueError, match='ORIGINAL_TASK_BUDGET'):
        q.bind_budget_path(query_registry, tmp_path / 'new-budget.sqlite')


def test_completed_recovery_recomputes_without_roles_or_capsules_and_rejects_tamper(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    result = q.run_query_round(**kwargs)
    monkeypatch.setattr(q, 'read_only_quarantine_check', lambda *a: None)
    def read(run, fixture, cid, proposal, worker, config, **kwargs):
        index = int(run.name.split('-')[1])
        return q.load(run.parent / f'decision-{index}.json')
    monkeypatch.setattr(q, 'read_decision', read)
    before = {p: p.read_bytes() for p in kwargs['directory'].rglob('*') if p.is_file()}
    recovered = q.resume_query_round(directory=kwargs['directory'], registry=kwargs['registry'], fixtures=kwargs['fixtures'])
    assert recovered == result and before == {p: p.read_bytes() for p in before}
    (kwargs['directory'] / 'public-context.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='ARTIFACT_CHANGED'):
        q.resume_query_round(directory=kwargs['directory'], registry=kwargs['registry'], fixtures=kwargs['fixtures'])


def test_partial_round_recovery_fails_closed_without_restarting(tmp_path):
    with pytest.raises(FileNotFoundError):
        q.resume_query_round(directory=tmp_path, registry=Registry(), fixtures=[])


def test_full_holm_family_survives_coordinator_archive_recovery_and_training(monkeypatch, tmp_path):
    """Review F2 counterexample; synthetic callbacks, real archival entry points."""
    from rsik import query_verification as verification
    from rsik.adjudicate import adjudicate_round
    from rsik.family_decision import validate_family
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    kwargs['roles'] = FakeRoles(abstain='P3_ORDER')
    monkeypatch.setattr(q, 'apply_patch', lambda worker, patch: ('# ' + patch['new']).encode())
    ordinary = q.replay_artifacts
    def counterexample(capsule, fixture, artifacts, output, **options):
        result = ordinary(capsule, fixture, artifacts, output, **options)
        slot = artifacts['B']['candidate_id']
        deltas = [-1] * 9 + [0] * 11 if slot == 'P1_DEDUP' else [-2] * 8 + [1] + [0] * 11
        rows = fake_rows()
        for row, delta in zip(rows, deltas, strict=True):
            row['chars_B'] = row['chars_A'] + delta
        result['worker_hash'] = q.bytes_hash(artifacts['B']['worker'])
        return evaluate_candidate({k: result[k] for k in ('angle', 'proposal_id', 'candidate_id', 'worker_hash', 'config_hash')},
                                  rows, [r['qid'] for r in rows], total_questions=20)
    monkeypatch.setattr(q, 'replay_artifacts', counterexample)
    result = q.run_query_round(**kwargs)
    selected = Path(result['selected_training_directory'])
    family = q.load(kwargs['directory'] / 'family-decision.json')
    item = next(d for d in result['candidate_decisions'] if d['angle'] == 'P2_BUDGET')
    assert item['sign_p_adjusted']['value'] == .078125
    assert result['adopted_candidate_id'] == 'P2_BUDGET'
    assert q.load(selected / 'round.json')['verdict'] == 'ADOPT_CANDIDATE'
    assert q.load(selected / 'round.json')['candidate_decisions'] == result['candidate_decisions']
    assert validate_family(family)['adopted_candidate_id'] == 'P2_BUDGET'
    primary = q.load(selected / 'decision-1.json')
    assert adjudicate_round([primary])['verdict'] == 'NO_ADOPTION'  # original bug, not the archival operation
    def read(run, fixture, cid, proposal, worker, config, **options):
        return q.load(run.parent / ('decision-' + run.name.split('-')[1] + '.json'))
    monkeypatch.setattr(q, 'read_decision', read)
    monkeypatch.setattr(q, 'read_only_quarantine_check', lambda *a: None)
    registry = kwargs['registry']
    registry.require_not_quarantined = lambda *a: None
    for child in registry.children.values():
        child['record'].update(worker_sha256=q.bytes_hash(child['worker']), config_sha256=q.bytes_hash(child['config']))
    assert q.resume_query_round(directory=kwargs['directory'],registry=registry,fixtures=kwargs['fixtures']) == result
    monkeypatch.setattr(verification, 'QueryRegistry', lambda _: registry)
    monkeypatch.setattr(verification, 'read_decision', read)
    monkeypatch.setattr(verification, 'check_split', q.check_split)
    monkeypatch.setattr(verification, 'invariant_probe_source', q.invariant_probe_source)
    candidate, parent = verification.verify_training(registry, selected, kwargs['fixtures'], 'P2_BUDGET')
    assert candidate['candidate_id'] == 'P2_BUDGET' and parent['candidate_id'] == 'parent'
    # Family content is checked even if an attacker re-hashes the completion file.
    family['outcome']['candidate_decisions'][1]['sign_p_adjusted']['value'] = .1171875
    (selected / 'family-decision.json').write_bytes(canonical(family))
    with pytest.raises(ValueError, match='FAMILY_DECISION_CHANGED'):
        verification.verify_training(registry, selected, kwargs['fixtures'], 'P2_BUDGET')


def test_family_requires_all_slots_and_rejects_duplicate_candidate_identity():
    from rsik.family_decision import make_family
    from rsik.workflow import placeholder
    decisions = [placeholder(s, s, 'ABSTAIN') for s in SLOTS]
    assert make_family(decisions)['outcome']['verdict'] == 'NO_ADOPTION'
    with pytest.raises(ValueError, match='EXACT_THREE_SLOTS'):
        make_family(decisions[:2])
    with pytest.raises(ValueError, match='EXACT_THREE_SLOTS'):
        make_family([decisions[0], decisions[0], decisions[2]])
    decisions[0]['candidate_id'] = decisions[1]['candidate_id'] = 'same-candidate'
    with pytest.raises(ValueError, match='DUPLICATE_CANDIDATE'):
        make_family(decisions)


def test_recovery_rejects_raw_actual_role_even_if_artifact_hashes_are_resealed(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    q.run_query_round(**kwargs)
    directory = kwargs['directory']
    record = q.load(directory / 'round.json')
    record['raw_role_proposals']['P1_DEDUP']['angle'] = 'P3_ORDER'
    (directory / 'round.json').write_bytes(canonical(record))
    completion = q.load(directory / 'completion.json')
    completion['files']['round.json'] = q.bytes_hash((directory / 'round.json').read_bytes())
    (directory / 'completion.json').write_bytes(canonical(completion))
    monkeypatch.setattr(q, 'read_only_quarantine_check', lambda *a: None)
    from rsik.contracts import ContractError
    with pytest.raises((ValueError, ContractError)):
        q.resume_query_round(directory=directory, registry=kwargs['registry'], fixtures=kwargs['fixtures'])


def test_experience_session_checks_frontier_before_any_new_role_request(monkeypatch, tmp_path):
    from rsik.handbook.store import Handbook
    from rsik.query_learning import ExperienceSession
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    book = Handbook(tmp_path / 'book', evidence_mode='SYNTHETIC_CONTROL')
    session = ExperienceSession(tmp_path / 'experience', book)
    kwargs.update(handbook=book,experience_session=session,compatibility_fingerprint='synthetic-compatible')
    result = q.run_query_round(**kwargs)
    assert session.verify()['evidence_mode'] == 'SYNTHETIC_CONTROL'
    assert (session.directory / ('learning-' + q.digest(result) + '.json')).is_file()
    book.log.append('external', 'EXTERNAL_TEST_INJECTION', {})
    kwargs['directory'] = tmp_path / 'must-not-start'
    kwargs['roles'].diagnose = lambda *a: pytest.fail('no request after external experience injection')
    with pytest.raises(ValueError,match='EXTERNAL_EXPERIENCE_CHANGE'):
        q.run_query_round(**kwargs)
    assert not kwargs['directory'].exists()


def test_versioned_dependency_role_preserves_original_role_and_fixed_slot_identity(monkeypatch, tmp_path):
    from rsik.role_profiles import DEPENDENCY
    kwargs, calls, _ = setup(monkeypatch, tmp_path, p3_change=True)
    class DependencyRoles(FakeRoles):
        role_profile = DEPENDENCY
        def propose(self, role, public, diagnosis):
            statistical_slot = 'P3_ORDER' if role == 'P3_DEPENDENCY' else role
            response = super().propose(statistical_slot, public, diagnosis)
            response['result']['angle'] = role
            return response
    kwargs.update(roles=DependencyRoles(same_patch=True, barrier=True),role_profile=DEPENDENCY)
    result = q.run_query_round(**kwargs)
    assert len(calls) == 12  # all three instances independently replay all four suites
    assert result['proposal_roles']['P3_ORDER'] == 'P3_DEPENDENCY'
    assert result['raw_role_proposals']['P3_ORDER']['angle'] == 'P3_DEPENDENCY'
    assert result['proposal_instances']['P3_ORDER']['angle'] == 'P3_ORDER'  # explicit host normalization
    assert next(d for d in result['candidate_decisions'] if d['angle'] == 'P3_ORDER')['reason_codes'] != ['ANGLE_VIOLATION']
    family = q.load(kwargs['directory'] / 'family-decision.json')
    assert family['role_profile']['version'] == DEPENDENCY
    assert family['strategy_binding']['role_profile'] == result['role_profile']
    assert 'P3_DEPENDENCY' in family['strategy_binding']['models']


@pytest.mark.parametrize('outcome',['regression','inconclusive'])
def test_sealed_failure_learning_never_calls_curator_and_internal_quarantine_keeps_session(monkeypatch,tmp_path,outcome):
    """Synthetic leaf execution; exercise the actual sealed workflow transaction."""
    from rsik import query_verification as verification
    from rsik.execution_evidence import ExecutionEvidence
    from rsik.handbook.store import Handbook
    from rsik.query_learning import ExperienceSession
    from rsik.tests.test_handbook_and_promotion import entry,delta,evidence_context
    book=Handbook(tmp_path/'book',evidence_mode='SYNTHETIC_CONTROL')
    seed=book.commit_delta(delta(entry()),evidence_context(True),idempotency_key='SYNTHETIC-seed')
    session=ExperienceSession(tmp_path/'experience',book)
    candidate={'candidate_id':'child','worker':b'SYNTHETIC child','config':b'{}'}
    parent={'candidate_id':'parent','worker':b'SYNTHETIC parent','config':b'{}'}
    quarantines=[]
    class FakeRegistry:
        def require_not_quarantined(self,*a):pass
        def get(self,cid):return parent if cid=='parent' else candidate
        def quarantine(self,cid,path,reason):
            assert (session.directory/'learning-intent.json').exists()
            quarantines.append(cid)
            book.log.append('synthetic-linked-regression','ENTRY_QUARANTINED',
                {'entry_id':seed,'candidate_id':cid,'reason':reason,'evidence_mode':'SYNTHETIC_CONTROL'})
    monkeypatch.setattr(verification,'QueryRegistry',lambda *a:FakeRegistry())
    monkeypatch.setattr(verification,'verify_training',lambda *a,**k:(candidate,parent))
    monkeypatch.setattr(verification,'source_separation',lambda *a:{'collisions':0})
    freeze={'SYNTHETIC_CONTROL':True}
    monkeypatch.setattr(verification,'check_split',lambda *a,**k:freeze)
    fixtures=[]
    for i in range(3):
        fixture=tmp_path/f'held-{i}';fixture.mkdir();fixtures.append(fixture)
        (fixture/'cases.json').write_bytes(canonical([{}]*20))
        (fixture/'freeze.json').write_bytes(canonical(freeze))
        (fixture/'sources.manifest.json').write_bytes(canonical([{'source_sha256':format(j,'064x')} for j in range(20)]))
    training=tmp_path/'training';training.mkdir();(training/'round.json').write_bytes(b'{}')
    def replay(*a,**k):
        rows=fake_rows()
        if outcome=='regression':rows[0]['candidate_facts']['fact']='ABSENT'
        else:rows[0].update(execution='FAILED',fault_origin='INFRA')
        return evaluate_candidate({'angle':'P2_BUDGET','proposal_id':'control','candidate_id':'child'},rows,
                                  [r['qid'] for r in rows],total_questions=20)
    monkeypatch.setattr(verification,'replay_artifacts',replay)
    class ControlCapsule:
        evidence_policy=ExecutionEvidence.synthetic_control()
        def setup(self):pass
        def close(self):pass
    class NoCurator:
        def call(self,*a):pytest.fail('nonverified evidence must never invoke Curator')
    output=tmp_path/'verification'
    result=verification.verify_sealed(registry=tmp_path/'registry',training=training,
        train_fixtures=[tmp_path/'UNOPENED']*4,sealed_fixtures=fixtures,used_holdout=tmp_path/'UNOPENED',
        candidate_id='child',trusted_id='parent',capsule=ControlCapsule(),output=output,
        handbook=book,advisory_roles=NoCurator(),compatibility_fingerprint='synthetic-compatible',experience_session=session)
    expected='REGRESSION_SUSPECTED' if outcome=='regression' else 'HOLDOUT_INCONCLUSIVE'
    assert result['verdict']==expected
    assert session.verify()['sequence']==len(book.log.read())
    assert quarantines==(['child'] if outcome=='regression' else [])
    assert book.view()['states'][seed]==('QUARANTINED' if outcome=='regression' else 'ACTIVE')
    learning=q.load(output/'learning-summary.json')
    assert learning['harmful_credit'] is False
    assert learning['status']==('HOLDOUT_FAILED' if outcome=='regression' else 'HOLDOUT_INCONCLUSIVE')

def test_query_round_single_observation_database(monkeypatch, tmp_path):
    import json
    from rsik.budgets import Budget
    from rsik.handbook.store import Handbook
    from rsik.lineage import LineagePolicy, ParentPool
    from rsik.observability import Observer
    from rsik.openai_roles import OpenAIRoles, TASKS
    from rsik.query_learning import ExperienceSession
    from rsik.state import StageJournal
    from rsik.tests.test_invocation_observability import receipt

    kwargs, replays, _ = setup(monkeypatch, tmp_path)
    fake = FakeRoles(same_patch=True)
    def transport(body, *_):
        role = next(k for k, task in TASKS.items() if body['input'][0]['content'].endswith(task))
        context = json.loads(body['input'][1]['content'])
        if role == 'diagnose':
            answer = fake.diagnose(context['public'])
        elif role in SLOTS:
            answer = fake.propose(role, context['public'], context['diagnosis'])
        else:
            answer = {'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'}
        return receipt(answer)

    budget = Budget(tmp_path / 'test-budget.sqlite')
    observer = Observer(tmp_path / 'run-observations' / 'observations.sqlite')
    book = Handbook(tmp_path / 'book')
    session = ExperienceSession(tmp_path / 'learning', book)
    for key in ('roles', 'advisory_roles'):
        kwargs[key] = OpenAIRoles(budget, tmp_path / key, tmp_path / 'unused.env',
                                  kwargs['scanner'], transport=transport)
    kwargs.update(handbook=book, experience_session=session, compatibility_fingerprint='a' * 64,
                  observer=observer)
    # This coordinator consumes an already chosen parent. Exercise the separate
    # selection entrypoint with the same run observer, including its empty return.
    pool = ParentPool(tmp_path / 'pool', evidence_mode='SYNTHETIC_CONTROL')
    assert pool.select(LineagePolicy(tmp_path / 'policy'), observer=observer,
                       round_id=kwargs['public']['round_id']) is None
    journal_calls = []
    execute = StageJournal.execute
    def capture(journal, stage, inputs, action, reconcile, **options):
        journal_calls.append((journal, stage, inputs, reconcile))
        return execute(journal, stage, inputs, action, reconcile, **options)
    monkeypatch.setattr(StageJournal, 'execute', capture)
    result = q.run_query_round(**kwargs)
    assert result['family_size'] == 3 and len(replays) == 12
    events = observer.log.read()
    assert sum(e['event_type'] == 'INVOCATION_FINISHED' for e in events) == 6
    assert budget.snapshot()['completed'] == budget.snapshot()['attempts'] == 6
    kinds = {e['event_type'] for e in events}
    assert {'PARENT_SELECTION_COMPLETED', 'PROPOSAL_STATIC_PASSED',
            'MEMORY_RETRIEVAL_COMPLETED', 'STAGE_STARTED', 'STAGE_COMPLETED',
            'EXECUTION_STARTED'} <= kinds
    assert sum(e['event_type'] == 'STAGE_COMPLETED' and
               e['payload']['stage_id'] == 'LEARNING_COMMIT' for e in events) == 1
    assert not any(kind.startswith('MODEL_REQUEST_') for kind in kinds)
    assert list(tmp_path.rglob('observations.sqlite')) == [observer.path]
    # Reconcile an existing stage through the same journal and checkpoint.
    journal, stage, inputs, reconcile = journal_calls[0]
    assert journal.directory == kwargs['directory'] / 'stages'
    assert journal._path(stage, 'checkpoint').is_file()
    # The synthetic registry uses slot aliases instead of production candidate IDs.
    # Its original reconciliation callback therefore refuses that identity.
    no_action = lambda _: pytest.fail('duplicate action')
    with pytest.raises(RuntimeError, match='committed side effect no longer matches'):
        execute(journal, stage, inputs, no_action, reconcile)
    failed = observer.log.read()[len(events):]
    assert failed[-1]['event_type'] == 'RECOVERY_BLOCKED'
    assert all(e['event_type'] != 'EXECUTION_STARTED' for e in failed)
    # Independently look up the registered child in this fixture by its slot.
    slot = stage.removeprefix('registry-')
    child = kwargs['registry'].children[slot]
    existing = {'candidate_id': child['candidate_id']}
    events = observer.log.read()
    _, recovered = execute(journal, stage, inputs, no_action,
                           lambda _: {'status': 'MATCHED', 'output': existing})
    assert recovered
    tail = observer.log.read()[len(events):]
    assert [e['event_type'] for e in tail] == [
        'STAGE_STARTED', 'RECOVERY_STARTED', 'STAGE_COMPLETED', 'RECOVERY_COMPLETED']
    assert {e['payload']['attempt'] for e in tail} == {3}
    assert len({e['payload']['operation_id'] for e in tail}) == 1
    assert list(tmp_path.rglob('observations.sqlite')) == [observer.path]
