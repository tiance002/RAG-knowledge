"""V4 contracts: pure risk, cheap default, evidence gates and direct history."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.model_router import (
    FEATURE_NAMES, FeatureValue, RouterConfig, RISK_V4_VERSION,
    apply_selection_policy, decide_route, qwen_risk_v4,
)


class RiskRouterV4Tests(unittest.TestCase):
    citations = [{'fileId': 'source-a', 'fileName': 'arbitrary.txt', 'content': 'Observed text'}]
    history = [{'role': 'assistant', 'content': 'The project had 18 units.'},
               {'role': 'user', 'content': 'Calculate remaining units in 2042'}]

    def config(self, **changes):
        return RouterConfig(**dict(mode='DYNAMIC', selection_policy='RISK_V4', threshold=.45, **changes))

    def route(self, question, **kwargs):
        return decide_route(question, kwargs.pop('citations', self.citations), config=kwargs.pop('config', self.config()), **kwargs)

    def test_defaults_unchanged(self):
        self.assertEqual((RouterConfig().mode, RouterConfig().selection_policy), ('OFF', 'WEIGHTED_V1'))
        self.assertEqual(RouterConfig.from_env({}).selection_policy, 'WEIGHTED_V1')
        decision = self.route('Calculate cost in 2042', config=RouterConfig())
        self.assertEqual(decision.reason, 'ROUTER_OFF')

    def test_cheap_default_and_threshold_boundary(self):
        self.assertEqual(self.route('Who leads Linden?').selected_role, 'CHEAP')
        self.assertEqual(self.route('Calculate the difference').score_lower, .45)
        self.assertEqual(self.route('Calculate the difference').selected_role, 'EXPENSIVE')
        self.assertEqual(self.route('Calculate the difference', config=RouterConfig(mode='DYNAMIC', selection_policy='RISK_V4', threshold=.450001)).selected_role, 'CHEAP')
        d = self.route('Calculate the difference')
        self.assertEqual(d.score_lower, d.score_upper)
        self.assertEqual(d.metadata['rule_version'], RISK_V4_VERSION)
        self.assertEqual(d.metadata['qwen_risk_v4']['interpretation'], 'ESTIMATED_RISK_NOT_PROBABILITY')

    def test_context_alone_is_zero_and_sources_need_three(self):
        citations = [dict(fileId=str(i), fileName='data.xlsx') for i in range(3)]
        risk = qwen_risk_v4('Find the table for 2042', citations)
        self.assertEqual(risk['score_points'], 0)
        self.assertEqual(sum(risk['amplifiers'].values()), 0)
        self.assertFalse(qwen_risk_v4('Calculate', citations[:2])['signals']['competing_sources'])
        self.assertTrue(qwen_risk_v4('Calculate', citations)['signals']['competing_sources'])

    def test_primary_max_amplifiers_discounts(self):
        r = qwen_risk_v4('Explain budget in 2042', [dict(fileName='data.csv')])
        self.assertEqual(r['score_points'], 55)
        self.assertEqual(r['primary_max_points'], 35)
        self.assertEqual(qwen_risk_v4('Explain the rollback sequence', self.citations)['score_points'], 10)
        self.assertEqual(qwen_risk_v4('Calculate remaining units')['score_points'], 35)
        self.assertEqual(qwen_risk_v4('Calculate remaining units in 2042')['score_points'], 55)
        r = qwen_risk_v4('Calculate the rollback sequence', self.citations)
        self.assertEqual(r['discounts']['procedure_or_identifier'], 0)

    def test_reference_requires_actual_assistant_history(self):
        q = 'What is its budget?'
        self.assertFalse(qwen_risk_v4(q)['signals']['history_reference'])
        self.assertFalse(qwen_risk_v4(q, messages=[{'role': 'user', 'content': 'prior'}])['signals']['history_reference'])
        self.assertTrue(qwen_risk_v4(q, messages=self.history)['signals']['history_reference'])

    def test_missing_question_and_metadata_are_unknown_cheap(self):
        for q in (None, '', '  '):
            with self.subTest(q=q):
                self.assertEqual(qwen_risk_v4(q)['status'], 'UNKNOWN')
                self.assertEqual(self.route(q).selected_role, 'CHEAP')
        features = {k: FeatureValue(1, 'OBSERVED', 'TEST') for k in FEATURE_NAMES}
        lower, upper, details = apply_selection_policy('Calculate', features, {}, self.config())
        self.assertEqual((lower, upper, details['status']), (0, 0, 'UNKNOWN'))

    def test_fixed_modes_and_evidence_gates(self):
        for mode, role in [('CHEAP_ONLY', 'CHEAP'), ('EXPENSIVE_ONLY', 'EXPENSIVE')]:
            config = RouterConfig(mode=mode, selection_policy='RISK_V4', threshold=.45)
            self.assertEqual(self.route('Who leads Linden?', config=config).selected_role, role)
            self.assertEqual(self.route('Calculate', citations=[], config=config).selected_role, 'NONE')
        for status in ('NO_RESULTS', 'RETRIEVAL_ERROR', 'LOW_RELEVANCE'):
            self.assertEqual(self.route('Calculate', retrieval_status=status).selected_role, 'NONE')
        self.assertEqual(self.route('Calculate', completeness=0).selected_role, 'NONE')
        self.assertEqual(self.route('Calculate', citations=[]).selected_role, 'NONE')

    def test_missing_risk_metadata_is_unknown_and_cannot_invent_preference(self):
        features = {key: FeatureValue(1, 'OBSERVED', 'EXPLICIT_TEST_OBSERVATION') for key in FEATURE_NAMES}
        lower, upper, policy = apply_selection_policy('Calculate cost in 2042', features, {}, self.config())
        self.assertEqual((lower, upper, policy['status']), (0, 0, 'UNKNOWN'))
        for question in (None, '', '   '):
            with self.subTest(question=question):
                risk = qwen_risk_v4(question)
                self.assertEqual((risk['score_points'], risk['status']), (0, 'UNKNOWN'))
                self.assertEqual(self.route(question).selected_role, 'CHEAP')

    def test_entities_source_labels_and_languages_are_general(self):
        for q in ('Calculate Alder budget in 2042', 'Calculate Birch budget in 2042', '计算新项目2042年预算差额'):
            with self.subTest(q=q):
                self.assertEqual(qwen_risk_v4(q, self.citations)['score_points'], 55)
        for filename in ('renamed-report.xlsx', 'unrelated-source.csv'):
            self.assertEqual(qwen_risk_v4('Calculate budget', [{'fileName': filename}])['score_points'], 55)

    def test_decisions_never_call_provider(self):
        with patch('app.services.grounded_generation.provider_generation', side_effect=AssertionError('provider called')) as provider:
            self.route('Calculate the difference')
            self.route('Who leads Linden?')
            provider.assert_not_called()

    def test_direct_history_high_low_no_history_and_permission_guard(self):
        from app.services import grounded_generation as gg
        baseline = SimpleNamespace(model='baseline')
        expensive = SimpleNamespace(model='expensive')
        env = dict(RAG_MODEL_ROUTER_MODE='DYNAMIC', RAG_MODEL_ROUTER_SELECTION_POLICY='RISK_V4',
                   RAG_MODEL_ROUTER_THRESHOLD='.45', RAG_ROUTER_ALLOW_MODEL_CALLS='true', RAG_MODEL_ROUTER_OFF_FILE='')
        with patch.dict(os.environ, env), patch.object(gg, '_role_service', return_value=expensive) as service, patch.object(gg, 'enforce_request_budget') as budget, patch.object(gg, 'event'), patch.object(gg, 'provider_generation', side_effect=AssertionError('provider called')) as provider:
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'Calculate remaining units in 2042')[0], expensive)
            service.assert_called_once()
            budget.assert_called_once()
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'Calculate remaining units')[0], baseline)
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history[1:], 'Calculate remaining units in 2042')[0], baseline)
            with patch.dict(os.environ, {'RAG_ROUTER_ALLOW_MODEL_CALLS': 'false'}):
                with self.assertRaises(gg.RoutingBlocked):
                    gg.route_direct_history_generation(baseline, None, self.history, 'Calculate remaining units in 2042')
            provider.assert_not_called()

    def test_direct_history_off_fixed_and_no_arithmetic(self):
        from app.services import grounded_generation as gg
        baseline = SimpleNamespace(model='baseline')
        for mode in ('OFF', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            with patch.dict(os.environ, {'RAG_MODEL_ROUTER_MODE': mode, 'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V4'}), patch.object(gg, '_role_service', side_effect=AssertionError('service called')):
                self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'Calculate cost in 2042')[0], baseline)
        with patch.dict(os.environ, {'RAG_MODEL_ROUTER_MODE': 'DYNAMIC', 'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_V4', 'RAG_MODEL_ROUTER_OFF_FILE': ''}), patch.object(gg, '_role_service', side_effect=AssertionError('service called')):
            self.assertIs(gg.route_direct_history_generation(baseline, None, self.history, 'What is its budget in 2042?')[0], baseline)


if __name__ == '__main__':
    unittest.main()
