"""Contract tests use synthetic messages only; no benchmark data is loaded."""

import ast
from copy import deepcopy
import inspect
import re

import pytest

from app.services import glm_answer_policy
from app.services.glm_answer_policy import GLM_ANSWER_POLICY, augment_glm_messages


@pytest.mark.parametrize("question", ["请保留原文和精度。", "Keep the original wording and precision."])
def test_preserves_source_history_and_nested_metadata_without_aliasing(question):
    messages = [
        {"role": "system", "content": "source: /release/Example-X 0.123450\n原文", "metadata": {"source": ["a"]}},
        {"role": "user", "content": "Earlier question", "name": "reader"},
        {"role": "assistant", "content": "Earlier approximation", "tool_calls": [{"id": "call-example"}]},
        {"role": "user", "content": [{"type": "text", "text": question}], "metadata": {"lang": "mixed"}},
    ]
    original = deepcopy(messages)
    result = augment_glm_messages(messages)

    assert messages == original
    assert result[:1] + result[2:] == original
    assert result[1] == {"role": "system", "content": GLM_ANSWER_POLICY}
    result[0]["metadata"]["source"].append("changed")
    result[3]["tool_calls"][0]["id"] = "changed"
    result[4]["content"][0]["text"] = "changed"
    assert messages == original


@pytest.mark.parametrize("messages", [[], [{"role": "user", "content": "Question"}], [{"role": "system", "content": "A"}, {"role": "system", "content": "B"}, {"role": "user", "content": "Question"}]])
def test_policy_is_added_once_and_repeated_application_is_idempotent(messages):
    once = augment_glm_messages(messages)
    twice = augment_glm_messages(once)

    assert twice == once
    assert twice is not once
    assert all(left is not right for left, right in zip(once, twice))
    assert sum(message.get("role") == "system" and message.get("content") == GLM_ANSWER_POLICY for message in twice) == 1
    assert [message for message in once if message.get("content") != GLM_ANSWER_POLICY] == messages


def test_matching_user_text_is_preserved_and_does_not_impersonate_system_policy():
    messages = [{"role": "user", "content": GLM_ANSWER_POLICY}]
    assert augment_glm_messages(messages) == [
        {"role": "system", "content": GLM_ANSWER_POLICY},
        messages[0],
    ]


def test_existing_policy_metadata_and_developer_messages_are_preserved():
    messages = [
        {"role": "developer", "content": "Keep this instruction", "metadata": {"x": [1]}},
        {"role": "system", "content": GLM_ANSWER_POLICY, "name": "existing"},
        {"role": "user", "content": "Question"},
    ]
    assert augment_glm_messages(tuple(messages)) == messages
    assert augment_glm_messages(messages)[0] is not messages[0]


def test_runtime_interface_has_no_evaluation_lookup_or_external_dependency():
    assert list(inspect.signature(augment_glm_messages).parameters) == ["messages"]
    source = inspect.getsource(glm_answer_policy)
    tree = ast.parse(source)
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert imports == ["copy", "typing"]
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names.intersection({"open", "eval", "exec", "__import__", "Gold", "gold", "case_id", "answers"})
    assert not re.search(r"qst_\d+|(?:forward|isolated|legacy)-session\d+|terra-ecs-b\d+", source)


def test_policy_is_the_same_for_different_content_and_carries_no_request_metadata():
    chinese = augment_glm_messages([{"role": "user", "content": "金额是多少？", "metadata": {"source_id": "example-a"}}])[0]
    english = augment_glm_messages([{"role": "user", "content": "Who owns the approval?", "metadata": {"source_id": "example-b"}}])[0]
    assert chinese == english == {"role": "system", "content": GLM_ANSWER_POLICY}
