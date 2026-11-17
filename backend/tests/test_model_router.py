"""Dependency-free router tests: no provider, tokenizer, Gold or validator."""
import json
import math
import unittest

from app.services.model_router import (
    FEATURE_NAMES, FeatureValue, RouterConfig, decide_escalation, decide_route,
    extract_features,
)


class ModelRouterTests(unittest.TestCase):
    def test_english_constraint_cues_match_words_not_name_substrings(self):
        false_cues, _ = extract_features('What industry is Chandamama associated with?', [])
        true_cues, _ = extract_features('Compare film and music', [])
        self.assertEqual(false_cues['C'].value, 0)
        self.assertGreater(true_cues['C'].value, 0)

    def setUp(self):
        self.citations = [{'file_id': 'file-a', 'chunk_id': 'a', 'content': 'Selected source text.', 'score': 0.9}]

    def known(self, value):
        return {key: FeatureValue(value, 'OBSERVED', 'TEST_OBSERVATION') for key in FEATURE_NAMES}

    def route(self, value=0.1, **kwargs):
        return decide_route('Question', self.citations, config=kwargs.pop('config', RouterConfig(mode='DYNAMIC')),
                            baseline_model='existing-model', feature_overrides=self.known(value), **kwargs)

    def test_default_off_preserves_baseline_even_without_retrieval(self):
        decision = decide_route('Question', None, retrieval_status='RETRIEVAL_ERROR',
                                config=RouterConfig(), baseline_model='existing-model')
        self.assertFalse(decision.enabled)
        self.assertEqual((decision.selected_model, decision.reason), ('existing-model', 'ROUTER_OFF'))
        self.assertEqual(decision.features, {})

    def test_from_env_role_mapping_weights_and_threshold_are_explicit(self):
        config = RouterConfig.from_env({'RAG_MODEL_ROUTER_MODE': 'dynamic',
            'RAG_MODEL_ROUTER_CHEAP_MODEL': 'cheap-test', 'RAG_MODEL_ROUTER_EXPENSIVE_MODEL': 'expensive-test',
            'RAG_MODEL_ROUTER_THRESHOLD': '0.35', 'RAG_MODEL_ROUTER_WEIGHT_T': '3',
            'RAG_MODEL_ROUTER_UNKNOWN_POLICY': 'expensive'})
        self.assertEqual((config.mode, config.threshold, config.weights['T']), ('DYNAMIC', 0.35, 3))
        self.assertEqual((config.cheap_model, config.expensive_model), ('cheap-test', 'expensive-test'))
        self.assertEqual(config.unknown_policy, 'EXPENSIVE')
        self.assertEqual(RouterConfig.from_env({}).mode, 'OFF')
        self.assertEqual(RouterConfig().cheap_model, 'Qwen/Qwen3.5-4B')
        self.assertEqual(RouterConfig().expensive_model, 'zai-org/GLM-5.3-Flash')

    def test_invalid_config_and_observations_rejected(self):
        invalid = [dict(mode='AUTO'), dict(unknown_policy='ZERO'), dict(threshold=math.nan),
                   dict(threshold=-0.1), dict(weights={}), dict(weights={k: 0 for k in FEATURE_NAMES}),
                   dict(weights={k: math.inf for k in FEATURE_NAMES}), dict(weights={k: 1e308 for k in FEATURE_NAMES}),
                   dict(escalate_on_truncated='false'), dict(cheap_model=''),
                   dict(cheap_model='same', expensive_model='same')]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RouterConfig(**kwargs)
        for kwargs in [dict(value=0, status='UNKNOWN'), dict(value=math.nan, status='OBSERVED', provenance='TEST'),
                       dict(value=0, status='OBSERVED'), dict(value=1, status='BOGUS')]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                FeatureValue(**kwargs)
        with self.assertRaises(ValueError):
            RouterConfig.from_env({'RAG_MODEL_ROUTER_ESCALATE_ON_TRUNCATED': 'yes'})

    def test_threshold_equality_selects_expensive(self):
        self.assertEqual(self.route(0.49).selected_role, 'CHEAP')
        self.assertEqual(self.route(0.5).selected_role, 'EXPENSIVE')

    def test_unknown_interval_cannot_silently_become_zero_or_high(self):
        unknown = self.known(0.4)
        unknown['D'] = FeatureValue()
        decision = decide_route('Question', self.citations, config=RouterConfig(mode='DYNAMIC'),
                                baseline_model='existing', feature_overrides=unknown)
        self.assertAlmostEqual(decision.score_lower, 0.36)
        self.assertAlmostEqual(decision.score_upper, 0.46)
        # Explicit threshold inside the uncertainty interval preserves baseline.
        decision = decide_route('Question', self.citations, config=RouterConfig(mode='DYNAMIC', threshold=0.4),
                                baseline_model='existing', feature_overrides=unknown)
        self.assertEqual(decision.selected_role, 'BASELINE')
        self.assertEqual(decision.selected_model, 'existing')
        self.assertEqual(decision.reason, 'UNCERTAIN_INTERVAL_BASELINE')

    def test_known_interval_can_decide_despite_one_unknown(self):
        for known_value, role in [(0, 'CHEAP'), (1, 'EXPENSIVE')]:
            values = self.known(known_value)
            values['D'] = FeatureValue()
            self.assertEqual(decide_route('Q', self.citations, config=RouterConfig(mode='DYNAMIC'),
                feature_overrides=values).selected_role, role)

    def test_unknown_policy_is_explicit(self):
        unknown = {key: FeatureValue() for key in FEATURE_NAMES}
        for policy in ('BASELINE', 'CHEAP', 'EXPENSIVE'):
            config = RouterConfig(mode='DYNAMIC', unknown_policy=policy)
            decision = decide_route('Q', self.citations, config=config, baseline_model='existing', feature_overrides=unknown)
            self.assertEqual((decision.score_lower, decision.score_upper), (0, 1))
            self.assertEqual(decision.selected_role, policy)

    def test_zero_weight_unknown_feature_does_not_widen_interval(self):
        weights = {key: 0 for key in FEATURE_NAMES}; weights['T'] = 2
        values = {key: FeatureValue() for key in FEATURE_NAMES}
        values['T'] = FeatureValue(0.3, 'OBSERVED', 'EXPLICIT')
        decision = decide_route('Q', self.citations, config=RouterConfig(mode='DYNAMIC', weights=weights), feature_overrides=values)
        self.assertEqual((decision.score_lower, decision.score_upper, decision.selected_role), (0.3, 0.3, 'CHEAP'))

    def test_fixed_modes_and_retrieval_gates(self):
        for mode, role in [('CHEAP_ONLY', 'CHEAP'), ('EXPENSIVE_ONLY', 'EXPENSIVE')]:
            config = RouterConfig(mode=mode)
            self.assertEqual(self.route(config=config).selected_role, role)
            for status, expected in [('RETRIEVAL_ERROR', 'RETRIEVAL_FAILURE'),
                                     ('NO_RESULTS', 'INSUFFICIENT_EVIDENCE'),
                                     ('LOW_RELEVANCE', 'INSUFFICIENT_EVIDENCE'),
                                     ('UNKNOWN', 'RETRIEVAL_STATUS_UNKNOWN')]:
                decision = decide_route('Q', self.citations, retrieval_status=status, config=config)
                self.assertEqual((decision.selected_model, decision.selected_role, decision.reason), (None, 'NONE', expected))
            self.assertEqual(decide_route('Q', [], config=config).reason, 'INSUFFICIENT_EVIDENCE')

    def test_features_preserve_missing_evidence_metadata_and_score_kind(self):
        rows = [dict(self.citations[0]), {'content': 'Another chunk', 'score': 0.8}]
        features, metadata = extract_features('compare sources', rows)
        self.assertEqual(features['D'].status, 'UNKNOWN')
        self.assertEqual(features['J'].status, 'ESTIMATED')
        self.assertEqual(features['M'].status, 'UNKNOWN')
        self.assertIsNone(metadata['multimodal_presence']['value'])
        self.assertIsNone(metadata['source_count'])
        self.assertEqual(metadata['source_count_lower_bound'], 1)
        self.assertEqual(metadata['top1'], {'value': 0.9, 'score_kind': 'UNKNOWN', 'status': 'OBSERVED'})
        self.assertEqual(metadata['question_chars'], len('compare sources'))
        self.assertEqual(metadata['evidence_count'], 2)
        self.assertEqual(metadata['completeness']['status'], 'UNKNOWN')
        self.assertEqual(metadata['conflict']['status'], 'UNKNOWN')

    def test_measured_counts_heuristic_estimates_and_provenance(self):
        rows = self.citations + [{'fileId': 'file-b', 'chunk_id': 'b', 'content': 'Other source'}]
        features, metadata = extract_features('比较并综合分析', rows,
            messages=[{'role': 'user', 'content': 'earlier'}, {'role': 'user', 'content': '比较并综合分析'}],
            completeness=1, conflict=0, top1_score_kind='NORMALIZED_VECTOR')
        self.assertEqual(metadata['source_count'], 2)
        self.assertEqual(metadata['history_message_count'], 1)
        self.assertEqual(metadata['top1']['score_kind'], 'NORMALIZED_VECTOR')
        for feature in features.values():
            self.assertEqual(feature.status, 'ESTIMATED')
            self.assertNotEqual(feature.provenance, 'NOT_CAPTURED')
        self.assertGreater(features['D'].value, 0)
        self.assertEqual(metadata['token_count']['status'], 'ESTIMATED')

    def test_reviewed_failure_categories_are_general_signals_not_named_entities(self):
        simple, _ = extract_features('What is the cost?', self.citations, messages=[])
        for question in ('Calculate the remaining budget and show the subtraction.',
                         'Compare the two reports and give their differences respectively.',
                         '分别比较两个口径的数值，计算差额，按升序列出。'):
            with self.subTest(question=question):
                features, metadata = extract_features(question, self.citations, messages=[])
                self.assertGreater(features['T'].value, simple['T'].value)
                self.assertGreater(features['J'].value, simple['J'].value)
                self.assertEqual(features['J'].status, 'ESTIMATED')
                self.assertTrue(metadata['question_signals']['arithmetic'] or metadata['question_signals']['comparison'])
        features, metadata = extract_features('For a 10 W transmitter at 500 MHz, give the distance to two decimals.', self.citations, messages=[])
        self.assertTrue(metadata['question_signals']['numeric_conditions'])
        self.assertTrue(metadata['question_signals']['output_constraint'])
        self.assertGreater(features['C'].value, simple['C'].value)

    def test_referential_question_requires_actual_history_to_claim_dependency(self):
        question = 'What was its total cost?'
        missing, measured = extract_features(question, self.citations)
        self.assertEqual(missing['D'].status, 'UNKNOWN')
        self.assertEqual(measured['history_dependency']['status'], 'UNKNOWN')
        messages = [{'role': 'user', 'content': 'Which project?'},
                    {'role': 'assistant', 'content': 'Project X'},
                    {'role': 'user', 'content': question}]
        actual, measured = extract_features(question, self.citations, messages)
        self.assertGreaterEqual(actual['D'].value, 0.5)
        self.assertEqual(measured['history_message_count'], 2)
        self.assertEqual(measured['completeness']['status'], 'UNKNOWN')

    def test_explicit_absence_never_routes_to_expensive_even_in_fixed_mode(self):
        for mode in ('DYNAMIC', 'CHEAP_ONLY', 'EXPENSIVE_ONLY'):
            decision = decide_route('Q', self.citations, config=RouterConfig(mode=mode), completeness=0)
            self.assertEqual((decision.selected_role, decision.reason), ('NONE', 'INSUFFICIENT_EVIDENCE'))
        _, measured = extract_features('Q', self.citations, messages=[], completeness=0.5, conflict=1)
        self.assertEqual(measured['conflict']['status'], 'OBSERVED')
        self.assertEqual(measured['completeness']['value'], 0.5)

    def test_raw_top1_score_is_not_implicitly_a_quality_probability(self):
        low = decide_route('Q', [dict(self.citations[0], score=-3)], messages=[], config=RouterConfig(mode='DYNAMIC'))
        high = decide_route('Q', [dict(self.citations[0], score=99)], messages=[], config=RouterConfig(mode='DYNAMIC'))
        self.assertEqual(low.score_lower, high.score_lower)
        self.assertEqual(low.metadata['top1']['score_kind'], 'UNKNOWN')

    def test_uncaptured_question_is_not_zero_difficulty(self):
        for question in (None, '', '  ', [{'type': 'image'}]):
            features, measured = extract_features(question, self.citations, messages=[])
            self.assertEqual(measured['question_status'], 'UNKNOWN')
            for key in ('T', 'C', 'J'):
                self.assertEqual(features[key].status, 'UNKNOWN')
                self.assertIsNone(features[key].value)

    def test_observation_payload_has_no_raw_question_evidence_or_history(self):
        decision = decide_route('secret question', self.citations, config=RouterConfig(mode='DYNAMIC'))
        payload = decision.to_payload()
        encoded = json.dumps(payload)
        self.assertNotIn('secret question', encoded)
        self.assertNotIn('Selected source text.', encoded)
        self.assertEqual(payload['calibration']['status'], 'UNCALIBRATED')
        self.assertEqual(payload['features']['D']['status'], 'ESTIMATED')

    def test_feature_meanings_reasoning_dependencies_and_modalities(self):
        simple, _ = extract_features('事实是什么', self.citations, messages=[])
        reasoning, _ = extract_features('比较并归纳差异', self.citations, messages=[])
        constrained, _ = extract_features('必须分别列出，至少三个，JSON格式', self.citations)
        multi, measured = extract_features('Question', self.citations + [
            {'file_id': 'other', 'chunk_id': 'b', 'content': '| label | value |', 'source_metadata': {'sheet': 'Sheet1', 'cell': 'A1'}}],
            messages=[{'role': 'user', 'content': [{'type': 'text', 'text': 'Question'},
                                                 {'type': 'image_url', 'image_url': {'url': 'private'}}]}])
        self.assertGreater(reasoning['J'].value, simple['J'].value)
        self.assertGreater(constrained['C'].value, simple['C'].value)
        self.assertGreater(multi['D'].value, simple['D'].value)
        self.assertGreater(multi['M'].value, simple['M'].value)
        self.assertTrue(measured['multimodal_presence']['value'])
        self.assertEqual(measured['structured_evidence_count'], 1)
        # History is measured separately; it must never masquerade as modality.
        history, _ = extract_features('Question', self.citations,
            messages=[{'role': 'user', 'content': 'old text'}] * 8)
        self.assertEqual(history['M'].value, simple['M'].value)
        self.assertGreater(history['L'].value, simple['L'].value)

    def test_filename_alone_is_not_modality_evidence_and_unknown_blocks_stay_unknown(self):
        rows = [dict(self.citations[0], fileName='picture.png')]
        plain, measured = extract_features('Q', rows, messages=[{'content': 'plain text'}])
        self.assertFalse(measured['multimodal_presence']['value'])
        self.assertEqual(plain['M'].value, 0)
        unknown, measured = extract_features('Q', rows, messages=[{'content': [{'type': 'unsupported'}]}])
        self.assertEqual(unknown['M'].status, 'UNKNOWN')
        self.assertIsNone(measured['multimodal_presence']['value'])

    def test_default_weights_do_not_promote_context_load_alone(self):
        config = RouterConfig.from_env({})
        self.assertEqual(config.weights, {'T': 0.25, 'C': 0.2, 'J': 0.25, 'D': 0.1, 'L': 0.1, 'M': 0.1})
        values = self.known(0)
        for key in ('D', 'L', 'M'):
            values[key] = FeatureValue(1, 'OBSERVED', 'EXPLICIT')
        decision = decide_route('Q', self.citations, config=RouterConfig(mode='DYNAMIC'), feature_overrides=values)
        self.assertEqual(decision.selected_role, 'CHEAP')
        self.assertAlmostEqual(decision.score_upper, 0.3)

    def test_missing_history_and_message_capture_remain_explicit(self):
        features, measured = extract_features('Q', self.citations)
        self.assertIsNone(measured['history_message_count'])
        self.assertEqual(features['M'].status, 'UNKNOWN')
        self.assertEqual(measured['multimodal_presence']['status'], 'UNKNOWN')
        features, measured = extract_features('Q', self.citations, messages=[])
        self.assertEqual(measured['history_message_count'], 0)
        self.assertEqual(features['M'].value, 0)
        self.assertFalse(measured['multimodal_presence']['value'])

    def test_uncaptured_evidence_content_is_not_a_zero_length_observation(self):
        rows = [dict(self.citations[0]), {'file_id': 'b', 'chunk_id': 'b'}]
        features, measured = extract_features('Q', rows)
        self.assertEqual(features['L'].status, 'UNKNOWN')
        self.assertIsNone(measured['evidence_chars'])
        self.assertEqual(measured['evidence_chars_status']['status'], 'UNKNOWN')
        self.assertEqual(measured['observed_evidence_text_chars'], len('Selected source text.'))

    def test_actual_messages_count_system_evidence_and_question_only_once(self):
        question = 'Question'
        evidence = self.citations[0]['content']
        system = 'Instructions and evidence: ' + evidence
        messages = [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': question}]
        features, measured = extract_features(question, self.citations, messages=messages)
        expected_chars = len(system) + len(question)
        self.assertEqual(measured['input_text_chars'], expected_chars)
        self.assertEqual(measured['token_estimate'], math.ceil(expected_chars / 4))
        self.assertAlmostEqual(features['L'].value, expected_chars / 12000)
        self.assertEqual(features['L'].provenance, 'ACTUAL_MESSAGE_TEXT_CHARACTERS_V1')
        self.assertEqual(measured['history_chars'], 0)
        self.assertEqual(measured['history_message_count'], 0)
        self.assertEqual(measured['completeness']['status'], 'UNKNOWN')
        self.assertEqual(measured['conflict']['status'], 'UNKNOWN')
        # Adding citation metadata cannot add already-delivered text to L again.
        extra_citations = self.citations + [dict(self.citations[0], chunk_id='other')]
        repeated, _ = extract_features(question, extra_citations, messages=messages)
        self.assertEqual(repeated['L'], features['L'])

    def test_actual_text_blocks_are_counted_once_and_media_load_stays_unknown(self):
        messages = [{'role': 'system', 'content': [{'type': 'text', 'text': 'System evidence'}]},
                    {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Question'},
                        {'type': 'image_url', 'image_url': {'url': 'private'}}]}]
        features, measured = extract_features('Question', self.citations, messages=messages)
        self.assertEqual(measured['input_text_chars'], len('System evidenceQuestion'))
        self.assertEqual(features['L'].status, 'UNKNOWN')
        self.assertIsNone(measured['token_estimate'])
        self.assertEqual(measured['token_count']['status'], 'UNKNOWN')
        self.assertEqual(measured['text_only_token_estimate'], math.ceil(len('System evidenceQuestion') / 4))
        self.assertTrue(measured['multimodal_presence']['value'])

    def test_source_members_contribute_real_paragraph_dependency(self):
        rows = [dict(self.citations[0], source_members=[
            {'id': 'one', 'file_id': 'file-a'}, {'id': 'two', 'file_id': 'file-a'}])]
        features, measured = extract_features('Q', rows)
        self.assertEqual(measured['paragraph_count'], 3)
        self.assertGreater(features['D'].value, 0)

    def test_escalation_requires_completed_cheap_explicit_fail_and_only_once(self):
        config = RouterConfig(mode='DYNAMIC')
        decision = self.route()
        result = decide_escalation(decision, config=config, completion_status='COMPLETED', quality_status='FAIL')
        self.assertTrue(result.should_retry)
        self.assertEqual(result.selected_model, config.expensive_model)
        for quality in ('UNKNOWN', 'NOT_RUN', 'PASS', 'UNSUPPORTED', 'INVALID'):
            self.assertFalse(decide_escalation(decision, config=config, completion_status='COMPLETED', quality_status=quality).should_retry)
        for status in ('UNKNOWN', 'TRUNCATED', 'EMPTY', 'FAILED'):
            self.assertFalse(decide_escalation(decision, config=config, completion_status=status, quality_status='FAIL').should_retry)
        for args in [dict(retries_used=1), dict(provider_fallback=True), dict(unsupported=True)]:
            self.assertFalse(decide_escalation(decision, config=config, completion_status='COMPLETED', quality_status='FAIL', **args).should_retry)

    def test_fixed_and_expensive_modes_cannot_escalate(self):
        for mode, value in [('OFF', 0), ('CHEAP_ONLY', 0), ('EXPENSIVE_ONLY', 0), ('DYNAMIC', 1)]:
            config = RouterConfig(mode=mode)
            decision = self.route(value, config=config)
            self.assertFalse(decide_escalation(decision, config=config, completion_status='COMPLETED', quality_status='FAIL').should_retry)

    def test_opt_in_truncation_is_separate_and_never_retries_unknown_quality(self):
        config = RouterConfig(mode='DYNAMIC', escalate_on_truncated=True)
        decision = self.route(config=config)
        self.assertTrue(decide_escalation(decision, config=config, completion_status='TRUNCATED', quality_status='FAIL').should_retry)
        self.assertFalse(decide_escalation(decision, config=config, completion_status='TRUNCATED', quality_status='UNKNOWN').should_retry)
        config = RouterConfig(mode='DYNAMIC', escalate_on_quality_fail=False)
        self.assertFalse(decide_escalation(decision, config=config, completion_status='COMPLETED', quality_status='FAIL').should_retry)


if __name__ == '__main__':
    unittest.main()
