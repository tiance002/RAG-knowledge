"""Offline integrity checks for curated calibration data and label isolation."""
import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import judge_calibration_dataset as calibration

BUNDLE = ROOT / 'var/judge-calibration-20261030'


class CalibrationDatasetTests(unittest.TestCase):
    def test_eight_cases_six_synthetic_two_historical(self):
        ledger, _ = calibration.validate(BUNDLE)
        self.assertEqual(len(ledger), 8)
        self.assertEqual(sum(r['synthetic_answer'] for r in ledger), 6)
        self.assertTrue(all((r['source_type'] == 'SYNTHETIC_CURATED') == r['synthetic_answer'] for r in ledger))

    def test_gold_is_unchanged_from_previously_authorized_frozen_sources(self):
        ledger, _ = calibration.validate(BUNDLE)
        original_items, _ = calibration.base.authorized_bundle(calibration.PREVIOUS)
        golds = {i['input']['qid']: i['expected_output']['reference_answer'] for i in original_items}
        for row in ledger:
            self.assertEqual(row['reference_answer'], golds[row['qid']])

    def test_historical_answers_match_the_original_sse_ledger(self):
        ledger, _ = calibration.validate(BUNDLE)
        _, original = calibration.base.authorized_bundle(calibration.PREVIOUS)
        answers = {r['qid']: r['answer'] for r in original}
        for row in ledger:
            if not row['synthetic_answer']:
                self.assertEqual(row['answer'], answers[row['qid']])

    def test_required_failure_and_success_mechanisms_are_covered(self):
        ledger, _ = calibration.validate(BUNDLE)
        covered = {name for row in ledger for name in row['coverage']}
        self.assertTrue({'numeric_error', 'statistical_scope_confusion', 'synonym_correct',
                         'alternative_instance_correct', 'missing_required_item', 'unsupported_addition'}.issubset(covered))
        self.assertEqual(sum(r['expected_verdict'] == 'REVIEW_REQUIRED' for r in ledger), 1)

    def test_nested_label_leak_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'label leaked'):
            calibration.assert_no_labels({'metadata': {'nested': [{'expected_verdict': 'PASS'}]}})

    def test_task_exports_only_question_gold_answer_and_origin_metadata(self):
        ledger, items = calibration.validate(BUNDLE)
        class CaptureClient:
            def update_current_span(self, **kwargs):
                calibration.assert_no_labels(kwargs)
            @contextmanager
            def start_as_current_observation(self, **kwargs):
                self.observation = kwargs
                calibration.assert_no_labels(kwargs)
                yield
        client = CaptureClient()
        task = calibration.make_task(client, ledger, items)
        output = task(item=items[1])
        self.assertEqual(set(client.observation['input']), {'question', 'reference_answer'})
        self.assertEqual(set(output), {'answer'})
        self.assertTrue(client.observation['metadata']['synthetic_answer'])
        self.assertEqual(client.observation['name'], 'eval.review_case')

    def test_task_rejects_changed_reference_before_export(self):
        ledger, items = calibration.validate(BUNDLE)
        modified = copy.deepcopy(items[0]); modified['expected_output'] = {'reference_answer': ['wrong']}
        with self.assertRaisesRegex(ValueError, 'differs from frozen'):
            calibration.make_task(None, ledger, items)(item=modified)

    def test_local_label_revision_preserves_original_answers_gold_and_human_conclusions(self):
        original, items = calibration.validate(BUNDLE)
        revised = calibration.base.rows(BUNDLE / 'calibration-ledger-v2.jsonl')
        self.assertEqual(len(revised), len(original))
        for before, after in zip(original, revised):
            for key in ['case_id', 'qid', 'question', 'reference_answer', 'answer', 'answer_hash', 'original_human_core']:
                self.assertEqual(before[key], after[key])
        self.assertEqual(next(r for r in revised if r['case_id'] == 'C07')['expected_verdict'], 'PASS')
        self.assertTrue(all('expected_verdict' not in i['metadata'] for i in items))


if __name__ == '__main__':
    unittest.main()
