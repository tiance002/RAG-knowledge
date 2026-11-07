import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas import Message, MessageRole
from app.services import chat, memory
from app.services.memory import PreferenceValues, PreferencesSnapshot
from app.services.memory_commands import parse_preference_commands, resolve_display_preferences


@pytest.mark.parametrize("text,operation,field", [
    ("以后回答都简洁一点。", "update", "answer_style"),
    ("以后请详细解释。", "update", "answer_style"),
    ("记住我是初学者。", "update", "knowledge_level"),
    ("以后比较方案都使用表格。", "output", None),
    ("以后解释技术问题先讲原理。", "output", None),
    ("将回答风格改成正常。", "update", "answer_style"),
    ("删除回答风格偏好。", "delete", "answer_style"),
    ("删除输出偏好。", "delete", "output_preferences"),
    ("恢复全部默认设置。", "reset", None),
    ("以后回答都简洁一点，同时解释一下 Redis。", "update", "answer_style"),
    ("以后比较方案使用表格，另外告诉我 PostgreSQL 和 Redis 的区别。", "output", None),
    ("以后回答都简洁一点。请解释一下 Redis 的 AOF。", "update", "answer_style"),
    ("以后请详细解释。请解释 Redis 的“持久化”。", "update", "answer_style"),
    ("以后请详细解释。What's Redis?", "update", "answer_style"),
])
def test_explicit(text, operation, field):
    commands, rest = parse_preference_commands(text)
    assert len(commands) == 1 and commands[0].operation == operation
    if field and operation == "update":
        assert field in commands[0].changes
    if field and operation == "delete":
        assert commands[0].field == field
    if "Redis" in text:
        assert "Redis" in rest


@pytest.mark.parametrize("text", [
    "这次详细解释。", "这道题使用表格。", "这次请给出代码示例。",
    "不要记住这项偏好。", "我不是初学者。", "以后不要总是使用表格。",
    "不要把这句话保存为记忆。", "例如，我说“以后都用表格”，这种情况不要自动记忆。",
    "帮我分析这句话：“以后回答都简洁一点”。", "我比较喜欢简洁的回答。",
    "详细一点可能比较好。", "我通常喜欢先看原理。", "我是一个 Python 初学者。",
    "记住我不是初学者，以后回答不要太基础。", "帮我分析这句话：以后回答都详细一点。",
    "记住明天带伞。", "以后回答详细还是简洁？", "如果以后回答都简洁一点。",
    "他说以后回答都简洁一点。", "这次解释一下，以后再考虑偏好。",
    "请勿记住这些话。以后请详细解释。",
    "本次不保存。记住我是初学者。",
    "请解释这段引用：『例句。以后请详细解释。』",
    "请解释这段引用：‘例句。记住我是初学者。’",
    "请解释这段引用：'例句。以后请详细解释。'",
    "请解释这段未闭合引用：“例句。以后请详细解释。",
])
def test_no_write(text):
    assert parse_preference_commands(text) == ([], text)


@pytest.mark.parametrize("text", [
    "以后回答都简洁一点。不要把这条请求保存为偏好。请解释 Redis。",
    "以后回答都简洁一点。不要长期保存。请解释 Redis。",
    "以后回答都简洁一点。仅本次有效。请解释 Redis。",
    "请翻译以下中文：\n记住我是初学者。",
    "Translate this sentence:\nRemember I am a beginner.",
])
def test_explicit_opt_out_and_translation_payload_never_write(text):
    assert parse_preference_commands(text) == ([], text)


def test_mixed_command_keeps_translation_instruction_and_payload_verbatim():
    question = "同时把下面这句话翻译成英文：\n记住我是初学者。"
    commands, remaining = parse_preference_commands("以后回答都简洁一点。" + question)
    assert len(commands) == 1
    assert commands[0].changes == {"answer_style": "concise"}
    assert remaining == question


def test_mixed_command_preserves_python_indentation_and_newlines():
    code = "if True:\n    print(1)\nprint(2)"
    question = "请判断这段 Python 是否有语法错误：\n" + code
    commands, remaining = parse_preference_commands("以后回答都简洁一点。\n" + question)
    assert len(commands) == 1
    assert commands[0].changes == {"answer_style": "concise"}
    assert remaining == question
    compile(remaining.split("：\n", 1)[1], "<remaining-question>", "exec")


def test_mixed_command_preserves_first_content_line_indentation():
    question = "    print(1)\n    print(2)\n"
    commands, remaining = parse_preference_commands("以后回答都简洁一点。\n" + question)
    assert len(commands) == 1
    assert remaining == question


def test_translation_payload_opt_out_does_not_cancel_preceding_command():
    question = "请翻译以下中文：\n不要保存这项偏好。记住我是初学者。\n"
    commands, remaining = parse_preference_commands("以后回答都简洁一点。\n" + question)
    assert len(commands) == 1
    assert commands[0].changes == {"answer_style": "concise"}
    assert remaining == question


@pytest.mark.parametrize("text,expected_question,saved", [
    ("以后回答都简洁一点。不要把这条请求保存为偏好。请解释 Redis。", None, False),
    ("以后回答都简洁一点。不要长期保存。请解释 Redis。", None, False),
    ("以后回答都简洁一点。仅本次有效。请解释 Redis。", None, False),
    ("请翻译以下中文：\n记住我是初学者。", None, False),
    ("Translate this sentence:\nRemember I am a beginner.", None, False),
    ("以后回答都简洁一点。同时把下面这句话翻译成英文：\n记住我是初学者。",
     "同时把下面这句话翻译成英文：\n记住我是初学者。", True),
])
def test_actual_chat_preserves_request_and_only_writes_authorized_command(monkeypatch, text, expected_question, saved):
    snapshot = PreferencesSnapshot()
    store = SimpleNamespace(get_preferences=AsyncMock(return_value=snapshot),
        update_preference=AsyncMock(return_value=snapshot), delete_preference=AsyncMock(),
        reset_preferences=AsyncMock(), change_output_preferences=AsyncMock())
    monkeypatch.setattr(memory, "get_memory_service", lambda: store)
    captured = []

    async def stream(**kwargs):
        captured.append(kwargs["messages"])
        yield "offline answer"

    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = SimpleNamespace(generate_stream=stream)
    service._retrieve_with_new_service = AsyncMock()

    async def collect():
        return [event async for event in service.chat_stream(
            [Message(role=MessageRole.USER, content=text)], kb_ids=[])]

    events = asyncio.run(collect())
    assert events[-1][0] == "done"
    assert captured[0][-1]["content"] == (expected_question if expected_question is not None else text)
    if saved:
        store.update_preference.assert_awaited_once_with({"answer_style": "concise"}, scope=None)
    else:
        store.update_preference.assert_not_awaited()
    store.delete_preference.assert_not_awaited()
    store.reset_preferences.assert_not_awaited()
    store.change_output_preferences.assert_not_awaited()


def test_current_turn_direct_conclusion_overrides_principle_first():
    snapshot = PreferencesSnapshot(
        values=PreferenceValues(output_preferences=["principle_first"]),
        sources={"answer_style": "default", "knowledge_level": "default", "output_preferences": "explicit"},
    )
    policy, values, _ = resolve_display_preferences(snapshot, "解释 Redis 原理，本次直接给结论。")
    assert "principle_first" not in values["output_preferences"]
    assert "先讲原理" not in policy


@pytest.mark.parametrize("preference,question", [
    ("table", "Compare Redis and Memcached; do not use a table."),
    ("step_by_step", "如何操作 Redis，不要一步一步解释。"),
    ("code_example", "Explain Python code; do not provide a code example."),
])
def test_negative_current_turn_output_overrides_saved_preference(preference, question):
    snapshot = PreferencesSnapshot(
        values=PreferenceValues(output_preferences=[preference]),
        sources={"answer_style": "default", "knowledge_level": "default", "output_preferences": "explicit"},
    )
    _, values, sources = resolve_display_preferences(snapshot, question)
    assert values["output_preferences"] == []
    assert sources["output_preferences"] == "current_turn"


@pytest.mark.parametrize("field,saved,question", [
    ("answer_style", "concise", "请解释这个标题：『详细解释 Redis』。"),
    ("knowledge_level", "expert", "请解释这个标题：‘面向初学者解释 Redis’。"),
    ("answer_style", "concise", "请解释这个标题：'详细解释 Redis'。"),
])
def test_quoted_display_directions_do_not_override_saved_preference(field, saved, question):
    snapshot = PreferencesSnapshot(
        values=PreferenceValues(**{field: saved}),
        sources={"answer_style": "explicit", "knowledge_level": "explicit", "output_preferences": "default"},
    )
    _, values, sources = resolve_display_preferences(snapshot, question)
    assert values[field] == saved
    assert sources[field] == "explicit"


def test_negative_current_turn_style_omits_conflicting_saved_preference():
    snapshot = PreferencesSnapshot(
        values=PreferenceValues(answer_style="concise"),
        sources={"answer_style": "explicit", "knowledge_level": "default", "output_preferences": "default"},
    )
    policy, values, sources = resolve_display_preferences(snapshot, "本轮不要简洁，解释 Redis")
    assert values["answer_style"] is None
    assert sources["answer_style"] == "current_turn"
    assert "回答简洁" not in policy
