"""Network-free semantic discrimination and frozen-policy contracts for V6."""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.model_router import (
    RouterConfig, RISK_V6_VERSION, decide_route, qwen_risk_v5, qwen_risk_v6,
)


class RiskRouterV6Tests(unittest.TestCase):
    citations = [{'fileId': 'source-a', 'fileName': 'arbitrary.txt', 'content': 'Observed text'}]
    history = [{'role': 'user', 'content': 'Earlier question'},
               {'role': 'assistant', 'content': 'Earlier result: 18 units'},
               {'role': 'user', 'content': 'Calculate remaining units in 2042'}]

    def config(self, **changes):
        return RouterConfig(**dict(dict(mode='DYNAMIC', selection_policy='RISK_V6', threshold=.45), **changes))

    def risk(self, question, **kw):
        return qwen_risk_v6(question, kw.pop('citations', self.citations), kw.pop('messages', self.history), **kw)

    def route(self, question, **kw):
        return decide_route(question, kw.pop('citations', self.citations), config=kw.pop('config', self.config()), **kw)

    def test_independent_dates_and_coupled_deadline(self):
        independent = self.risk('When was the hall opened and when was its annex opened in 2042?', messages=[])
        coupled = self.risk('By what time must the hall open and when must its annex open in 2042?', messages=[])
        self.assertEqual(independent['primary_contributions']['multiple_facts'], 25)
        self.assertEqual(coupled['primary_contributions']['multiple_facts'], 35)
        self.assertEqual((independent['score_points'], coupled['score_points']), (35, 45))
        self.assertFalse(independent['semantic_signals']['coupled_multiple_facts'])
        self.assertTrue(coupled['semantic_signals']['coupled_multiple_facts'])
        measurement = self.risk('What sampling approach and percentage were used?', messages=[])
        self.assertEqual(measurement['primary_contributions']['multiple_facts'], 35)
        # A background event does not turn a name pair into a temporal join.
        background = self.risk('After restoration, who designed the hall and who designed the annex?', messages=[])
        self.assertEqual(background['primary_contributions']['multiple_facts'], 25)

    def test_resulting_names_and_causal_explanations(self):
        for q in ('这座大厅更名为什么？', '这座大厅改名 为 什么？', '这座大厅命名为什么？'):
            self.assertFalse(self.risk(q)['signals']['mechanism'], q)
        for q in ('为什么这座大厅更名？', '这座大厅更名为什么，为什么采用这个名称？', 'Why was the hall renamed?'):
            self.assertTrue(self.risk(q)['signals']['mechanism'], q)

    def test_history_reference_precision(self):
        for q in ('How does reissuing the same request prevent duplicate writes?', 'How does retrying the same operation prevent failures?', '其他大厅叫什么？', '其中哪个大厅开放？', '其余大厅叫什么？'):
            self.assertFalse(self.risk(q)['signals']['history_reference'], q)
        for q in ('What was the value in that same fresh form?', 'What was observed in the same region?', 'What was the result for the same cohort?', 'What was its name?', '其名称是什么？', '同一年度有何变化？'):
            self.assertTrue(self.risk(q)['signals']['history_reference'], q)
            self.assertFalse(self.risk(q, messages=[])['signals']['history_reference'], q)

    def test_exclusion_requires_requested_quantity(self):
        for q in ('Excluding cancelled proposals, how many proposals are listed?', '剔除撤回项目后还有几项？', 'After removing archived entries, what is the count?'):
            r = self.risk(q, messages=[])
            self.assertTrue(r['signals']['arithmetic'], q)
            self.assertEqual(r['score_points'], 45)
        for q in ('Excluding cancelled proposals, name the surviving proposal.', 'Which proposals were excluded?', 'What is the number of proposals?'):
            self.assertFalse(self.risk(q, messages=[])['semantic_signals']['exclusion_arithmetic'], q)

    def test_score_trace_boundary_and_length_observation(self):
        q = 'Excluding cancelled proposals, how many proposals are listed?'
        d = self.route(q)
        self.assertEqual((d.selected_role, d.score_lower, d.score_upper), ('EXPENSIVE', .45, .45))
        self.assertEqual(self.route(q, config=self.config(threshold=.450001)).selected_role, 'CHEAP')
        self.assertEqual(d.metadata['rule_version'], RISK_V6_VERSION)
        self.assertNotIn('qwen_risk_v5', d.metadata)
        r = d.metadata['qwen_risk_v6']
        self.assertEqual(r['score_points'], max(0, min(100, r['primary_max_points'] + sum(r['amplifiers'].values()) - sum(r['discounts'].values()))))
        long = [{'role': 'system', 'content': 'unrelated ' * 10000}, *self.history]
        self.assertEqual(self.risk(q)['score_points'], self.risk(q, messages=long)['score_points'])
        self.assertGreater(self.risk(q, messages=long)['context_length']['request_chars'], self.risk(q)['context_length']['request_chars'])
        self.assertEqual(qwen_risk_v6(q)['context_length']['status'], 'UNKNOWN')

    def test_opt_in_unknown_and_evidence_gates(self):
        self.assertEqual((RouterConfig.from_env({}).mode, RouterConfig.from_env({}).selection_policy), ('OFF', 'WEIGHTED_V1'))
        self.assertEqual(RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V6'}).threshold, .45)
        self.assertEqual(RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V6', 'RAG_MODEL_ROUTER_THRESHOLD': '.7'}).threshold, .7)
        for q in (None, '', '  '):
            self.assertEqual((qwen_risk_v6(q)['status'], self.route(q).selected_role), ('UNKNOWN', 'CHEAP'))
        self.assertEqual(self.route('Calculate', config=self.config(mode='OFF')).reason, 'ROUTER_OFF')
        for status in ('NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR'):
            self.assertEqual(self.route('Calculate', retrieval_status=status).selected_role, 'NONE')
        for mode in ('DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            self.assertEqual(self.route('Calculate', citations=[], config=self.config(mode=mode)).selected_role, 'NONE')
        self.assertEqual(self.route('Calculate', completeness=0).selected_role, 'NONE')

    def test_v4_v5_source_and_all_old_policy_payloads_frozen(self):
        root = Path(__file__).resolve().parents[2]
        frozen = root / 'var/router-risk-v6-20261117/baseline-v5/backend/app/services/model_router.py'
        current = root / 'backend/app/services/model_router.py'
        def source(path, name):
            text = path.read_text(encoding='utf8')
            node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
            return ast.get_source_segment(text, node)
        for name in ('qwen_risk_v4', 'qwen_risk_v5', '_risk_v5_context_length'):
            self.assertEqual(source(frozen, name), source(current, name))
        spec = importlib.util.spec_from_file_location('_frozen_v5_router_v6_test', frozen)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        for policy in ('WEIGHTED_V1', 'AFFINITY_V2', 'QUALITY_FIRST_V3', 'RISK_V4', 'RISK_V5'):
            for mode in ('OFF', 'DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
                for q in ('Calculate budget in 2042', 'When did it open and when did it close?', 'Explain the rollback command', None):
                    with self.subTest(policy=policy, mode=mode, q=q):
                        kw = dict(mode=mode, selection_policy=policy, threshold=.45)
                        self.assertEqual(decide_route(q, self.citations, self.history, config=RouterConfig(**kw)).to_payload(), old.decide_route(q, self.citations, self.history, config=old.RouterConfig(**kw)).to_payload())

    def test_direct_history_actual_history_arithmetic_authorization_and_budget(self):
        from app.services import grounded_generation as gg
        baseline, expensive = SimpleNamespace(model='baseline'), SimpleNamespace(model='expensive')
        env = dict(RAG_MODEL_ROUTER_MODE='DYNAMIC', RAG_MODEL_ROUTER_SELECTION_POLICY='RISK_V6', RAG_MODEL_ROUTER_THRESHOLD='.45', RAG_ROUTER_ALLOW_MODEL_CALLS='true', RAG_MODEL_ROUTER_OFF_FILE='')
        q = self.history[-1]['content']
        with patch.dict(os.environ, env), patch.object(gg, '_role_service', return_value=expensive) as service, patch.object(gg, 'enforce_request_budget') as budget, patch.object(gg, 'event'), patch.object(gg, 'provider_generation', side_effect=AssertionError('provider called')) as provider:
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, q)[0], expensive)
            budget.assert_called_once_with(self.history)
            service.assert_called_once()
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history[-1:], q)[0], baseline)
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'What was its budget in 2042?')[0], baseline)
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'Calculate remaining units')[0], baseline)
            with patch.dict(os.environ, {'RAG_ROUTER_ALLOW_MODEL_CALLS': 'false'}):
                with self.assertRaises(gg.RoutingBlocked):
                    gg.route_direct_history_generation(baseline, None, self.history, q)
            with patch.object(gg, 'enforce_request_budget', side_effect=gg.RoutingBlocked('budget')):
                with self.assertRaises(gg.RoutingBlocked):
                    gg.route_direct_history_generation(baseline, None, self.history, q)
            provider.assert_not_called()
        for mode in ('OFF', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            with patch.dict(os.environ, dict(env, RAG_MODEL_ROUTER_MODE=mode)), patch.object(gg, '_role_service', side_effect=AssertionError('service called')):
                self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, q)[0], baseline)


if __name__ == '__main__':
    unittest.main()
