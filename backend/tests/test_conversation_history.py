from app.services.conversation_history import bound_conversation, bound_rewrite_history


def test_prior_rounds_plus_current_keep_system_and_drop_old_round():
    system = {'role': 'system', 'content': 'system rules'}
    old = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'old answer'}]
    recent = [{'role': 'user', 'content': 'recent'}, {'role': 'assistant', 'content': 'recent answer'}]
    current = {'role': 'user', 'content': 'current'}
    original = [system] + old + recent + [current]
    assert bound_conversation(original, 1) == [system] + recent + [current]
    assert original == [system] + old + recent + [current]


def test_incomplete_turns_and_tool_chain_remain_attached_to_turn():
    rows = [{'role': 'assistant', 'content': 'orphan'},
            {'role': 'user', 'content': 'recent'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'}]},
            {'role': 'tool', 'content': 'evidence', 'tool_call_id': 'x'},
            {'role': 'user', 'content': 'follow-up'}]
    assert bound_conversation(rows, 1) == rows[1:]
    assert bound_conversation(rows, 0) == rows[-1:]


def test_manual_rewrite_bound_matches_answer_prior_turns():
    history = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'old answer'},
               {'role': 'user', 'content': 'recent'}, {'role': 'assistant', 'content': 'recent answer'}]
    assert bound_rewrite_history(history, 1) == history[-2:]
    assert bound_rewrite_history(history, 0) == []
