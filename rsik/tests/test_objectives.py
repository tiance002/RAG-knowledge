"""Offline coverage of objective authority, freezing and Token attribution."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from rsik.analytics.objective_usage import objective_usage
from rsik.autonomy_contract import project_search_memory, remember
from rsik.autonomous import history_item
from rsik.budgets import Budget
from rsik.objectives import CONTRACT_VERSION, freeze_objective, profile_change, prompt_objective
from rsik.openai_roles import OpenAIRoles
from rsik.redact import LeakScanner
from rsik.tests.test_autonomy_boundary import public
from rsik.tests.test_openai_roles import response


def profile(primary='context_chars', version=1, *, secondary=None, constraints=None):
    secondary, constraints = secondary or [], constraints or []
    metrics = {primary, *secondary, *(row['metric_id'] for row in constraints)}
    return {'objective_profile_id': 'owner-dev', 'version': version, 'description': 'Explicit objective',
            'primary_metric': primary, 'direction': 'max' if primary == 'shorter_cases' else 'min',
            'secondary_metrics': secondary, 'tie_breakers': [], 'hard_constraints': constraints,
            'required_metric_contracts': {metric: CONTRACT_VERSION for metric in sorted(metrics)},
            'components': ['context_selection']}


def test_trusted_absolute_constraint_value_takes_precedence_over_direction():
    from rsik.objectives import constraint_evidence_status

    bound = {'metric_id': 'shorter_cases', 'operator': '>=', 'value': 5}
    assert constraint_evidence_status(bound, 'REGRESSED') == 'CONSTRAINT_RISK'
    assert constraint_evidence_status(bound, 'REGRESSED', measured_value=6) == 'PASS'
    assert constraint_evidence_status(bound, 'IMPROVED', measured_value=4) == 'CONFIRMED_FAIL'
    upper = {**bound, 'operator': '<='}
    assert constraint_evidence_status(upper, 'REGRESSED') == 'UNKNOWN'
    exact = {**bound, 'operator': '=='}
    assert constraint_evidence_status(exact, 'REGRESSED') == 'UNKNOWN'
    assert constraint_evidence_status(exact, measured_value=5) == 'PASS'
    assert constraint_evidence_status(exact, measured_value=4) == 'CONFIRMED_FAIL'


def test_profile_revision_freezes_source_and_does_not_overwrite(tmp_path):
    first = profile()
    constraints = [{'metric_id': 'necessary_fact_status', 'operator': '==', 'value': 'PASS'}]
    second = profile(version=2, constraints=constraints, secondary=['shorter_cases'])
    reason = {'reason': 'Owner added a constraint and metric', 'source_reference': 'dev-review-note-2'}
    record = freeze_objective(tmp_path, second, previous=first, **reason)
    assert record['previous_objective_profile'] == first
    assert record['primary_changed'] is False
    assert not record['goal_family_changed'] and not record['primary_metric_changed']
    assert record['constraints_changed'] and record['secondary_metrics_changed']
    assert not record['component_scope_changed']
    assert {'hard_constraints', 'secondary_metrics'} <= set(record['changed_fields'])
    archived = (tmp_path / 'objective-profile.json').read_bytes()
    second['description'] = 'different'
    with pytest.raises(FileExistsError):
        freeze_objective(tmp_path, second, previous=first, **reason)
    assert (tmp_path / 'objective-profile.json').read_bytes() == archived
    with pytest.raises(ValueError, match='OBJECTIVE_VERSION_MUST_ADVANCE'):
        profile_change(first, profile('shorter_cases'), **reason)
    assert profile_change(first, profile('shorter_cases', 2), **reason)['primary_changed'] is True
    other = {**first, 'objective_profile_id': 'other', 'components': ['retrieval']}
    change = profile_change(first, other, **reason)
    assert change['goal_family_changed'] and change['component_scope_changed']
    assert not change['primary_metric_changed']


def test_launch_objective_reads_only_hash_verified_bytes(tmp_path):
    from rsik.launch import file_binding, read_binding, read_frozen_launch_plan

    plan = tmp_path / 'launch.json'
    source = {'objective_transition': {'current': profile()}}
    plan.write_text(json.dumps(source), encoding='utf-8-sig')
    binding = file_binding(plan)
    launch = SimpleNamespace(plan_path=str(plan), plan_sha256=binding['sha256'])
    assert read_binding(binding) == read_frozen_launch_plan(launch) == source
    plan.write_text(json.dumps({'objective_transition': {'current': profile('shorter_cases', 2)}}))
    with pytest.raises(ValueError, match='LAUNCH_PLAN_CHANGED'):
        read_frozen_launch_plan(launch)
    with pytest.raises(ValueError, match='LAUNCH_STALE_BINDING'):
        read_binding(binding)


def test_objective_follows_normal_round_and_keeps_original_adoption_gate(monkeypatch, tmp_path):
    from rsik import query_workflow

    setup = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests' / 'test_query_workflow.py'))['setup']
    kwargs, calls, _ = setup(monkeypatch, tmp_path, p3_change=True)
    selected = profile('shorter_cases', 2)
    transition = {'previous': profile(), 'current': selected, 'reason': 'Owner changed primary',
                  'source_reference': 'DEV fixture'}
    observed = []
    diagnose = kwargs['roles'].diagnose

    def capture(public_context):
        observed.append(deepcopy(public_context['objective']))
        return diagnose(public_context)

    monkeypatch.setattr(kwargs['roles'], 'diagnose', capture)
    result = query_workflow.run_query_round(**kwargs, objective_transition=transition)
    assert observed == [prompt_objective(selected)] and 'objective' not in kwargs['public']
    assert result['objective_profile'] == selected
    assert result['adopted_candidate_id'] != 'P3_ORDER'
    p3 = next(row for row in result['candidate_decisions'] if row['angle'] == 'P3_ORDER')
    assert p3['reason_codes'] == ['ANGLE_VIOLATION']
    assert json.loads((kwargs['directory'] / 'objective-profile.json').read_bytes())['primary_changed']
    assert json.loads((Path(result['selected_training_directory']) / 'round.json').read_bytes())['objective_profile'] == selected


def test_scope_usage_uses_provider_totals_and_does_not_infer_section_tokens(tmp_path):
    ledger = tmp_path / 'budget.sqlite'
    sent = []

    def transport(body, *_):
        sent.append(json.loads(body['input'][1]['content']))
        return response({'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'})

    roles = OpenAIRoles(Budget(ledger), tmp_path / 'roles', tmp_path / 'unused', LeakScanner(),
                        transport=transport, seam_profile='query-autonomous-v1')
    memory = {'schema': 'rsi-autonomy-memory/v1', 'rounds': 0, 'evicted_entries': 0, 'entries': []}
    remember(memory, 'a' * 64, 1, history_item(None, 'NO_GAIN', ['NO_GAIN'],
                                              quality_verdict='REJECTED', execution_status='COMPLETED'))
    first, second = public(), public()
    first['objective'] = prompt_objective(profile())
    second['objective'] = prompt_objective(profile('shorter_cases', 2, secondary=['answer_tokens']))
    second['search_memory'] = project_search_memory(memory)
    roles.call('P2_BUDGET', first)
    roles.call('P2_BUDGET', second)
    paths = list(roles.directory.glob('*.input-receipt.json'))
    data = objective_usage(ledger, paths + paths)
    early, later = data['groups']
    assert sent[0]['public']['objective']['primary_metric'] == 'context_chars'
    assert sent[1]['public']['objective']['primary_metric'] == 'shorter_cases'
    assert 'Unavailable secondary metrics (UNKNOWN): answer_tokens.' in sent[1]['objective_instructions']
    assert later['requests'] == early['requests'] == 1
    assert data['all_usage']['input_tokens'] == 200
    assert data['all_usage']['output_tokens'] == 40
    assert later['proposal_input_tokens']['tokens'] == 100
    assert early['search_memory_tokens']['tokens'] == 0
    assert later['search_memory_tokens'] == {'status': 'UNKNOWN', 'tokens': None, 'unknown_requests': 1}


@pytest.mark.parametrize(('selected', 'error'), [
    (profile('answer_tokens'), 'OBJECTIVE_PRIMARY_UNAVAILABLE:answer_tokens'),
    (profile('replay_seconds'), 'OBJECTIVE_PRIMARY_UNAVAILABLE:replay_seconds'),
    (profile('parent_chars'), 'OBJECTIVE_REFERENCE_METRIC_AXIS:parent_chars'),
    (profile(constraints=[{'metric_id': 'answer_tokens', 'operator': '<=', 'value': 100}]),
     'OBJECTIVE_CONSTRAINT_UNAVAILABLE:answer_tokens'),
])
def test_workflow_rejects_unmeasurable_executable_objective_before_roles(monkeypatch, tmp_path, selected, error):
    from rsik import query_workflow

    setup = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tests' / 'test_query_workflow.py'))['setup']
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    transition = {'previous': None, 'current': selected, 'reason': 'Owner objective',
                  'source_reference': 'DEV fixture'}
    monkeypatch.setattr(kwargs['roles'], 'diagnose', lambda _: pytest.fail('Diagnosis unexpectedly started'))
    with pytest.raises(ValueError, match=error):
        query_workflow.run_query_round(**kwargs, objective_transition=transition)
    assert not kwargs['directory'].exists()
