"""SYNTHETIC checks of existing pure evaluation functions; no Gold/data/SDK import."""
import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[2] / 'backend/app/services/evaluation.py'
spec = importlib.util.spec_from_file_location('rsi_synthetic_evaluation', path)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationContract(unittest.TestCase):
    def test_unknown_is_not_zero(self):
        result = evaluation.evaluate_evidence(['synthetic-a'], None, gold_grain='field', observed_grain='field', gold_complete=True)
        self.assertEqual(result, {'state': 'UNKNOWN', 'coverage': None})

    def test_grain_mismatch_is_unknown(self):
        result = evaluation.evaluate_evidence(['synthetic-a'], ['synthetic-a'], gold_grain='field', observed_grain='page', gold_complete=True)
        self.assertIsNone(result['coverage'])

    def test_incomplete_qrel_can_only_prove_hit(self):
        result = evaluation.evaluate_ranking(['synthetic-a'], ['synthetic-a'], gold_grain='chunk', observed_grain='chunk', gold_complete=False, k=10)
        self.assertEqual(result['hit_at_k'], 1)
        self.assertIsNone(result['recall_at_k'])
        self.assertIsNone(result['mrr_at_k'])

    def test_loss_recovery_and_partial(self):
        stages = [{'stage': 'retrieved', 'grain': 'field', 'units': ['a', 'b']}, {'stage': 'selected', 'grain': 'field', 'units': ['a']}, {'stage': 'delivered', 'grain': 'field', 'units': ['a', 'b']}]
        result = evaluation.evidence_transitions(['a', 'b'], stages, gold_grain='field', gold_complete=True)
        self.assertEqual(result['retrieved_to_selected']['lost'], ['b'])
        self.assertEqual(result['selected_to_delivered']['recovered'], ['b'])

    def test_review_linkage_required(self):
        with self.assertRaisesRegex(ValueError, 'missing review linkage'):
            evaluation.review_metrics({'core_correctness': 'PASS'})

    def test_partial_unknown_and_denominators(self):
        result = evaluation.summarize_attempts([{'core_correctness': 'PASS'}, {'core_correctness': 'PARTIAL'}, {'core_correctness': 'FAIL'}, {'execution_status': 'NOT_RUN'}])
        self.assertEqual(result['strict_pass_per_judged'], 1/3)
        self.assertEqual(result['confirmed_pass_per_planned'], 1/4)
        self.assertEqual(result['n_unknown'], 1)
        self.assertEqual(result['n_partial'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
