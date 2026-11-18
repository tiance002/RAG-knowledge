"""Offline contracts for quality-first preferences; no model/provider calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services.model_router import (
    FEATURE_NAMES, FeatureValue, QUALITY_FIRST_VERSION, RouterConfig,
    decide_escalation, decide_route, quality_first_candidate,
)
from app.core.router_environment import load_router_environment


class QualityFirstRouterTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'file_id': 'unrelated-file', 'chunk_id': 'unrelated-chunk',
                      'content': 'Ordinary selected evidence.'}]

    def route(self, question, **kwargs):
        return decide_route(question, kwargs.pop('citations', self.rows), [],
                            config=kwargs.pop('config', RouterConfig(mode='DYNAMIC', selection_policy='QUALITY_FIRST_V3')),
                            baseline_model='baseline', **kwargs)

    def test_priority_risks_override_both_deferral_families(self):
        for question, expected in [
            ('Calculate the remaining rollback sequence.', 'DERIVED_NUMERIC_OPERATION'),
            ('List the environment variable for that project.', 'CONVERSATIONAL_ENTITY_ALIGNMENT'),
            ('Compare the branch name and rollback path.', 'RELATION_OR_FIELD_ALIGNMENT'),
            ('比较两项处置流程。', 'RELATION_OR_FIELD_ALIGNMENT'),
        ]:
            with self.subTest(question=question):
                d = self.route(question)
                self.assertEqual(d.selected_role, 'EXPENSIVE')
                self.assertEqual(d.reason, 'BENEFIT_RISK_PRIORITY')
                self.assertIn(expected, d.metadata['selection_policy']['risk_rules'])
                self.assertTrue(d.metadata['selection_policy']['defer_rules'])

    def test_generic_renamed_topics_allow_deferral(self):
        for topic in ('Orion', 'Juniper', 'Aurora'):
            for question, rule in [
                (f'List rollback mitigations for {topic}.', 'PROCEDURAL_COVERAGE'),
                (f'Give the branch name for {topic}.', 'VERBATIM_IDENTIFIER_LOOKUP'),
                (f'{topic} 的环境变量是什么？', 'VERBATIM_IDENTIFIER_LOOKUP'),
            ]:
                with self.subTest(question=question):
                    d = self.route(question)
                    self.assertEqual(d.selected_role, 'CHEAP')
                    self.assertEqual(d.reason, 'LOW_OBSERVED_BENEFIT_FAMILY')
                    self.assertIn(rule, d.metadata['selection_policy']['matched_rules'])
                    self.assertEqual(d.metadata['selection_policy']['cheap_deferral_reason'], d.reason)

    def test_default_prefers_expensive_even_for_simple_lookup(self):
        for question in ('Who is Mira?', 'Give the title.', '什么是索引？'):
            d = self.route(question)
            self.assertEqual((d.selected_role, d.reason), ('EXPENSIVE', 'QUALITY_FIRST_DEFAULT'))
            self.assertEqual(d.to_payload()['rule_version'], QUALITY_FIRST_VERSION)
            self.assertEqual(d.metadata['selection_policy']['interpretation'], 'ESTIMATED_BENEFIT_NOT_PROBABILITY')

    def test_role_override_never_manipulates_score_or_calibrates_unknowns(self):
        for question, role, value in [('Who is Mira?', 'EXPENSIVE', 0), ('Give branch name.', 'CHEAP', 1)]:
            d = self.route(question, feature_overrides={k: FeatureValue(value, 'OBSERVED', 'TEST') for k in FEATURE_NAMES})
            self.assertEqual(d.selected_role, role)
            self.assertEqual((d.score_lower, d.score_upper), (value, value))
        d = self.route('Compare rollback paths.', feature_overrides={k: FeatureValue() for k in FEATURE_NAMES})
        self.assertEqual((d.score_lower, d.score_upper), (0, 1))
        self.assertTrue(all(v.status == 'UNKNOWN' for v in d.features.values()))

    def test_missing_question_preserves_unknown_policy(self):
        for question in (None, '', '   '):
            d = self.route(question)
            self.assertEqual(d.selected_role, 'BASELINE')
            self.assertEqual(d.metadata['selection_policy']['status'], 'UNKNOWN')
            self.assertEqual(d.metadata['selection_policy']['matched_rules'], [])
            self.assertIsNone(d.metadata['selection_policy']['role_override'])

    def test_off_fixed_modes_and_evidence_gates_remain_authoritative(self):
        for mode, role in [('OFF', 'BASELINE'), ('CHEAP_ONLY', 'CHEAP'), ('EXPENSIVE_ONLY', 'EXPENSIVE')]:
            cfg = RouterConfig(mode=mode, selection_policy='QUALITY_FIRST_V3')
            for question in ('Compare rollback paths.', 'Give branch name.'):
                self.assertEqual(self.route(question, config=cfg).selected_role, role)
            if mode == 'OFF':
                self.assertEqual(self.route(None, config=cfg, citations=[]).reason, 'ROUTER_OFF')
            else:
                for kwargs in ({'citations': []}, {'completeness': 0}, {'retrieval_status': 'RETRIEVAL_ERROR'},
                               {'retrieval_status': 'NO_RESULTS'}, {'retrieval_status': 'UNKNOWN'}):
                    self.assertEqual(self.route('Compare rollback paths.', config=cfg, **kwargs).selected_role, 'NONE')

    def test_configuration_is_opt_in_and_invalid_policy_rejected(self):
        self.assertEqual((RouterConfig().mode, RouterConfig().selection_policy), ('OFF', 'WEIGHTED_V1'))
        c = RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': ' quality_first_v3 '})
        self.assertEqual((c.mode, c.selection_policy), ('OFF', 'QUALITY_FIRST_V3'))
        for policy in ('V3', 'QUALITY_FIRST_V4', ''):
            with self.assertRaises(ValueError):
                RouterConfig.from_env({'RAG_MODEL_ROUTER_SELECTION_POLICY': policy})

    def test_existing_policy_behavior_unchanged(self):
        weighted = self.route('Compare the versions.', config=RouterConfig(mode='DYNAMIC'))
        affinity = self.route('Compare the versions.', config=RouterConfig(mode='DYNAMIC', selection_policy='AFFINITY_V2'))
        self.assertEqual(weighted.selected_role, 'CHEAP')
        self.assertEqual(affinity.selected_role, 'EXPENSIVE')
        self.assertEqual(affinity.metadata['selection_policy']['matched_rules'], ['RELATIONAL_COMPARISON'])

    def test_no_provider_calls_or_extra_retry_and_no_label_inputs(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            d = self.route('Who is Mira?')
            escalation = decide_escalation(d, completion_status='COMPLETED', quality_status='FAIL',
                                          config=RouterConfig(mode='DYNAMIC', selection_policy='QUALITY_FIRST_V3'))
        self.assertFalse(escalation.should_retry)
        q = 'Give branch name.'
        poison = [{'file_id': 'secret-id', 'gold': 'secret-gold', 'answer': 'secret-answer', 'label': 'secret-label'}]
        self.assertEqual(quality_first_candidate(q), quality_first_candidate(q, poison, poison))
        payload = json.dumps(self.route(q, citations=[dict(self.rows[0], **poison[0])]).to_payload())
        for value in ('secret-id', 'secret-gold', 'secret-answer', 'secret-label', q):
            self.assertNotIn(value, payload)


class RouterEnvironmentTests(unittest.TestCase):
    def test_missing_file_keeps_environment_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {}
            self.assertEqual(load_router_environment(env=env, project_dir=directory), ())
            self.assertEqual(env, {})

    def test_default_path_explicit_override_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            file = root / 'config/model-router.env'
            file.write_text('RAG_MODEL_ROUTER_MODE=DYNAMIC\nRAG_MODEL_ROUTER_SELECTION_POLICY=QUALITY_FIRST_V3\nRAG_ROUTER_EXPENSIVE_ENABLE_THINKING=false\n', encoding='utf8')
            env = {'RAG_MODEL_ROUTER_MODE': 'OFF'}
            loaded = load_router_environment(env=env, project_dir=root)
            self.assertEqual(env['RAG_MODEL_ROUTER_MODE'], 'OFF')
            self.assertNotIn('RAG_MODEL_ROUTER_MODE', loaded)
            self.assertEqual(env['RAG_MODEL_ROUTER_SELECTION_POLICY'], 'QUALITY_FIRST_V3')
            self.assertEqual(env['RAG_ROUTER_EXPENSIVE_ENABLE_THINKING'], 'false')
            override = root / 'other.env'
            override.write_text('RAG_MODEL_ROUTER_MODE=CHEAP_ONLY\n', encoding='utf8')
            explicit = {'RAG_MODEL_ROUTER_ENV_FILE': str(override)}
            load_router_environment(env=explicit, project_dir=root)
            self.assertEqual(explicit['RAG_MODEL_ROUTER_MODE'], 'CHEAP_ONLY')

    def test_disallowed_or_bare_keys_reject_before_any_mutation(self):
        for bad in ('OPENAI_API_KEY=secret', 'RAG_MODEL_ROUTER_SECRET=secret', 'RAG_MODEL_ROUTER_MODE',
                    'RAG_MODEL_ROUTER_MODE="unclosed'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / 'bad.env'
                file.write_text('RAG_MODEL_ROUTER_SELECTION_POLICY=QUALITY_FIRST_V3\n' + bad, encoding='utf8')
                env = {}
                with self.assertRaises(ValueError):
                    load_router_environment(file, env=env)
                self.assertEqual(env, {})

    def test_no_environment_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'literal.env'
            file.write_text('RAG_MODEL_ROUTER_CHEAP_MODEL=${PRIVATE_TOKEN}\n', encoding='utf8')
            env = {'PRIVATE_TOKEN': 'never-copy-secret'}
            load_router_environment(file, env=env)
            self.assertEqual(env['RAG_MODEL_ROUTER_CHEAP_MODEL'], '${PRIVATE_TOKEN}')
