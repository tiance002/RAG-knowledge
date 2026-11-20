from copy import deepcopy
import pytest

from rsik.autonomous import history_item, remember_round
from rsik.autonomy_contract import remember, project_search_memory, validate_memory_archive
from rsik.contracts import ContractError
from rsik.objectives import CONTRACT_VERSION


def profile(metric='context_chars', version=1, secondary=(), constraints=()):
    metrics = {metric, *secondary, *(item['metric_id'] for item in constraints)}
    return {'objective_profile_id': 'compact', 'version': version, 'description': 'Explicit owner goal',
            'primary_metric': metric, 'direction': 'max' if metric == 'shorter_cases' else 'min',
            'hard_constraints': list(constraints), 'secondary_metrics': list(secondary), 'tie_breakers': [],
            'required_metric_contracts': {key: CONTRACT_VERSION for key in metrics},
            'components': ['context_selection']}


def archive():
    return {'schema': 'rsi-autonomy-memory/v1', 'rounds': 0, 'evicted_entries': 0, 'entries': []}


def scope(objective=None, fingerprint='a' * 64, component='context_selection'):
    return dict(objective_profile=objective or profile(), compatibility_fingerprint=fingerprint, component=component)


def feedback(outcome):
    if outcome == 'REJECTED':
        return history_item(None, outcome, ['FACT_LOSS'], failure_origin='CANDIDATE')
    if outcome == 'NO_GAIN':
        return history_item(None, outcome, ['NO_GAIN'], quality_verdict='REJECTED', execution_status='COMPLETED')
    if outcome == 'PATCH_INVALID':
        return history_item(None, outcome, ['STATIC_GLOBAL_NAME'])
    return history_item(None, outcome, [outcome])


def outcomes(value):
    return {entry['history']['outcome'] for entry in value['entries']}


def test_objective_switch_keeps_applicable_failures_and_excludes_success_no_gain():
    memory = archive()
    for number, outcome in enumerate(('OBSERVED', 'VALIDATED', 'NO_GAIN', 'REJECTED', 'PATCH_INVALID'), 1):
        remember(memory, format(number, '064x'), number, feedback(outcome), **scope())
    frozen = deepcopy(memory)
    assert outcomes(project_search_memory(memory, **scope())) == {
        'OBSERVED', 'VALIDATED', 'NO_GAIN', 'REJECTED', 'PATCH_INVALID'}
    assert outcomes(project_search_memory(memory, **scope(profile('shorter_cases', 2)))) == {
        'REJECTED', 'PATCH_INVALID'}
    other_family = {**profile(), 'objective_profile_id': 'different-goal'}
    assert outcomes(project_search_memory(memory, **scope(other_family))) == {'REJECTED', 'PATCH_INVALID'}
    assert project_search_memory(memory, **scope(fingerprint='b' * 64))['entries'] == []
    assert project_search_memory(memory, **scope(component='retrieval'))['entries'] == []
    assert memory == frozen
    validate_memory_archive(memory)


@pytest.mark.parametrize('new_profile', [
    profile(version=2, secondary=['shorter_cases']),
    profile(version=2, constraints=[{'metric_id': 'necessary_fact_status', 'operator': '==', 'value': 'PASS'}]),
])
def test_secondary_metrics_and_constraints_keep_primary_scope(new_profile):
    memory = archive()
    remember(memory, 'c' * 64, 1, feedback('VALIDATED'), **scope())
    assert outcomes(project_search_memory(memory, **scope(new_profile))) == {'VALIDATED'}


def test_legacy_memory_is_preserved_without_inferred_cross_objective_scope():
    memory = archive()
    remember(memory, 'c' * 64, 1, feedback('VALIDATED'))
    original = deepcopy(memory)
    assert outcomes(project_search_memory(memory)) == {'VALIDATED'}
    assert project_search_memory(memory, **scope())['entries'] == []
    assert validate_memory_archive(memory) == original


def test_same_selector_preserves_objective_specific_outcomes_and_projection_privacy():
    memory = archive()
    remember(memory, 'c' * 64, 1, feedback('OBSERVED'), **scope())
    remember(memory, 'c' * 64, 2, feedback('NO_GAIN'), **scope(profile('shorter_cases', 2)))
    assert len(memory['entries']) == 2
    validate_memory_archive(memory)
    recent = [feedback('OBSERVED')]
    projection = project_search_memory(memory, recent=recent, **scope())
    assert projection['entries'][0]['history'] == {'recent_history_index': 0}
    assert set(projection['entries'][0]) == {'selector_sha256', 'first_round', 'last_round', 'attempts', 'history'}
    assert memory['entries'][0]['history']['outcome'] == 'OBSERVED'


def test_persisted_archive_unbounded_and_rejects_prompt_reference():
    memory = archive()
    for number in range(1, 20):
        remember(memory, format(number, '064x'), number, feedback('VALIDATED'), **scope())
    validate_memory_archive(memory)
    assert len(project_search_memory(memory, **scope())['entries']) == 8
    memory['entries'][0]['history'] = {'recent_history_index': 0}
    with pytest.raises(ContractError):
        validate_memory_archive(memory)


def test_infrastructure_unknown_parent_and_stop_do_not_become_strategy_rules():
    memory = archive()
    for number, origin in enumerate(('INFRASTRUCTURE', 'PARENT', 'UNKNOWN', 'GLOBAL_STOP'), 1):
        item = history_item(None, 'REJECTED', ['FACT_LOSS'], failure_origin=origin)
        remember(memory, format(number, '064x'), number, item, **scope())
    assert project_search_memory(memory, **scope(profile('shorter_cases', 2)))['entries'] == []

def test_autonomous_wires_owner_objective_and_hash_bound_prior_memory(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import rsik.autonomous as controller
    import rsik.query_workflow as workflow
    import rsik.selector_contract as structural
    import rsik.launch as admission
    from rsik.experiment import bytes_hash

    objective = profile('shorter_cases', 2)
    transition = {'current': objective, 'previous': profile(),
                  'reason': 'Explicit goal change', 'source_reference': 'owner-config:2'}
    memory = archive()
    remember(memory, 'd' * 64, 1, feedback('OBSERVED'), **scope())
    source = tmp_path / 'prior-search-memory.json'
    source.write_text(json.dumps(memory), encoding='utf-8')
    original_bytes = source.read_bytes()
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'objective_transition': transition,
                               'previous_search_memory': admission.file_binding(source)}), encoding='utf-8')
    launch = SimpleNamespace(plan_path=str(plan), plan_sha256=bytes_hash(plan.read_bytes()))
    monkeypatch.setattr(admission, 'validate_launch', lambda *args, **kwargs: None)
    directory = tmp_path / 'run'; directory.mkdir()
    registry = tmp_path / 'registry'; registry.mkdir()
    baseline = tmp_path / 'baseline'; baseline.mkdir()
    (baseline / 'round.json').write_text('{}')
    receipt = tmp_path / 'verified.json'
    receipt.write_text(json.dumps({'verdict': 'VERIFIED', 'candidate_id': 'baseline'}))
    worker = b'def select_member_indices(members, chunks, files, seed_ids, selection, query):\n    return []\n'
    complete = []
    calls = []

    class Budget:
        limit_nano = 1
        def __init__(self, *args): pass
        def snapshot(self):
            return {'unbounded_usage_requests': 0, 'pending_request_keys': [],
                    'recorded_cost_nano_upper': int(bool(complete))}

    class Log:
        def __init__(self, *args): pass
        def append(self, *args): pass
        def read(self):
            return [{'event_type': 'QUERY_SEALED_BUNDLE_FINISHED', 'payload': {
                'candidate_id': 'baseline', 'verdict': 'VERIFIED',
                'receipt_sha256': bytes_hash(receipt.read_bytes())}}]

    class Registry:
        def __init__(self, *args): pass
        def get(self, *args): return {'candidate_id': 'baseline'}

    def run_round(**kwargs):
        calls.append(kwargs)
        complete.append(True)
        return {'execution_state': 'COMPLETED', 'proposal_instances': {}, 'stop_reason': 'DIAGNOSIS_ABSTAIN'}

    monkeypatch.setattr(controller, 'Budget', Budget)
    monkeypatch.setattr(controller, 'EventLog', Log)
    monkeypatch.setattr(controller, 'QueryRegistry', Registry)
    monkeypatch.setattr(controller, 'OpenAIRoles', lambda *args, **kwargs: object())
    monkeypatch.setattr(controller, 'stop_guard', lambda *args: None)
    monkeypatch.setattr(controller, 'verify_training', lambda *args: None)
    monkeypatch.setattr(structural, 'structural_counts', lambda *args: {})
    monkeypatch.setattr(controller, 'frames', lambda *args: {
        'parent_id': 'baseline', 'worker': worker, 'config': b'{"selection":"all"}', 'baseline': {}})
    monkeypatch.setattr(workflow, 'run_query_round', run_round)
    result = controller.run({'directory': str(directory), 'query_registry': str(registry), 'fixtures': [],
        'budget_ledger': 'unused', 'baseline_round': str(baseline), 'baseline_id': 'baseline',
        'baseline_verification': str(receipt), 'freeze': 'unused', 'freeze_sha256': 'f' * 64,
        'key_file': 'unused', 'profile': 'single'}, launch=launch)
    assert result['stop_reason'] == 'BUDGET_EXHAUSTED'
    assert calls[0]['objective_transition'] == transition
    assert calls[0]['public']['search_memory']['entries'] == []
    saved = json.loads((directory / 'search-memory.json').read_text())
    assert saved['rounds'] == 2
    assert saved['entries'][0] == memory['entries'][0]
    assert saved['entries'][1]['objective_profile'] == objective
    assert source.read_bytes() == original_bytes
