"""Synthetic operation arithmetic; no agents, model calls, or corpus copies."""
import json

import pytest

from rsik.analytics.metrics import compute_metrics
from rsik.analytics.report import build_report, main
from rsik.analytics.projections import project_sources
from rsik.analytics.selection import analyze_selection, SELECTION_CONTRACT_VERSION as VERSION
from rsik.events import EventLog


def selection(identity, parent, **extra):
    return {'projection_type': 'Selection', 'id': identity, 'dimensions': {'role_profile': 'B1'},
        'event_ids': [identity], 'source_refs': [{'source_id': 'log-a'}],
        'payload': {'metric_contract_version': 'rsi-metrics/m1a1', 'selection_id': identity,
            'completed': True, 'valid': True, 'selected_parent_id': parent,
            'returned_count': int(parent is not None), **extra}}


def values(records):
    result = analyze_selection(records)
    return result, {m['metric_id']: m for m in result['metrics']}


def test_empty_selection_separates_success_and_conditional_metrics():
    rows = [selection('empty', None), selection('one', 'p')]
    result, m = values(rows)
    assert (result['completed_valid_operations'], result['successful_operations'], result['empty_operations']) == (2, 1, 1)
    assert m['selection_success_rate']['value'] == .5
    assert m['selected_parent_diversity']['value'] == 1
    assert m['conditional_parent_hhi']['value'] == 1
    old = compute_metrics(rows)
    old_metrics = {m['metric_id']: m for m in old['groups'][0]['metrics']}
    assert old_metrics['S3']['value'] == .5
    assert old_metrics['S6']['value'] == .25
    opt = compute_metrics(rows, selection_contract_version=VERSION)
    block = opt['groups'][0].pop('selection_metrics')
    assert opt == old
    assert block == result


def test_repeated_parent_counts_and_normalized_hhi():
    result, m = values([selection(str(i), p) for i, p in enumerate(('p', 'p', 'q', None))])
    assert result['parent_counts'] == {'p': 2, 'q': 1}
    assert m['selection_success_rate']['value'] == 3/4
    assert m['selected_parent_diversity']['value'] == 2/3
    assert m['conditional_parent_hhi']['numerator'] == 5
    assert m['conditional_parent_hhi']['denominator'] == 9
    assert m['conditional_parent_hhi']['value'] == 5/9


def test_completed_all_empty_has_zero_success_and_na_conditional():
    _, m = values([selection('empty', None)])
    assert m['selection_success_rate']['value'] == 0
    for mid in ('selected_parent_diversity', 'conditional_parent_hhi'):
        assert m[mid]['status'] == 'N/A'
        assert m[mid]['denominator'] == 0
        assert m[mid]['value'] is None


@pytest.mark.parametrize('rows', [[], [selection('interrupted', None, completed='UNKNOWN', valid='UNKNOWN')]])
def test_absent_or_only_interrupted_is_unknown(rows):
    _, m = values(rows)
    assert all(x['status'] == 'UNKNOWN' and x['denominator'] is None for x in m.values())


def test_interrupted_is_coverage_not_empty_failure():
    result, m = values([selection('known', 'p'), selection('interrupted', None, completed='UNKNOWN')])
    assert result['unknown_operations'] == 1
    assert m['selection_success_rate']['value'] == 1
    assert m['selection_success_rate']['denominator'] == 1


def test_mirrors_conflicts_and_source_local_identity():
    first = selection('same', 'p')
    mirror = {**first, 'id': 'mirror'}
    result, _ = values([first, mirror])
    assert result['completed_valid_operations'] == 1
    conflict = selection('same', 'q')
    result, m = values([first, conflict])
    assert result['conflicting_operations'] == result['unknown_operations'] == 1
    assert m['selection_success_rate']['status'] == 'UNKNOWN'
    other = {**conflict, 'source_refs': [{'source_id': 'log-b'}]}
    assert analyze_selection([first, other])['successful_operations'] == 2


@pytest.mark.parametrize('parent', ['', 'UNKNOWN', 'N/A', 1])
def test_unknown_parent_is_not_success(parent):
    result, _ = values([selection('invalid', parent)])
    assert result['unknown_operations'] == 1
    assert result['successful_operations'] == 0


def test_legacy_parent_cannot_supply_operation_denominator():
    row = selection('old', 'p')
    row['projection_type'] = 'Parent'
    assert analyze_selection([row])['observed_operations'] == 0


def test_opt_in_partitions_profiles_and_contracts():
    first, other = selection('a', 'p'), selection('b', 'q')
    other['dimensions'] = {'role_profile': 'other'}
    groups = compute_metrics([first, other], selection_contract_version=VERSION)['groups']
    assert len(groups) == 2
    assert all(g['selection_metrics']['successful_operations'] == 1 for g in groups)
    with pytest.raises(ValueError, match='UNKNOWN_SELECTION_CONTRACT'):
        compute_metrics([], selection_contract_version='unrecognized')


def test_lifecycle_projection_rejects_conflicting_and_interrupted_operations():
    common = {'schema_version': 'rsi-observation/v1', 'metric_contract_version': 'rsi-metrics/m1a1'}
    events = []
    for identity, kinds in (
            ('ok', [('PARENT_SELECTION_STARTED', {}), ('PARENT_SELECTED', {'parent_id': 'p'}),
                    ('PARENT_SELECTION_COMPLETED', {'returned_count': 1})]),
            ('empty', [('PARENT_SELECTION_STARTED', {}), ('PARENT_SELECTION_COMPLETED', {'returned_count': 0})]),
            ('interrupted', [('PARENT_SELECTION_STARTED', {})]),
            ('conflict', [('PARENT_SELECTION_STARTED', {}),
                          ('PARENT_SELECTION_COMPLETED', {'returned_count': 0}),
                          ('PARENT_SELECTION_COMPLETED', {'returned_count': 1})])):
        for kind, payload in kinds:
            events.append({'event_id': str(len(events)), 'sequence': len(events)+1,
                'event_type': kind, 'payload': {**common, 'selection_id': identity, **payload}})
    rows = project_sources([{'source_id': 'lifecycle', 'path': 'synthetic/lifecycle',
                             'sha256': 'a'*64, 'events': events}])
    result = compute_metrics(rows, selection_contract_version=VERSION)['groups'][0]['selection_metrics']
    assert result['completed_valid_operations'] == 2
    assert result['successful_operations'] == result['empty_operations'] == 1
    assert result['unknown_operations'] == 2
    assert result['metrics'][0]['value'] == .5


def test_report_cli_opt_in_is_additive_and_read_only(tmp_path):
    source = tmp_path / 'events.sqlite'
    log = EventLog(source)
    log.append('e', 'UNRELATED', {'experiment_id': 'synthetic'})
    before = source.read_bytes()
    base = build_report([source])
    opt = build_report([source], selection_contract_version=VERSION)
    assert opt.pop('selection_metrics_schema') == VERSION
    for group in opt['statistics']['groups']:
        group.pop('selection_metrics')
    assert opt == base
    output = tmp_path / 'report'
    main(['--event-log', str(source), '--selection-contract-version', VERSION, '--output', str(output)])
    assert json.loads((output/'report.json').read_bytes())['selection_metrics_schema'] == VERSION
    assert json.loads((output/'metrics-schema.json').read_bytes())['selection_contract']['schema'] == VERSION
    assert '独立选择指标' in (output/'report.zh.md').read_text(encoding='utf-8')
    assert source.read_bytes() == before
