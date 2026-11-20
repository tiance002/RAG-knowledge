"""Synthetic controller-boundary tests: no model, remote host or real fixture paths."""
from copy import deepcopy
from pathlib import Path

import pytest

from rsik import autonomous
from rsik.experiment import bytes_hash, load
from rsik.launch import LaunchContext
from rsik.state import canonical
from rsik.tests.test_objectives import profile


def synthetic_controller(tmp_path, monkeypatch, *, mode='all_eligible', tamper=None, legacy=False):
    import rsik.baseline_qualification as qualification
    import rsik.foundation as foundation
    import rsik.launch as launch_module
    import rsik.query_workflow as workflow
    import rsik.selector_contract as contract
    from rsik.execution_evidence import ExecutionEvidence

    root_id, first_id, second_id = ('a'*64, 'b'*64, 'c'*64)
    directory = tmp_path/'search'; directory.mkdir()
    root = tmp_path/'root'; root.mkdir()
    (root/'round.json').write_bytes(canonical({'candidate_id': root_id}))
    fixtures = [tmp_path/f'fixture-{i}' for i in range(4)]
    for fixture in fixtures:
        fixture.mkdir(); (fixture/'synthetic.json').write_bytes(b'{}')
    spec = {'schema':'rsi-query-parent-selection/v1', 'mode':mode, 'seed':7}
    objective = profile(constraints=[{'metric_id':'necessary_fact_status', 'operator':'==', 'value':'PASS'}])
    plan = {'experiment':{'allow_train_only_parents':True},
        'objective_transition':{'previous':None, 'current':objective,
                              'reason':'Synthetic controller coverage', 'source_reference':'synthetic-test'}}
    if not legacy:
        plan['experiment']['parent_selection'] = spec
    plan_path = tmp_path/'plan.json'; plan_path.write_bytes(canonical(plan))
    launch = LaunchContext(str(plan_path), bytes_hash(plan_path.read_bytes()), 'synthetic-run',
                           'AUTONOMOUS_SEARCH', str(tmp_path/'budget'), str(tmp_path/'registry'), str(tmp_path))
    strategy = {'profile':'single', 'role_profile':'query-k3-dependency/v1',
                'handbook_format':'OTHER', 'handbook_features':{}}
    if not legacy:
        strategy['parent_selection'] = spec
    strategy_path = tmp_path/'strategy.json'; strategy_path.write_bytes(canonical(strategy))
    receipt = tmp_path/'qualified.json'; receipt.write_bytes(canonical({'status':'BASELINE_QUALIFIED', 'candidate_id':root_id}))
    config = {'directory':str(directory), 'query_registry':str(tmp_path/'registry'),
        'fixtures':[str(f) for f in fixtures], 'budget_ledger':str(tmp_path/'budget'),
        'baseline_round':str(root), 'baseline_id':root_id, 'baseline_verification':str(receipt),
        'business_root':str(tmp_path), 'freeze':str(tmp_path/'freeze'), 'freeze_sha256':'synthetic',
        'strategy_config':str(strategy_path), 'strategy_config_sha256':bytes_hash(strategy_path.read_bytes()),
        'profile':'single', 'key_file':str(tmp_path/'unused'), 'host':'synthetic', 'identity':str(tmp_path/'unused')}
    worker = b'def select_member_indices(members, chunks, files, seed_ids, selection, query):\n    return []\n'
    candidates = {cid:{'candidate_id':cid, 'worker':worker + ('# '+cid+'\n').encode(),
                       'config':canonical({'selection':{}}), 'record':{}}
                  for cid in (root_id, first_id, second_id)}
    paths = {str(root.resolve()):root_id}
    quarantined = set(); role_parents = []; round_parents = []; launch_calls = []; verified = []

    class Registry:
        def __init__(self, *_): pass
        def get(self, cid): return deepcopy(candidates[cid])
        def require_not_quarantined(self, cid):
            if cid in quarantined: raise ValueError('QUERY_PARENT_QUARANTINED')

    class Budget:
        limit_nano = 4
        def __init__(self, *_): pass
        def snapshot(self):
            return {'unbounded_usage_requests':[], 'pending_request_keys':[],
                    'recorded_cost_nano_upper':len(round_parents)}

    def validate_launch(context, **kwargs):
        if kwargs.get('candidate'):
            cid = kwargs['candidate']['candidate_id']
            expected = None if cid == root_id else next(path for path, value in paths.items() if value == cid)
            assert context.exploration_training == expected
            launch_calls.append(cid)

    def frame(path, *_, **__):
        cid = paths[str(Path(path).resolve())]; candidate = candidates[cid]
        rows = [{'qid':'synthetic-q', 'chars_A':100, 'chars_B':{root_id:100, first_id:80, second_id:60}[cid],
            'pair_status':'PAIRED', 'pair_identity':True, 'execution':'COMPLETED',
            'upstream_hash':'synthetic-upstream', 'execution_environment_hash':'synthetic-environment',
            'parent_facts':{'fact':'PRESENT'}, 'candidate_facts':{'fact':'PRESENT'},
            'provenance_A':'PASS', 'provenance_B':'PASS'}]
        return {'parent_id':cid, 'worker':candidate['worker'], 'config':candidate['config'], 'entries':[],
            'suite_decisions':[{'schema_version':'rsi-candidate-decision/v2.2', 'eligible_count':1,
                                'per_question':deepcopy(rows)} for _ in range(4)],
            'baseline':{'1-1':{'suite':1,'capture':{'context':'synthetic', 'members':[]}, 'case':{'seed_ids':[]}}}}

    def roles(*args, **kwargs):
        trial = Path(args[1]).parent
        if not legacy:
            selection = load(trial/'parent-selection.json')
            baseline = load(trial/'baseline.json')
            assert selection['selected_candidate_id'] == baseline['parent_id']
            assert baseline['parent_selection_sha256'] == bytes_hash((trial/'parent-selection.json').read_bytes())
            role_parents.append(selection['selected_candidate_id'])
        return object()

    def run_round(**kwargs):
        number = len(round_parents)+1; round_parents.append(kwargs['parent']['candidate_id'])
        integrated = kwargs['directory']; integrated.mkdir()
        if number < 3:
            cid = first_id if number == 1 else second_id
            training = integrated/'selected'; training.mkdir()
            family = f'synthetic-family-{number}'
            candidates[cid]['record']['parent_id'] = kwargs['parent']['candidate_id']
            (training/'round.json').write_bytes(canonical({'adopted_candidate_id':cid, 'family_id':family}))
            (training/'family-decision.json').write_bytes(canonical({'family_id':family}))
            (integrated/'round.json').write_bytes(canonical({'launch_run_id':launch.run_id,
                'launch_plan_sha256':launch.plan_sha256, 'adopted_candidate_id':cid,
                'family_id':family, 'parent_id':kwargs['parent']['candidate_id']}))
            paths[str(training.resolve())] = cid
            if number == 2 and tamper:
                tamper(root, paths, candidates, quarantined)
            return {'adopted_candidate_id':cid, 'selected_training_directory':str(training), 'family_id':family}
        return {}

    monkeypatch.setattr(autonomous, 'Budget', Budget)
    monkeypatch.setattr(autonomous, 'QueryRegistry', Registry)
    monkeypatch.setattr(autonomous, 'frames', frame)
    monkeypatch.setattr(autonomous, 'OpenAIRoles', roles)
    monkeypatch.setattr(autonomous, 'stop_guard', lambda *_:None)
    monkeypatch.setattr(autonomous, 'verify_training', lambda *args, **_:verified.append(args[3]))
    monkeypatch.setattr(autonomous, 'make_visible', lambda *args, **kwargs:{})
    monkeypatch.setattr(qualification, 'verify_baseline_qualification', lambda **_:None)
    monkeypatch.setattr(ExecutionEvidence, 'real', classmethod(lambda cls, *_:None))
    monkeypatch.setattr(launch_module, 'validate_launch', validate_launch)
    monkeypatch.setattr(workflow, 'run_query_round', run_round)
    monkeypatch.setattr(foundation, 'check_split', lambda *args, **kwargs:{'synthetic':True})
    monkeypatch.setattr(contract, 'structural_counts', lambda *_:{})
    return config, launch, round_parents, role_parents, launch_calls, verified


def test_controller_restores_older_root_with_its_own_frame_before_roles(tmp_path, monkeypatch):
    config, launch, parents, roles, calls, verified = synthetic_controller(tmp_path, monkeypatch)
    result = autonomous.run(config, launch=launch)
    assert result['stop_reason'] == 'BUDGET_EXHAUSTED'
    assert parents == ['a'*64, 'b'*64, 'c'*64, 'a'*64]
    assert roles == [cid for cid in parents for _ in range(2)]
    assert verified == ['b'*64, 'c'*64]
    final = load(Path(config['directory'])/'round-000004'/'parent-selection.json')
    assert len(final['population']) == 3
    assert final['frame']['ordinary_round'] == str(Path(config['baseline_round']).resolve())
    assert final['frame']['arm'] == 'B'
    assert result['current_parent_quality'] == 'QUALIFIED_ROOT'
    assert {'a'*64,'b'*64,'c'*64} <= set(calls)


def test_archive_check_does_not_require_python312_junction_api(tmp_path, monkeypatch):
    monkeypatch.delattr(Path, 'is_junction', raising=False)
    path = tmp_path/'ordinary'; path.mkdir(); (path/'round.json').write_bytes(b'{}')
    assert autonomous.parent_archive_files(path) == [path/'round.json']


def test_system_freeze_surface_contains_scheduler():
    from rsik.query_workflow import implementation_hashes
    assert 'rsik/query_parent_pool.py' in implementation_hashes()


@pytest.mark.parametrize('tampering', ['archive', 'registry', 'quarantine', 'symlink'])
def test_controller_excludes_changed_alternative_and_keeps_eligible_parent(tmp_path, monkeypatch, tampering):
    def tamper(root, paths, candidates, quarantined):
        if tampering == 'archive': (root/'round.json').write_bytes(b'{}')
        elif tampering == 'registry': candidates['a'*64]['worker'] += b'# changed\n'
        elif tampering == 'quarantine': quarantined.add('a'*64)
        else:
            original = Path.is_symlink
            monkeypatch.setattr(Path, 'is_symlink', lambda self:self == root/'round.json' or original(self))
    config, launch, parents, _, _, _ = synthetic_controller(tmp_path, monkeypatch, tamper=tamper)
    autonomous.run(config, launch=launch)
    assert parents == ['a'*64, 'b'*64, 'c'*64, 'b'*64]
    receipt = load(Path(config['directory'])/'round-000003'/'parent-selection.json')
    assert receipt['exclusions'][0]['candidate_id'] == 'a'*64
    assert receipt['exclusions'][0]['reason'] in {'AUTONOMY_POOL_EVIDENCE_CHANGED','QUERY_PARENT_QUARANTINED',
                                                  'AUTONOMY_POOL_ARCHIVE_PATH_UNSAFE'}
    assert len(receipt['population']) == 3


def test_controller_legacy_keeps_latest_and_has_no_pool_artifacts(tmp_path, monkeypatch):
    config, launch, parents, _, _, _ = synthetic_controller(tmp_path, monkeypatch, legacy=True)
    autonomous.run(config, launch=launch)
    assert parents == ['a'*64,'b'*64,'c'*64,'c'*64]
    assert not list(Path(config['directory']).rglob('parent-selection*.json'))


def test_controller_no_eligible_parent_persists_terminal_selection_before_roles(tmp_path, monkeypatch):
    def tamper(root, paths, candidates, quarantined):
        # Admit round two first, then mark all parents unavailable at the next boundary.
        original = autonomous.checked_parent_frame
        def unavailable(registry, directory, fixtures, candidate_id):
            if len(paths) == 3 and candidate_id != 'c'*64:
                raise ValueError('QUERY_PARENT_QUARANTINED')
            result = original(registry, directory, fixtures, candidate_id)
            if candidate_id == 'c'*64:
                quarantined.update(candidates)
            return result
        monkeypatch.setattr(autonomous, 'checked_parent_frame', unavailable)
    config, launch, parents, roles, _, _ = synthetic_controller(tmp_path, monkeypatch, tamper=tamper)
    with pytest.raises(ValueError): autonomous.run(config, launch=launch)
    assert parents == ['a'*64, 'b'*64]
    assert len(roles) == 4
    receipt_path = Path(config['directory'])/'round-000003'/'parent-selection.json'
    receipt = load(receipt_path)
    assert receipt['selected_candidate_id'] is None
    assert len(receipt['population']) == len(receipt['exclusions']) == 3
    state = load(Path(config['directory'])/'state.json')
    assert state['execution_state'] == 'FAILED'
    assert state['parent_selection_sha256'] == bytes_hash(receipt_path.read_bytes())


@pytest.mark.parametrize('change', ['runtime', 'strategy', 'authorization'])
def test_controller_rejects_unfrozen_or_unapproved_selection_before_roles(tmp_path, monkeypatch, change):
    config, launch, _, roles, _, _ = synthetic_controller(tmp_path, monkeypatch)
    if change == 'runtime': config['parent_selection'] = {}
    elif change == 'strategy':
        strategy_path = Path(config['strategy_config']); strategy = load(strategy_path)
        strategy['parent_selection']['seed'] += 1; strategy_path.write_bytes(canonical(strategy))
        config['strategy_config_sha256'] = bytes_hash(strategy_path.read_bytes())
    else:
        from dataclasses import replace
        path = Path(launch.plan_path); plan = load(path)
        plan['experiment']['allow_train_only_parents'] = False; path.write_bytes(canonical(plan))
        launch = replace(launch, plan_sha256=bytes_hash(path.read_bytes()))
    with pytest.raises(ValueError): autonomous.run(config, launch=launch)
    assert roles == []
