# 回答输出上限调整：4096

- 旧的 250 题评测回答请求显式使用 `max_tokens=2000`。原项目 `LLMService.generate()` 也显式使用 2000；普通聊天 `generate_stream()` 原来未传 `max_tokens`，Agent `get_chat_model()` 默认传 `None`，它们实际默认值由服务端决定，不能声称全项目统一是 2000。
- 本地候选代码新增 `LLM_ANSWER_MAX_TOKENS=4096`；非流式、流式和 `get_chat_model()` 默认请求均使用该配置，显式传入的 `max_tokens` 仍可覆盖。问题改写 200 和图片描述 1000 的专用限制没有改变。
- `D:\RAG knowledge` 中代码已改；ECS `/opt/amrag/experiments/pdf-table-hardcases-20261027/answer-cap4096-v1/backend/app` 是以 ECS 真实生产源为基线的隔离候选。因之前约定暂不切换正式服务，ECS `/opt/amrag/backend/app` 和运行中的服务没有切换，4096 在正式服务中尚未生效。
- 老的 2000 上限原始评测保持原样。4096 使用独立 `smoke-plan.json`、`smoke-results.jsonl`、`smoke-requests.jsonl`，只选此前两组均为空的同一题作初步诊断，不代表完整250题的效果。

## 验证记录

- 本机 `./.venv/Scripts/python.exe -m pytest backend/tests/test_llm_answer_limit.py -q`：exit1，缺少 `langchain_openai`，未声称通过。
- ECS 隔离候选执行 `PYTHONPATH=<candidate backend>:<existing test-tools> /opt/amrag/.venv/bin/python -m pytest test_llm_answer_limit.py -q`：exit0，2 passed；验证流式、非流式、Agent 默认及显式覆盖参数，不发送模型请求。
- `/opt/amrag/.venv/bin/python .../smoke_ecs_answer4096.py ... --preflight`：exit0，`PREPARED_ZERO_CALLS`，两组上下文、模型和 Prompt 已冻结。
- 两次真实模型诊断完成，`smoke-completion.json` exit0。此前同一道题的两组回答均为空、`finish_reason=length` / 2000 tokens；新上限下关闭组返回964字符、3603输出 tokens，开启组返回955字符、2624输出 tokens，均 `finish_reason=stop`。两次响应分别包含8824和5039字符的 `reasoning_content`。这是单题两组的诊断结果，不能推断其余46次空回答都已修复，也不代表回答正确率。
