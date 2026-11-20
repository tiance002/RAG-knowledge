# 服务端会话与历史证据：本地实施记录

日期：2026-11-14。项目：`D:\RAG knowledge`。

本文件记录首次本地实施时点：当时行为验收尚未执行，没有调用模型、启动产品服务、修改现有数据库、操作 ECS、提交或推送。后续本地及 ECS 隔离实测结果以 `docs/server-chat-history-acceptance-20261114.md` 为准，不把本文件的未运行状态当作最新状态。

## 起点和保护范围

- 实际分支：`codex/bm25-stats-write-optimization-20261112`。
- HEAD：`7c7415f85a063e3f7645f676bd22f7c8f766a732`；工作区原本已有大量未提交改动，因此 HEAD 不代表本次完整源码。
- 已读取 D 盘项目 AGENTS.md；该项目没有 `progress.md` 和 `docs/adr/`，未套用 E 盘另一个项目的数据库或运行架构。
- 保留现有未提交修改，不执行 reset、clean、checkout。未操作原浏览器存储。
- 复用现有 Python 虚拟环境、SQLite、ChatService、普通/Agent 检索生成链、引用构造及历史裁剪。没有复制语料、Chroma 或现有数据库。

## 实施范围

| 文件 | 改动 |
|---|---|
| `backend/app/models/conversations.py` | 新增 chat_sessions、chat_turns、chat_turn_references、chat_evidence_bodies 四表 |
| `backend/app/services/conversation_store.py` | 原子领取、revision、幂等请求、300 秒租约、旧结果隔离、归档、分页、导入和正文哈希复用 |
| `backend/app/services/conversation_runner.py` | 在现有 ChatService 外管理持久化生命周期；终态落库后发送 SSE；取消和断线不重发 |
| `backend/app/services/conversation_context.py` | 后端历史窗口、历史引用身份核验、相关来源补入及模型请求内容身份记录 |
| `backend/app/api/conversations.py` | 会话 API、轮次 API、手动改写与浏览器导入 API |
| `backend/app/api/__init__.py` | 注册会话路由 |
| `backend/app/core/database.py` | 初始化时注册四张新表，沿用已有 create_all；本轮未调用 init_db |
| `backend/app/services/chat.py` | 接收固定运行设置/模型绑定；普通模式接入历史引用；Agent 传递同一来源候选；普通闲聊补全生成终态 |
| `backend/app/services/agent_workflow.py` | Agent 工具结果复用经过校验的历史来源，并保留本轮来源编号 |
| `backend/app/services/grounded_generation.py` | 记录实际应用层生成请求对应的来源身份 |
| `backend/app/services/llm.py` | 记录查询理解结果；流式客户端调用前记录应用层消息身份 |
| `frontend-weknora/src/conversationApi.ts` | 服务端会话 DTO/API 及引用字段适配 |
| `frontend-weknora/src/chatHistory.tsx` | 服务端会话缓存；旧 localStorage 只读；显式导入与原始 JSON 导出 |
| `frontend-weknora/src/pages/Chat.tsx` | 会话 ID+本次问题提交、配置保存、范围变化创建新会话、SSE 和断线恢复 |
| `frontend-weknora/src/App.tsx` | 会话列表及旧历史导入/导出状态 |

## 接口合同

基础路径 `/api/v1`，新接口返回直接 JSON；列表返回 `{items:[...]}`。

| 方法和路径 | 输入/功能 |
|---|---|
| POST `/chat/sessions` | title、scope_mode、kb_ids、config_overrides |
| GET `/chat/sessions?limit=100&offset=0` | 会话分页 |
| GET/PATCH/DELETE `/chat/sessions/{id}` | 详情、带 expected_revision 的修改、归档 |
| GET `/chat/sessions/{id}/turns?limit=50&before_seq=...` | 按顺序返回最近一页，可继续加载更早记录 |
| POST `/chat/sessions/{id}/turns` | 仅 question、client_request_id、expected_revision；拒绝客户端历史和临时配置混入 |
| GET `/chat/turns/{id}` | 恢复状态和已保存回答 |
| POST `/chat/turns/{id}/cancel` | 标记取消，实际执行端保存部分回答 |
| POST `/chat/sessions/{id}/rewrite` | query；历史与模型来自后端会话 |
| POST `/chat/history-import/preview` | sessions 数组，逐项返回可导入/已导入/错误 |
| POST `/chat/history-import` | 按原 ID+完整内容哈希幂等导入，错误项保留索引 |

会话默认参数继承已有系统设置；config_overrides 仅保存显式覆盖。每轮保存实际参数与解析后的知识库范围、运行设置 revision、模型身份，不保存 API Key。provider URL 持久化时移除 userinfo、query 和 fragment。

原 `/chat/completions` 仍为无状态兼容入口，已有评测脚本不强制迁移。新页面使用服务端会话入口。

## 历史、来源和引用

- N 表示最近 N 个真实用户轮次再加本次问题，失败轮次也占据窗口，不跳回更早的成功答案。失败/截断/UNKNOWN 助手正文不进入新的事实背景。
- 导入记录保存原问题、回答、未知字段及不成对消息；导入终态为 LEGACY_IMPORTED/UNKNOWN，不冒充当年的模型成功。
- 历史 `[1]` 在模型输入投影中标注所属历史轮次，数据库和 UI 原始回答不改写。
- 原始问答用于指代理解，原文来源另行校验。校验当前 KB/文件、未删除状态、ACTIVE FileIndexBuild、manifest_hash、chunk_ids 归属，以及正文或可信快照身份。缺失身份时保留展示，不能假装可复用。
- 模型生成图片描述保持来源标记，不升级为原文事实。
- 新的相关历史证据选择规则为 `history-reference-overlap-v1`：复用现有分词，查询词覆盖率至少 0.3，最多补入 3 条，按覆盖率和最近顺序选择；这是可解释的词法估计，不是语义或 Gold 覆盖保证。
- 不移除当前命中种子，不修改当前检索权重、阈值或排名。历史证据整体加入已有 CONTEXT_MAX_CHARS 剩余空间，不截断正文。
- 当前检索 NO_RESULTS、但历史来源可用时，聚合证据状态可为 SUCCESS，同时保存 current_retrieval_status=NO_RESULTS 和 history_references_used；真实 RETRIEVAL_ERROR/UNKNOWN 不改成正常检索。
- 引用正文按精确 UTF-8 SHA256 复用；不同来源继续有独立引用身份。
- `PROVIDER_REQUEST_PREPARED` 表示在应用层客户端调用前检查到该正文，不代表供应商已经接收、理解，也不是语义评分。LangChain 记录标明是消息投影，未伪称完整供应商线协议。

## 并发、失败与恢复

- 同会话一个活动轮次；不同会话后端可并行。前端单页当前只持有一个活动流控制器，多标签页可使用不同会话。
- client_request_id 重复返回原回执；先检查幂等回执，再解析当前模型和范围，模型后来失效也不导致重新生成。
- SQLite 写事务仅用于状态变化；控制状态每 2 秒使用轻量只读查询，租约每 30 秒续期，不按 Token 落库。
- 租约过期保留 UNKNOWN，不自动重发模型；迟到结果必须通过 token 栅栏。断线时持久化已发出的部分正文。
- 成功/错误终态以数据库结果为准，完成保存后才发送终态。没有完整回执时不冒充成功。
- 归档不删除原文和引用；知识库/文件删除不级联删除聊天历史。

## 浏览器历史保留

`rag-workspace.chat-history.v1` 只调用 getItem，不 setItem/removeItem。前端导出原始 JSON 字符串，导入由用户操作触发。相同旧 ID、相同内容复用已导入会话；内容改变则创建新副本。坏记录逐项报告，有效记录可继续导入。原记录与未配对消息保留，原浏览器备份不删除。

服务端成为新会话的持久化权威。服务不可用时显示错误和可查看的浏览器旧记录，不静默退回浏览器继续创建另一条历史分支。

## 本次检查与未运行项

- Python 11 个本轮相关文件 AST 语法检查：exit 0。
- 前端 TypeScript `--noEmit`：exit 0。
- 原始 `git diff --check` 会将现有 CRLF 识别为尾随空白；保存回执 exit 2。显式识别 CRLF、仍保留其他空白检查后的检查 exit 0，未为此统一改写已有文件行尾。
- 6luna 做接口字段/路由/旧存储保护的只读复核；6.1sol xhigh 做并发/来源/状态的复杂复核，发现的接线问题已处理。
- 行为测试、浏览器测试、真实 SQLite 故障恢复、模型请求、Cloud 回读、ECS 更新：NOT_RUN。
- 没有用语法通过声明产品验收通过。测试实施交由负责人指定的测试会话，详见同目录测试方案。

## ECS 后续顺序

先执行本地独立测试方案并复核结果，再冻结本轮源码清单和实际哈希。更新前比较 ECS 对应文件，避免用本地其他未提交性能改动覆盖不同版本。复用已有可恢复备份；必要 SQLite 备份使用在线 backup，而非运行中直接复制主文件。增量更新 API 与前端，新增表迁移不删除旧表、不改文档/Chroma。先用隔离端口验证，再安排服务更新。旧代码回滚时保留新聊天表及数据，前端原 localStorage 备份继续存在。磁盘保护线保持负责人要求的 8 GiB。
