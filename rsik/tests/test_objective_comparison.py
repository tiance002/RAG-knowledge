"""Offline comparison evidence and immutable-history regression checks."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from rsik.analytics.reader import canonical
from rsik.objectives import METRIC_CONTRACTS
from rsik.objective_comparison import compare_historical, write_comparison


def profile(primary='context_chars', secondary=(), constraints=()):
    metrics = {primary, *secondary, *(item['metric_id'] for item in constraints)}
    return {'objective_profile_id': 'test-' + primary, 'version': 1, 'description': 'offline test',
            'primary_metric': primary, 'direction': METRIC_CONTRACTS[primary]['direction'],
            'secondary_metrics': list(secondary), 'tie_breakers': [], 'components': ['context_selection'],
            'hard_constraints': list(constraints),
            'required_metric_contracts': {metric: METRIC_CONTRACTS[metric]['metric_contract_version']
                                          for metric in metrics}}


def decision(candidate, sizes, *, verdict='REJECTED'):
    return {'angle': candidate, 'candidate_id': candidate, 'proposal_id': 'proposal-' + candidate,
            'verdict': verdict, 'gates_passed': False, 'eligible_count': len(sizes),
            'schema_version': 'rsi-candidate-decision/v2.2',
            'per_question': [{'qid': str(index), 'chars_A': 100, 'chars_B': size,
                              'pair_status': 'PAIRED', 'pair_identity': True, 'execution': 'COMPLETED',
                              'candidate_facts': {'necessary': 'PRESENT'}, 'parent_facts': {'necessary': 'PRESENT'},
                              'provenance_B': 'PRESENT',
                              'source_group': 'group-' + str(index), 'upstream_hash': 'frozen-' + str(index)}
                             for index, size in enumerate(sizes)]}


class ObjectiveComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self, decisions, name='one', **extra):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        path = folder / 'round.json'
        data = {'round_id': name, 'execution_state': 'COMPLETED', 'parent_id': 'parent',
                'adopted_candidate_id': None, 'adopted_proposal_id': None,
                'candidate_decisions': decisions, 'proposal_instances': {}, **extra}
        path.write_bytes(canonical(data))
        return {'kind': 'round', 'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}

    def test_goal_switch_keeps_tradeoff_and_rejected_history(self):
        spec = self.artifact([decision('small', [10, 100]), decision('many', [60, 60])])
        before = Path(spec['path']).read_bytes()
        first = compare_historical(profile(secondary=['shorter_cases']), [spec])
        second = compare_historical(profile('shorter_cases', ['context_chars']), [spec])
        first_rows = {row['candidate_id']: row for row in first['candidates']}
        second_rows = {row['candidate_id']: row for row in second['candidates']}
        self.assertEqual(first_rows['small']['rank'], 1)
        self.assertEqual(second_rows['many']['rank'], 1)
        self.assertTrue(all(row['pareto_status'] == 'FRONTIER' for row in first_rows.values()))
        self.assertEqual(first_rows['many']['metric_values']['shorter_cases'], 2)
        self.assertEqual(first_rows['small']['historical_verdict'], 'REJECTED')
        self.assertFalse(first_rows['small']['historical_adopted'])
        self.assertEqual(first_rows['small']['admission_effect'], 'NONE')
        self.assertEqual(Path(spec['path']).read_bytes(), before)

    def test_missing_primary_is_unknown_never_zero(self):
        result = compare_historical(profile('answer_tokens'), [self.artifact([decision('a', [10])])])
        row = result['candidates'][0]
        self.assertIsNone(row['primary_metric_value'])
        self.assertEqual(row['metric_statuses']['answer_tokens'], 'UNKNOWN')
        self.assertIsNone(row['rank'])
        self.assertEqual(result['ranked_count'], 0)

    def test_incomplete_or_duplicate_pairs_never_rank(self):
        partial = decision('partial', [10, 20])
        partial['per_question'].pop()
        duplicate = decision('duplicate', [10, 20])
        duplicate['per_question'][1]['qid'] = '0'
        failed = decision('failed', [10])
        failed['per_question'][0]['execution'] = 'FAILED'
        result = compare_historical(profile(), [self.artifact([partial, duplicate, failed])])
        self.assertEqual(result['ranked_count'], 0)
        self.assertTrue(all(row['metric_values']['context_chars'] is None for row in result['candidates']))

    def test_contract_mismatch_and_unmeasured_secondary_excluded(self):
        spec = self.artifact([decision('a', [10])])
        contracts = deepcopy(METRIC_CONTRACTS)
        contracts['context_chars']['measurement_method'] = 'different unit or evaluator'
        result = compare_historical(profile(), [spec], metric_contracts=contracts)
        self.assertEqual(result['candidates'][0]['metric_statuses']['context_chars'], 'CONTRACT_MISMATCH')
        self.assertEqual(result['ranked_count'], 0)
        optional = compare_historical(profile(secondary=['replay_seconds']), [spec])
        self.assertEqual(optional['ranked_count'], 1)
        self.assertEqual(optional['candidates'][0]['pareto_status'], 'UNKNOWN')
        self.assertEqual(optional['candidates'][0]['ranking_metrics'], ['context_chars'])

    def test_constraints_checked_before_rank_and_unknown_retained(self):
        bad = decision('bad', [10])
        bad['per_question'][0]['candidate_facts']['necessary'] = 'MISSING'
        unknown = decision('unknown', [10])
        unknown['per_question'][0].pop('candidate_facts')
        good = decision('good', [30])
        result = compare_historical(profile(constraints=[{'metric_id': 'necessary_fact_status',
                                                          'operator': '==', 'value': 'PASS'}]),
                                    [self.artifact([bad, unknown, good])])
        rows = {row['candidate_id']: row for row in result['candidates']}
        self.assertEqual(rows['bad']['comparison_status'], 'CONSTRAINT_FAILED')
        self.assertEqual(rows['unknown']['constraint_status'], 'UNKNOWN')
        self.assertIsNone(rows['unknown']['rank'])
        self.assertEqual(rows['good']['rank'], 1)

    def test_secondary_only_changes_pareto_until_explicitly_used_to_break_ties(self):
        spec = self.artifact([decision('one-shorter', [20, 100]), decision('two-shorter', [60, 60])])
        objective = profile(secondary=['shorter_cases'])
        rows = {row['candidate_id']: row for row in compare_historical(objective, [spec])['candidates']}
        self.assertEqual({row['rank'] for row in rows.values()}, {1})
        self.assertEqual(rows['one-shorter']['pareto_status'], 'DOMINATED')
        self.assertEqual(rows['two-shorter']['pareto_status'], 'FRONTIER')
        objective['tie_breakers'] = ['shorter_cases']
        rows = {row['candidate_id']: row for row in compare_historical(objective, [spec])['candidates']}
        self.assertEqual(rows['two-shorter']['rank'], 1)
        self.assertEqual(rows['one-shorter']['rank'], 2)

    def test_legacy_alias_profile_is_readable_without_allowing_duplicate_axes(self):
        import pytest
        from rsik.objectives import validate_profile

        legacy = profile(secondary=['candidate_chars'])
        self.assertEqual(validate_profile(legacy), legacy)
        with pytest.raises(ValueError, match='OBJECTIVE_DUPLICATE_METRIC_ALIAS'):
            compare_historical(legacy, [])

    def test_unsupported_hard_constraint_cannot_pass_in_historical_comparison(self):
        objective = profile(constraints=[{'metric_id': 'answer_tokens', 'operator': '<=', 'value': 100}])
        spec = self.artifact([decision('a', [10])])
        row = compare_historical(objective, [spec])['candidates'][0]
        self.assertEqual(row['constraint_status'], 'UNKNOWN')
        self.assertIsNone(row['rank'])

    def test_dominance_and_deterministic_ties(self):
        spec = self.artifact([decision('z', [20]), decision('a', [20]), decision('large', [30])])
        result = compare_historical(profile(), [spec, spec])
        rows = {row['candidate_id']: row for row in result['candidates']}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows['a']['rank'], 1)
        self.assertEqual(rows['z']['rank'], 1)
        self.assertEqual(rows['large']['rank'], 3)
        self.assertEqual(rows['large']['pareto_status'], 'DOMINATED')
        self.assertEqual(len(rows['large']['dominated_by']), 2)
        self.assertEqual(result, compare_historical(profile(), [spec]))

    def test_evaluation_cohorts_do_not_mix(self):
        first = self.artifact([decision('a', [10])], 'first', evidence_mode='REAL_REPLAY', evaluator_version='one')
        second = self.artifact([decision('b', [20])], 'second', evidence_mode='ENGINEERING_DEMO', evaluator_version='one')
        result = compare_historical(profile(), [first, second])
        self.assertEqual(result['cohort_count'], 2)
        self.assertTrue(all(row['rank'] == 1 for row in result['candidates']))
        # Absent evidence identities must also remain archive-scoped.
        first = self.artifact([decision('a', [10])], 'unknown1')
        second = self.artifact([decision('b', [20])], 'unknown2')
        self.assertEqual(compare_historical(profile(), [first, second])['cohort_count'], 2)

    def test_fact_identity_and_confirmed_absence(self):
        absent = decision('absent', [10])
        absent['per_question'][0]['candidate_facts']['necessary'] = 'ABSENT'
        subset = decision('subset', [10])
        subset['per_question'][0]['parent_facts']['other-required'] = 'PRESENT'
        result = compare_historical(profile(constraints=[{'metric_id': 'necessary_fact_status',
                                                          'operator': '==', 'value': 'PASS'}]),
                                    [self.artifact([absent, subset])])
        rows = {row['candidate_id']: row for row in result['candidates']}
        self.assertEqual(rows['absent']['metric_values']['necessary_fact_status'], 'FAIL')
        self.assertEqual(rows['absent']['constraint_status'], 'FAIL')
        self.assertEqual(rows['subset']['metric_statuses']['necessary_fact_status'], 'UNKNOWN')
        self.assertEqual(result['ranked_count'], 0)

    def test_missing_upstream_or_environment_prevents_cross_archive_pooling(self):
        extra = {'evidence_mode': 'REAL_REPLAY', 'evaluator_version': 'frozen-v1'}
        first = self.artifact([decision('a', [10])], 'first', **extra)
        second = self.artifact([decision('b', [20])], 'second', **extra)
        self.assertEqual(compare_historical(profile(), [first, second])['cohort_count'], 2)
        for key in ('upstream_hash', 'execution_environment_hash'):
            left, right = decision('a', [10]), decision('b', [20])
            for item in (left, right):
                item['per_question'][0]['execution_environment_hash'] = 'same-runtime'
                item['per_question'][0].pop(key)
            first = self.artifact([left], 'first', **extra)
            second = self.artifact([right], 'second', **extra)
            self.assertEqual(compare_historical(profile(), [first, second])['cohort_count'], 2)

    def test_cli_resolves_relative_manifest_paths_and_refuses_overwrite(self):
        import subprocess
        import sys
        spec = self.artifact([decision('a', [10])])
        spec['path'] = str(Path(spec['path']).relative_to(self.root))
        manifest = self.root / 'manifest.json'
        manifest.write_bytes(canonical({'artifacts': [spec]}))
        profile_path = self.root / 'profile.json'
        profile_path.write_bytes(canonical(profile()))
        output = self.root / 'result.json'
        command = [sys.executable, '-m', 'rsik.objective_comparison', '--profile', str(profile_path),
                   '--manifest', str(manifest), '--output', str(output)]
        run = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['ranked_count'], 1)
        self.assertEqual(json.loads(output.read_bytes())['ranked_count'], 1)
        repeated = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(repeated.returncode, 2)
        self.assertIn(b'OBJECTIVE_COMPARISON_FAILED', repeated.stderr)

    def test_hash_binding_and_exclusive_output(self):
        spec = self.artifact([decision('a', [10])])
        result = compare_historical(profile(), [spec])
        target = self.root / 'comparison.json'
        receipt = write_comparison(result, target)
        self.assertEqual(receipt['sha256'], sha256(target.read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            write_comparison(result, target)
        Path(spec['path']).write_text('{}', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            compare_historical(profile(), [spec])


if __name__ == '__main__':
    unittest.main()
