from app.services.model_router import RouterConfig, decide_route, qwen_risk_e, qwen_risk_v6


def test_e_adds_risk_only_for_evidence_backed_requested_slots():
    evidence = [{'fileId': 'fixture', 'content': 'Source evidence'}]
    question = 'What is its name, who founded it, and where is it located?'
    assert qwen_risk_v6(question, evidence)['score_points'] < 45
    result = qwen_risk_e(question, evidence)
    assert result['score_points'] == 45
    assert 'three_independent_requested_slots' in result['e_rules']
    config = RouterConfig.from_env({'RAG_MODEL_ROUTER_MODE': 'DYNAMIC',
                                    'RAG_MODEL_ROUTER_SELECTION_POLICY': 'RISK_E'})
    assert decide_route(question, evidence, config=config).selected_role == 'EXPENSIVE'
    assert decide_route(question, [], config=config).selected_role == 'NONE'
    assert qwen_risk_e(question, [{'fileId': 'fixture'}]) == qwen_risk_v6(question, [{'fileId': 'fixture'}])


def test_e_preserves_v6_scores_and_history_without_evidence():
    question = 'Calculate remaining units in 2042'
    history = [{'role': 'assistant', 'content': 'Earlier result: 18 units'}]
    assert qwen_risk_e(question, [], history) == qwen_risk_v6(question, [], history)
    evidence = [{'fileId': 'fixture', 'content': 'Source evidence'}]
    question = 'Who designed the hall?'
    assert qwen_risk_e(question, evidence)['score_points'] == qwen_risk_v6(question, evidence)['score_points']
