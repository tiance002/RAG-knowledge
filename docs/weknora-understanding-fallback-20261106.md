# 查询理解意图与无结果兜底：2026-11-06

## 本次改动

Agent 不再独立调用闲聊/问答意图模型；启用查询理解时复用其意图，未启用或结果无效时默认检索。普通模式同样只在查询理解开启时根据其意图区分闲聊；未选知识库且默认检索时明确返回 NO_SCOPE。用户显式知识库需求仍保留范围保护，联网权限仍由原有开关控制。

确认知识库没有结果时，FALLBACK_STRATEGY 可取 model（默认）或 fixed，FALLBACK_PROMPT 为模型策略提示词，FALLBACK_RESPONSE 为固定策略回复。模型策略显式告知未找到知识库资料，通用知识不充当引用。检索错误、UNKNOWN、降级后空结果、缺失扩展或图片不当作已确认无结果。模型报错保留状态、不自动重试；空输出及截断单独标记。Agent 在检索重试结束后才进入兜底。

## 测试

本地 `D:/RAG knowledge/.venv/Scripts/python.exe -m pytest -q backend/tests/test_query_understanding_fallback.py backend/tests/test_query_agent_context.py backend/tests/test_agent_request_parameters.py backend/tests/test_rewrite_language_and_limit.py --tb=short`：退出码 0，26 passed，7 warnings。为隔离测试，不能替代真实端到端验证。

ECS 隔离目录 `/opt/amrag/experiments/weknora-fallback-20261106`，复用已有 Python 环境，只上传相关代码与运行脚本；固定兜底 0 次模型调用，模型兜底调用 SiliconFlow Qwen/Qwen3.5-4B 1 次，返回 stop，输入 69 / 输出 113 Token。模型正确标明回答是通用知识。回执为本地 `var/weknora-fallback-20261106/summary.json` 和 calls；此项仅为组件测试，不是实际检索或 HTTP/SSE。未更改正式服务及其数据库、索引。

## 限制

真实 Hybrid + HTTP/SSE 两种模式、故障注入和多轮 Agent 兜底端到端仍 NOT RUN。原版 WeKnora 提示词在先前 Qwen 实测中仍有英文改写成中文，未因本任务修复。图片聊天最终回答尚未接通。默认模型兜底可能产生无引用的通用知识；需要严格依据知识库时提示词要求说明无法确认，但模型自身仍可能出错。无余额账单时费用 UNKNOWN。
