"""Placement and isolation checks only; no generation-quality claim."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('contract_v8_2',
    Path(__file__).resolve().parents[2] / 'scripts/cheap_grounding_contract_v8_2.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class PlacementTests(unittest.TestCase):
    def test_first_system_prefix_and_all_other_messages_retained(self):
        original = [{'role': 'system', 'content': '[E1] 原文\n[2] evidence', 'extra': [1]},
                    {'role': 'user', 'content': 'earlier'},
                    {'role': 'assistant', 'content': 'history'},
                    {'role': 'user', 'content': [{'text': 'current'}]}]
        snapshot = copy.deepcopy(original)
        out = c.transform_messages(original)
        self.assertEqual(out[0]['content'], original[0]['content'] + '\n\n' + c.CONTRACT_TEXT)
        self.assertEqual(out[0]['extra'], [1])
        self.assertEqual(out[1:], original[1:])
        self.assertEqual(len(out), len(original))
        out[0]['extra'].append(2)
        out[-1]['content'][0]['text'] = 'changed'
        self.assertEqual(original, snapshot)

    def test_no_initial_system_and_nonstring_initial_system(self):
        for original in ([{'role': 'user', 'content': 'question'}],
                         [{'role': 'system', 'content': [{'text': 'content'}]},
                          {'role': 'user', 'content': 'question'}]):
            snapshot = copy.deepcopy(original)
            out = c.transform_messages(original)
            self.assertEqual(out[0], {'role': 'system', 'content': c.CONTRACT_TEXT})
            self.assertEqual(out[1:], original)
            self.assertEqual(original, snapshot)

    def test_repeat_and_embedded_text_rejected(self):
        original = [{'role': 'system', 'content': 'evidence'}, {'role': 'user', 'content': 'q'}]
        out = c.transform_messages(original)
        with self.assertRaisesRegex(ValueError, 'already present'):
            c.transform_messages(out)
        with self.assertRaisesRegex(ValueError, 'already present'):
            c.transform_messages([{'role': 'user', 'content': 'prefix' + c.CONTRACT_TEXT}])

    def test_frozen_text_hash_and_invalid_inputs(self):
        self.assertEqual(c.CONTRACT_SHA256,
                         '527db41ffeaafdf3e2936916f7c5186a0d5b13611e8231eb949cda11c78bc004')
        for value in ([], {}, [{'role': 'user'}]):
            with self.assertRaises(ValueError):
                c.transform_messages(value)
