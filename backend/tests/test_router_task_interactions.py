"""Offline contracts for the opt-in task-risk preference; no model calls."""
import json
import unittest

from app.services.model_router import (
    FEATURE_NAMES, FeatureValue, RouterConfig, decide_escalation, decide_route,
    measure_target_field_collision,
)


class TaskInteractionRouterTests(unittest.TestCase):
    def setUp(self):
        self.config = RouterConfig(mode='DYNAMIC', selection_policy='AFFINITY_V2')
        self.rows = [{'file_id': 'source-a', 'chunk_id': 'chunk-a',
                      'content': 'Selected evidence text.'}]

    def route(self, question, **kwargs):
        return decide_route(question, kwargs.pop('citations', self.rows),
                            messages=kwargs.pop('messages', []),
                            config=kwargs.pop('config', self.config),
                            baseline_model='baseline-model', **kwargs)

    def test_generic_operations_prefer_expensive(self):
        cases = [
            ('Compare the two versions of this instrument.', 'RELATIONAL_COMPARISON'),
            ('Give total income and grant income respectively.', 'ORDERED_FIELD_ALIGNMENT'),
            ('What is the tabulated distance for 20 W at 600 MHz?', 'CONDITIONED_TABLE_LOOKUP'),
            ('List compulsory module identifiers in ascending order, in list format.', 'CONSTRAINED_ORDERED_LIST'),
            ('Mira played the lead in The Voyage, by which director?', 'ENTITY_RELATION_JOIN'),
            ('请分别列出两种统计口径的支出。', 'ORDERED_FIELD_ALIGNMENT'),
        ]
        for question, rule in cases:
            with self.subTest(question=question):
                result = self.route(question)
                self.assertEqual(result.selected_role, 'EXPENSIVE')
                self.assertIn(rule, result.metadata['selection_policy']['matched_rules'])
                self.assertGreaterEqual(result.score_lower, self.config.task_risk_floor)

    def test_visual_reference_requires_observed_structure(self):
        question = 'How many layers are in the face diagram on this slide?'
        rows = [dict(self.rows[0], content_type='image_caption')]
        result = self.route(question, citations=rows)
        self.assertEqual(result.selected_role, 'EXPENSIVE')
        self.assertIn('VISUAL_REFERENCE_WITH_STRUCTURE', result.metadata['selection_policy']['matched_rules'])
        plain = self.route(question)
        self.assertNotIn('VISUAL_REFERENCE_WITH_STRUCTURE', plain.metadata['selection_policy']['matched_rules'])
        unknown = self.route(question, citations=rows, messages=None)
        self.assertEqual(unknown.features['M'].status, 'UNKNOWN')
        self.assertNotIn('VISUAL_REFERENCE_WITH_STRUCTURE', unknown.metadata['selection_policy']['matched_rules'])

    def test_long_table_alone_does_not_force_expensive(self):
        rows = [dict(self.rows[0], content='| Item | Value |\n' * 1600, content_type='table')]
        result = self.route('What is the title?', citations=rows, messages=None)
        self.assertEqual(result.selected_role, 'CHEAP')
        self.assertEqual(result.metadata['selection_policy']['matched_rules'], [])
        self.assertEqual(result.features['L'].value, 1)

    def test_numeric_question_without_table_anchor_has_no_table_rule(self):
        result = self.route('What equipment uses 20 W at 600 MHz?')
        self.assertNotIn('CONDITIONED_TABLE_LOOKUP', result.metadata['selection_policy']['matched_rules'])

    def test_plain_entity_lookup_does_not_trigger_join(self):
        result = self.route('Who is Mira?')
        self.assertEqual(result.metadata['selection_policy']['matched_rules'], [])
        self.assertEqual(result.selected_role, 'CHEAP')

    def test_opt_in_does_not_change_default_policy(self):
        self.assertEqual(RouterConfig().mode, 'OFF')
        self.assertEqual(RouterConfig().selection_policy, 'WEIGHTED_V1')
        result = self.route('Compare the versions.', config=RouterConfig(mode='DYNAMIC'))
        self.assertEqual(result.selected_role, 'CHEAP')
        self.assertEqual(result.metadata['selection_policy']['matched_rules'], [])

    def test_off_and_fixed_modes_remain_authoritative(self):
        for mode, role in [('OFF', 'BASELINE'), ('CHEAP_ONLY', 'CHEAP'), ('EXPENSIVE_ONLY', 'EXPENSIVE')]:
            with self.subTest(mode=mode):
                result = self.route('Compare the versions.', config=RouterConfig(mode=mode, selection_policy='AFFINITY_V2'))
                self.assertEqual(result.selected_role, role)
                if mode == 'OFF':
                    self.assertEqual(result.selected_model, 'baseline-model')
                    self.assertEqual(result.features, {})

    def test_retrieval_and_empty_evidence_gates_precede_risk_rules(self):
        for mode in ('DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            config = RouterConfig(mode=mode, selection_policy='AFFINITY_V2')
            for status in ('RETRIEVAL_ERROR', 'NO_RESULTS', 'LOW_RELEVANCE', 'UNKNOWN'):
                with self.subTest(mode=mode, status=status):
                    result = self.route('Compare the versions.', config=config, retrieval_status=status)
                    self.assertEqual(result.selected_role, 'NONE')
                    self.assertIsNone(result.selected_model)
            self.assertEqual(self.route('Compare the versions.', config=config, citations=[]).selected_role, 'NONE')
            self.assertEqual(self.route('Compare the versions.', config=config, completeness=0).selected_role, 'NONE')

    def test_unknown_observations_are_retained_with_task_preference(self):
        unknown = {key: FeatureValue() for key in FEATURE_NAMES}
        result = self.route('Compare the versions.', feature_overrides=unknown)
        self.assertEqual(result.selected_role, 'EXPENSIVE')
        self.assertEqual(result.score_lower, 0.55)
        self.assertEqual(result.score_upper, 1)
        self.assertTrue(all(value.status == 'UNKNOWN' and value.value is None for value in result.features.values()))
        self.assertEqual(result.metadata['completeness']['status'], 'UNKNOWN')
        self.assertEqual(result.metadata['conflict']['status'], 'UNKNOWN')
        self.assertEqual(result.metadata['selection_policy']['base_score_lower'], 0)

    def test_unmatched_unknown_interval_preserves_baseline(self):
        result = self.route('Who is Mira?', feature_overrides={key: FeatureValue() for key in FEATURE_NAMES})
        self.assertEqual(result.selected_role, 'BASELINE')
        self.assertEqual((result.score_lower, result.score_upper), (0, 1))

    def test_missing_question_does_not_invent_task_cues(self):
        result = self.route(None)
        self.assertEqual(result.metadata['selection_policy']['matched_rules'], [])
        for key in ('T', 'C', 'J'):
            self.assertEqual(result.features[key].status, 'UNKNOWN')

    def test_name_and_topic_substitution_preserves_generic_rule(self):
        results = [self.route(f'{name} played the lead in {work}, by which director?')
                   for name, work in [('Mira', 'The Voyage'), ('Orin', 'The Crossing'), ('Zara', 'Spring Festival')]]
        self.assertEqual({result.selected_role for result in results}, {'EXPENSIVE'})
        self.assertEqual({tuple(result.metadata['selection_policy']['matched_rules']) for result in results},
                         {('ENTITY_RELATION_JOIN',)})

    def test_correlated_rules_use_max_and_floor_remains_configurable(self):
        result = self.route('Compare values in the table for 20 W at 600 MHz; give both respectively in ascending order, JSON format.')
        self.assertGreater(len(result.metadata['selection_policy']['matched_rules']), 2)
        self.assertEqual(result.score_lower, max(result.metadata['selection_policy']['base_score_lower'], 0.55))
        config = RouterConfig(mode='DYNAMIC', selection_policy='AFFINITY_V2', threshold=0.8)
        self.assertEqual(self.route('Compare the versions.', config=config).selected_role, 'CHEAP')

    def test_env_parsing_and_invalid_candidate_settings(self):
        config = RouterConfig.from_env({'RAG_MODEL_ROUTER_MODE': 'dynamic',
            'RAG_MODEL_ROUTER_SELECTION_POLICY': ' affinity_v2 ', 'RAG_MODEL_ROUTER_TASK_RISK_FLOOR': '0.65'})
        self.assertEqual((config.selection_policy, config.task_risk_floor), ('AFFINITY_V2', 0.65))
        for key, value in [('SELECTION_POLICY', 'AUTO'), ('TASK_RISK_FLOOR', 'nan'),
                           ('TASK_RISK_FLOOR', 'inf'), ('TASK_RISK_FLOOR', '-0.1'),
                           ('TASK_RISK_FLOOR', '1.1'), ('TASK_RISK_FLOOR', 'unknown')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                RouterConfig.from_env({'RAG_MODEL_ROUTER_' + key: value})

    def test_unknown_or_truncated_outcome_does_not_automatically_retry(self):
        decision = self.route('Who is Mira?')
        self.assertEqual(decision.selected_role, 'CHEAP')
        for completion, quality in [('UNKNOWN', 'FAIL'), ('COMPLETED', 'UNKNOWN'),
                                    ('COMPLETED', 'NOT_RUN'), ('TRUNCATED', 'FAIL')]:
            with self.subTest(completion=completion, quality=quality):
                retry = decide_escalation(decision, completion_status=completion,
                    quality_status=quality, config=self.config)
                self.assertFalse(retry.should_retry)
                self.assertIsNone(retry.selected_model)

    def collision_rows(self, target='aabbccdd00112233.docx', competitor='ffeeddcc99887766.docx'):
        return [dict(self.rows[0], source_file=target,
                     content='| Total project cost to date | $91234.56 |'),
                {'file_id': 'source-b', 'chunk_id': 'chunk-b', 'source_file': competitor,
                 'content': '| Total project cost to date | $87654.32 |'}]

    def test_target_and_competitor_heading_collision_selects_expensive(self):
        result = self.route('In report aabbccdd, what is the total project cost to date?',
                            citations=self.collision_rows())
        collision = result.metadata['target_field_collision']
        self.assertEqual((collision['status'], collision['value']), ('ESTIMATED', True))
        self.assertEqual(collision['competing_source_label_count'], 1)
        self.assertEqual(collision['evidence_completeness'], 'UNKNOWN')
        self.assertEqual(result.selected_role, 'EXPENSIVE')
        self.assertIn('TARGET_FIELD_DISTRACTORS', result.metadata['selection_policy']['matched_rules'])

    def test_heading_only_in_competitor_does_not_trigger(self):
        rows = self.collision_rows()
        rows[0]['content'] = '| Project title | Synthetic title |'
        result = self.route('In report aabbccdd, what is the total project cost to date?', citations=rows)
        self.assertFalse(result.metadata['target_field_collision']['value'])
        self.assertFalse(result.metadata['target_field_collision']['target_heading_matched'])
        self.assertNotIn('TARGET_FIELD_DISTRACTORS', result.metadata['selection_policy']['matched_rules'])

    def test_repeated_heading_within_one_source_is_not_competition(self):
        rows = self.collision_rows(competitor='aabbccdd00112233.docx')
        collision = measure_target_field_collision('In report aabbccdd, what is the total project cost to date?', rows)
        self.assertEqual((collision['status'], collision['value']), ('ESTIMATED', False))
        self.assertEqual(collision['competing_source_label_count'], 0)

    def test_missing_and_ambiguous_target_names_remain_unknown(self):
        rows = self.collision_rows()
        cases = [
            ('What is the total project cost to date?', rows),
            ('In report aabbccdd, what is the total project cost to date?',
             [dict(row, source_file=None) for row in rows]),
            ('In reports aabbccdd and ffeeddcc, what is the total project cost to date?', rows),
        ]
        for question, citations in cases:
            with self.subTest(question=question, citations=citations):
                collision = measure_target_field_collision(question, citations)
                self.assertEqual((collision['status'], collision['value']), ('UNKNOWN', None))

    def test_collision_rule_is_invariant_to_source_name_substitution(self):
        cases = [('aabbccdd', 'aabbccdd00112233.docx', 'ffeeddcc99887766.docx'),
                 ('01234567', '0123456789abcdef.docx', 'deadbeef12345678.docx'),
                 ('annual-review.xlsx', 'annual-review.xlsx', 'other-review.xlsx')]
        results = [self.route(f'In report {prefix}, what is the total project cost to date?',
                   citations=self.collision_rows(target, competitor))
                   for prefix, target, competitor in cases]
        self.assertEqual({result.selected_role for result in results}, {'EXPENSIVE'})
        self.assertEqual({tuple(result.metadata['selection_policy']['matched_rules']) for result in results},
                         {('TARGET_FIELD_DISTRACTORS',)})

    def test_collision_metadata_omits_source_labels_and_field_values(self):
        result = self.route('In report aabbccdd, what is the total project cost to date?',
                            citations=self.collision_rows())
        serialized = json.dumps(result.to_payload())
        for secret in ('aabbccdd', 'ffeeddcc', '91234.56', '87654.32', 'Total project cost to date'):
            self.assertNotIn(secret, serialized)

    def test_missing_competitor_observation_preserves_unknown_negative(self):
        for missing in ('source_file', 'content'):
            with self.subTest(missing=missing):
                rows = self.collision_rows()
                rows[1][missing] = None
                result = self.route('In report aabbccdd, what is the total project cost to date?', citations=rows)
                collision = result.metadata['target_field_collision']
                self.assertEqual((collision['status'], collision['value']), ('UNKNOWN', None))
                self.assertNotIn('TARGET_FIELD_DISTRACTORS', result.metadata['selection_policy']['matched_rules'])

    def test_chat_file_name_alias_supports_collision_detection(self):
        rows = self.collision_rows()
        for row in rows:
            row['fileName'] = row.pop('source_file')
        result = self.route('In report aabbccdd, what is the total project cost to date?', citations=rows)
        collision = result.metadata['target_field_collision']
        self.assertEqual((collision['status'], collision['value']), ('ESTIMATED', True))
        self.assertIn('TARGET_FIELD_DISTRACTORS', result.metadata['selection_policy']['matched_rules'])
        self.assertEqual(result.selected_role, 'EXPENSIVE')


if __name__ == '__main__':
    unittest.main()
