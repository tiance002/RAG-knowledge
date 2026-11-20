"""Offline contract invariants; these tests make no model-quality claim."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'cheap_grounding_contract_v8', Path(__file__).resolve().parents[2]
    / 'scripts' / 'cheap_grounding_contract_v8.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class ContractTests(unittest.TestCase):
    def test_all_original_messages_and_nested_content_preserved(self):
        messages = [dict(role='system', content='[E7] evidence; [1] source'),
                    dict(role='user', content='earlier request'),
                    dict(role='assistant', content='historical answer'),
                    dict(role='user', content=[{'type': 'text', 'text': 'now'}])]
        before = copy.deepcopy(messages)
        result = contract.transform_messages(messages)
        self.assertEqual(messages, before)
        self.assertEqual(result[:-1], before)
        self.assertEqual(result[-1], dict(role='system', content=contract.CONTRACT_TEXT))
        result[3]['content'][0]['text'] = 'mutated copy'
        self.assertEqual(messages, before)

    def test_same_contract_for_unrelated_questions_and_frozen_hash(self):
        a = contract.transform_messages([dict(role='user', content='question A')])
        b = contract.transform_messages([dict(role='user', content='question B')])
        self.assertEqual(a[-1], b[-1])
        self.assertEqual(contract.CONTRACT_SHA256,
                         hashlib.sha256(contract.CONTRACT_TEXT.encode('utf-8')).hexdigest())
        self.assertGreater(contract.request_chars(a), contract.request_chars(a[:-1]))

    def test_invalid_or_duplicate_application_fails_without_mutation(self):
        for value in ([], {}, [dict(role='user')]):
            with self.assertRaises(ValueError):
                contract.transform_messages(value)
        messages = contract.transform_messages([dict(role='user', content='q')])
        before = copy.deepcopy(messages)
        with self.assertRaisesRegex(ValueError, 'already applied'):
            contract.transform_messages(messages)
        self.assertEqual(messages, before)


if __name__ == '__main__':
    unittest.main()
