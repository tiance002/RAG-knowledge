"""Explicit-memory examples and ambiguous requests, without any model calls."""
import pytest

from app.services.memory_commands import parse_preference_commands


@pytest.mark.parametrize(
    ("text", "operation", "changes", "field", "remaining"),
    [
        ("以后回答都简洁一点。", "update", {"answer_style": "concise"}, None, ""),
        ("以后请详细解释。", "update", {"answer_style": "detailed"}, None, ""),
        ("记住我是初学者。", "update", {"knowledge_level": "beginner"}, None, ""),
        ("以后比较方案都使用表格。", "output", {"add": ["table"]}, None, ""),
        ("以后解释技术问题先讲原理。", "output", {"add": ["principle_first"]}, None, ""),
        ("以后回答都简洁一点，同时解释一下 Redis。", "update", {"answer_style": "concise"}, None, "同时解释一下 Redis。"),
        ("以后比较方案使用表格，另外告诉我 PostgreSQL 和 Redis 的区别。", "output", {"add": ["table"]}, None, "另外告诉我 PostgreSQL 和 Redis 的区别。"),
        ("将回答风格改成正常。", "update", {"answer_style": "normal"}, None, ""),
        ("把我的回答风格改为正常。", "update", {"answer_style": "normal"}, None, ""),
        ("删除回答风格偏好。", "delete", {}, "answer_style", ""),
        ("删除输出偏好。", "delete", {}, "output_preferences", ""),
        ("恢复全部默认设置。", "reset", {}, None, ""),
        ("恢复默认设置。", "reset", {}, None, ""),
        ("忘记我的回答风格偏好。", "delete", {}, "answer_style", ""),
        ("以后回答都简洁一点。请解释 Redis。", "update", {"answer_style": "concise"}, None, "请解释 Redis。"),
    ],
)
def test_explicit_commands(text, operation, changes, field, remaining):
    commands, question = parse_preference_commands(text)
    assert len(commands) == 1
    assert (commands[0].operation, commands[0].changes, commands[0].field) == (operation, changes, field)
    assert question == remaining


@pytest.mark.parametrize(
    "text",
    [
        "这次详细解释。", "这道题使用表格。", "这次请给出代码示例。",
        "不要记住这项偏好。", "我不是初学者。", "以后不要总是使用表格。",
        "不要把这句话保存为记忆。", "例如，我说“以后都用表格”，这种情况不要自动记忆。",
        "帮我分析这句话：“以后回答都简洁一点”。", "我比较喜欢简洁的回答。",
        "详细一点可能比较好。", "如果以后回答都简洁一点会怎样？",
        "以后回答都简洁一点还是详细一点？", "他说以后请详细解释。",
        "本次回答请简洁。", "我想了解回答风格设置。",
        "以后回答都简洁一点，同时不要记住这项偏好。",
        "以后回答都简洁一点，另外这是一个例子。",
    ],
)
def test_no_implicit_or_ambiguous_memory(text):
    assert parse_preference_commands(text) == ([], text)
