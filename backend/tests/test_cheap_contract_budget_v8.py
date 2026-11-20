import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('adapter', Path(__file__).resolve().parents[2] / 'scripts/cheap_contract_budget_v8.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
FIELDS = dict(model='test', max_tokens=4096, temperature=0.7, stream=False,
              extra_body={'enable_thinking': False})


class BudgetTests(unittest.TestCase):
    def fixture(self):
        cites = [dict(content='A' * 200, fileName='a', location='p', chunk_id='a'),
                 dict(content='B' * 1000, fileName='b', location='p', chunk_id='b'),
                 dict(content='absent', fileName='c', location='p', chunk_id='c')]
        text = 'fixed prefix\n' + '\n\n'.join(f'[{i+1}] 来源：{c["fileName"]} {c["location"]}\n{c["content"]}' for i,c in enumerate(cites[:2])) + '\nfixed suffix'
        return [dict(role='system', content=text), dict(role='user', content='question')], cites

    def test_fitting_inputs_unchanged_except_contract_and_absent_not_reintroduced(self):
        messages, cites = self.fixture(); snap = copy.deepcopy((messages, cites))
        out, selected, audit = a.prepare_candidate(messages, cites, provider_fields=FIELDS)
        self.assertEqual(out, a.contract.transform_messages(messages))
        self.assertEqual(len(selected), 2)
        self.assertEqual(audit['inactive_citations'][0]['original_index'], 2)
        self.assertEqual((messages, cites), snap)
        out[0]['content'] = 'mutate'
        self.assertEqual((messages, cites), snap)

    def test_drop_whole_secondary_source_and_preserve_fixed_text(self):
        messages, cites = self.fixture()
        out, selected, audit = a.prepare_candidate(messages, cites, provider_fields=FIELDS, limit=1200)
        self.assertEqual(len(selected), 1)
        self.assertIn('A' * 200, out[0]['content'])
        self.assertNotIn('B' * 1000, out[0]['content'])
        self.assertIn('fixed suffix', out[0]['content'])
        self.assertTrue(out[0]['content'].startswith('fixed prefix'))
        self.assertEqual(audit['omitted_citations'][0]['original_index'], 1)
        self.assertLessEqual(audit['provider_body_chars'], 1200)

    def test_irreducible_and_ambiguous_fail(self):
        messages, cites = self.fixture()
        with self.assertRaisesRegex(ValueError, 'irreducible'):
            a.prepare_candidate(messages, cites, provider_fields=FIELDS, limit=100)
        messages[0]['content'] *= 2
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            a.prepare_candidate(messages, cites, provider_fields=FIELDS)

    def test_history_only_retains_baseline(self):
        messages = [{'role': 'user', 'content': 'earlier'},
                    {'role': 'assistant', 'content': 'answer'},
                    {'role': 'user', 'content': 'current'}]
        snapshot = copy.deepcopy(messages)
        out, cites, audit = a.prepare_candidate(messages, [], provider_fields=FIELDS)
        self.assertEqual(out, messages)
        self.assertEqual(audit['eligibility'], 'NOT_APPLICABLE_HISTORY_ONLY')
        self.assertFalse(audit['contract_applied'])
        self.assertEqual(messages, snapshot)
        self.assertEqual(cites, [])
        self.assertEqual(audit['omitted_history'], [])

    def test_history_removed_as_whole_turn_and_overhead_derived(self):
        messages, cites = self.fixture()
        messages[1:1] = [dict(role='user', content='old' * 1000), dict(role='assistant', content='answer')]
        out, selected, audit = a.prepare_candidate(messages, cites, provider_fields=FIELDS, limit=1200)
        self.assertEqual(out[-1], messages[-1])
        self.assertEqual([x['original_index'] for x in audit['omitted_history']], [1,2])
        self.assertEqual(audit['provider_overhead_chars'], a.provider_body_chars(out, FIELDS)-a.budget.request_chars(out))
