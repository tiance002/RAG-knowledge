"""Local, network-free contracts for the frozen V5 variant D candidate."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.model_router import (
    FEATURE_NAMES, FeatureValue, RouterConfig, RISK_V5_VERSION,
    apply_selection_policy, decide_route, qwen_risk_v4, qwen_risk_v5,
)
from app.services.grounded_request_budget import request_chars


class RiskRouterV5Tests(unittest.TestCase):
    citations = [{'fileId': 'source-a', 'fileName': 'arbitrary.txt', 'content': 'Observed text'}]
    history = [{'role': 'system', 'content': 'System 中文'},
               {'role': 'user', 'content': 'Earlier question'},
               {'role': 'assistant', 'content': 'Earlier 18 units'},
               {'role': 'user', 'content': 'Calculate remaining units in 2042'}]

    def config(self, **changes):
        return RouterConfig(**dict(dict(mode='DYNAMIC', selection_policy='RISK_V5', threshold=.45), **changes))

    def route(self, question, **kwargs):
        return decide_route(question, kwargs.pop('citations', self.citations), config=kwargs.pop('config', self.config()), **kwargs)

    def test_numeric_boundary(self):
        d = self.route('Calculate the difference')
        self.assertEqual((d.score_lower, d.score_upper, d.selected_role), (.45, .45, 'EXPENSIVE'))
        self.assertEqual(self.route('Calculate the difference', config=self.config(threshold=.450001)).selected_role, 'CHEAP')
        self.assertEqual(d.metadata['rule_version'], RISK_V5_VERSION)
        self.assertNotIn('qwen_risk_v4', d.metadata)

    def test_opt_in_and_env_threshold(self):
        self.assertEqual((RouterConfig().mode, RouterConfig().selection_policy, RouterConfig.from_env({}).threshold), ('OFF', 'WEIGHTED_V1', .5))
        self.assertEqual(RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V5'}).threshold, .45)
        self.assertEqual(RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V5', 'RAG_MODEL_ROUTER_THRESHOLD': '.8'}).threshold, .8)

    def test_pure_multi_vs_dependent_multi(self):
        for question, expected in [('Who leads it and what is its title?', 25),
                                   ('What date and time?', 35),
                                   ('Who leads it and what changed because of this?', 35)]:
            with self.subTest(question=question):
                r = qwen_risk_v5(question, self.citations)
                self.assertEqual(r['primary_contributions']['multiple_facts'], expected)
                self.assertEqual(r['score_points'], expected)

    def test_conditional_mechanism_and_history_finance(self):
        r = qwen_risk_v5('Explain what happens if we retry', self.citations)
        self.assertEqual((r['score_points'], r['amplifiers']['conditional_mechanism']), (40, 10))
        r = qwen_risk_v5('What is its budget?', self.citations, self.history)
        self.assertEqual((r['score_points'], r['amplifiers']['history_financial']), (45, 5))
        self.assertEqual(qwen_risk_v5('What is its budget?', self.citations)['score_points'], 35)

    def test_identifier_discount_and_protected_arithmetic_history(self):
        r = qwen_risk_v5('Explain the command', self.citations)
        self.assertEqual((r['score_points'], r['discounts']['literal_identifier_query']), (20, 10))
        self.assertEqual(qwen_risk_v5('Calculate the command', self.citations)['discounts']['literal_identifier_query'], 0)
        self.assertEqual(qwen_risk_v5('Explain its command', self.citations, self.history)['discounts']['literal_identifier_query'], 0)
        self.assertEqual(qwen_risk_v5('Explain the rollback command', self.citations)['score_points'], 0)

    def test_context_alone_and_fixed_task_risk_ignore_length(self):
        long = [{'role': 'system', 'content': 'irrelevant ' * 10000}, *self.history]
        self.assertEqual(self.route('Find the table in 2042', messages=long).selected_role, 'CHEAP')
        for question in ('Calculate budget in 2042', 'Who leads it and what is the title?', 'Explain what happens if we retry'):
            self.assertEqual(qwen_risk_v5(question, self.citations, self.history)['score_points'], qwen_risk_v5(question, self.citations, long)['score_points'])

    def test_exact_context_measurement_and_unknown(self):
        q = self.history[-1]['content']
        r = qwen_risk_v5(q, self.citations, self.history)['context_length']
        self.assertEqual(r, dict(status='OBSERVED', request_chars=request_chars(self.history), system_text_chars=len('System 中文'), prior_history_chars=len('Earlier question') + len('Earlier 18 units'), question_chars=len(q), interpretation='OBSERVE_ONLY_NO_VALIDATED_LENGTH_WEIGHT'))
        missing = qwen_risk_v5(q)['context_length']
        self.assertEqual(missing['status'], 'UNKNOWN')
        for key in ('request_chars', 'system_text_chars', 'prior_history_chars'):
            self.assertIsNone(missing[key])
        self.assertEqual(qwen_risk_v5(q, messages=[])['context_length']['request_chars'], request_chars([]))

    def test_multimodal_text_and_tool_serialization(self):
        messages = [{'role': 'system', 'content': [{'type': 'text', 'text': 'sys'}]},
                    {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'x', 'function': {'name': 'f', 'arguments': '{"x":1}'}}]},
                    {'role': 'tool', 'content': 'result'}, {'role': 'user', 'content': 'q'}]
        measured = qwen_risk_v5('q', messages=messages)['context_length']
        self.assertEqual(measured['request_chars'], request_chars(messages))
        self.assertEqual((measured['system_text_chars'], measured['prior_history_chars']), (3, 6))

    def test_missing_question_and_policy_metadata(self):
        for q in (None, '', '  '):
            self.assertEqual((qwen_risk_v5(q)['status'], self.route(q).selected_role), ('UNKNOWN', 'CHEAP'))
        features = {k: FeatureValue(1, 'OBSERVED', 'TEST') for k in FEATURE_NAMES}
        self.assertEqual(apply_selection_policy('Calculate', features, {}, self.config())[:2], (0, 0))

    def test_off_fixed_evidence_gates(self):
        self.assertEqual(self.route('Calculate', config=self.config(mode='OFF')).reason, 'ROUTER_OFF')
        for mode, role in [('CHEAP_ONLY', 'CHEAP'), ('EXPENSIVE_ONLY', 'EXPENSIVE')]:
            self.assertEqual(self.route('Who leads it?', config=self.config(mode=mode)).selected_role, role)
            self.assertEqual(self.route('Calculate', citations=[], config=self.config(mode=mode)).selected_role, 'NONE')
        for status in ('NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR'):
            self.assertEqual(self.route('Calculate', retrieval_status=status).selected_role, 'NONE')
        self.assertEqual(self.route('Calculate', completeness=0).selected_role, 'NONE')

    def test_v4_frozen_function_and_payload_bytes(self):
        root = Path(__file__).resolve().parents[2]
        frozen = root / 'var/router-risk-v5-20261117/baseline/backend/app/services/model_router.py'
        current = root / 'backend/app/services/model_router.py'
        def function_source(path):
            text = path.read_text(encoding='utf8')
            node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == 'qwen_risk_v4')
            return ast.get_source_segment(text, node)
        self.assertEqual(function_source(frozen), function_source(current))
        spec = importlib.util.spec_from_file_location('_frozen_v4_router_for_test', frozen)
        old = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        for policy in ('WEIGHTED_V1', 'RISK_V4'):
            for mode in ('OFF', 'DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
                for q in ('Calculate budget in 2042', 'What date and time?', 'Explain the rollback command', None):
                    with self.subTest(policy=policy, mode=mode, q=q):
                        kw = dict(mode=mode, selection_policy=policy, threshold=.45)
                        a = decide_route(q, self.citations, self.history, config=RouterConfig(**kw)).to_payload()
                        b = old.decide_route(q, self.citations, self.history, config=old.RouterConfig(**kw)).to_payload()
                        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))

    def test_direct_history_whole_request_high_low_and_guards(self):
        from app.services import grounded_generation as gg
        baseline = SimpleNamespace(model='baseline')
        expensive = SimpleNamespace(model='expensive')
        env = dict(RAG_MODEL_ROUTER_MODE='DYNAMIC', RAG_MODEL_ROUTER_SELECTION_POLICY='RISK_V5', RAG_MODEL_ROUTER_THRESHOLD='.45', RAG_ROUTER_ALLOW_MODEL_CALLS='true', RAG_MODEL_ROUTER_OFF_FILE='')
        with patch.dict(os.environ, env), patch.object(gg, '_role_service', return_value=expensive) as service, patch.object(gg, 'enforce_request_budget') as budget, patch.object(gg, 'event'), patch.object(gg, 'provider_generation', side_effect=AssertionError('provider called')) as provider, patch('app.services.model_router.qwen_risk_v5', wraps=qwen_risk_v5) as scorer:
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, self.history[-1]['content'])[0], expensive)
            self.assertEqual(scorer.call_args.args[2], self.history)
            budget.assert_called_once_with(self.history)
            service.assert_called_once()
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'Calculate remaining units')[0], baseline)
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history[-1:], self.history[-1]['content'])[0], baseline)
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'What is its budget in 2042?')[0], baseline)
            with patch.dict(os.environ, {'RAG_ROUTER_ALLOW_MODEL_CALLS': 'false'}):
                with self.assertRaises(gg.RoutingBlocked):
                    gg.route_direct_history_generation(baseline, None, self.history, self.history[-1]['content'])
            provider.assert_not_called()
        for mode in ('OFF', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            with patch.dict(os.environ, dict(env, RAG_MODEL_ROUTER_MODE=mode)), patch.object(gg, '_role_service', side_effect=AssertionError('service called')):
                self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, self.history[-1]['content'])[0], baseline)


if __name__ == '__main__':
    unittest.main()
