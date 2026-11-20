# 服务端会话历史与短期记忆：当前状态核查

日期：2026-11-14；范围：`D:\RAG knowledge` 当前本地源码与已有验收记录。本文件不是正式 ECS 在线状态证明；正式部署与数据库状态由主代理另行只读核验。本次核查未连接 ECS、调用模型或修改产品代码。

## 当前本地实现

- **服务端是新会话的权威来源。** `frontend-weknora/src/pages/Chat.tsx` 创建或更新会话后，向 `/api/v1/chat/sessions/{id}/turns` 只提交本次问题、`client_request_id` 和 `expected_revision`；历史轮次、知识库范围、运行设置和模型身份由 `backend/app/api/conversations.py` 与服务端 Store 解析，不采用客户端注入的历史。原 `/chat/completions` 仍是无状态兼容入口，不能把它当作会话持久化接口。
- **持久化结构。** `backend/app/models/conversations.py` 定义 `chat_sessions`、`chat_turns`、`chat_turn_references`、`chat_evidence_bodies` 四表；回答及来源快照可回读，正文按内容哈希复用。`conversation_store.py` 提供会话列表、轮次分页、归档、历史导入、revision CAS、同会话单活动轮次和请求幂等。租约为 300 秒、运行期间续租；过期恢复为 UNKNOWN，旧 token 不得覆盖后续状态；不自动重发未知请求。
- **短期历史窗口不是数据库删除。** `history_turns()` 选取当前轮之前最近 N 个真实用户轮次，失败轮次也占窗口；`prepare_history()` 根据当前范围排除不兼容历史，只把成功完成的助手回答或符合条件的旧导入回答送入上下文；失败、截断或 UNKNOWN 助手正文不作为事实背景。窗口默认值代码为 20，具体请求使用服务端运行时设置/会话覆盖，不能据源码默认值推断正在运行服务的实际值。历史内的 `[1]` 会重写为历史来源标记，避免与本轮引用序号混淆。
- **历史引用不是直接信任旧快照。** `conversation_context.py::_validate_reference` 核验当前 KB/文件未删除、ACTIVE 构建及 manifest 哈希和 chunk 成员、来源身份、正文与当前 chunk 一致。通过后才可能作为候选；`merge_history_citations` 根据词项相关性和现有正文预算最多追加三项。候选出现不代表已经送达模型；应用层请求身份使用 `PROVIDER_REQUEST_PREPARED`，不证明供应商实际接收或回答正确。
- **浏览器旧历史仍是只读备份。** `chatHistory.tsx` 读取 `localStorage` 的 `rag-workspace.chat-history.v1`，提供显式预检、导入服务端和导出原始 JSON；不会自动清空原浏览器备份。服务端以旧 ID+内容哈希去重，保留未配对消息/原始元数据；旧引用默认 UNKNOWN，只有通过当前来源核验才能复用。缺失或非法的旧知识库范围不能被猜成全库。这里的合成导入测试不证明用户日常浏览器全部历史已经导入。
- **并发及断线。** 前端以会话 ID 记录 controller/提交状态，取消目标轮次并在断线后读取服务端状态，不自动重复提交。后端新轮次先经过有限准入；`conversation_runner.py` 在取消或断线清理前写入终态和部分回答。已记录用户取消时为 CANCELLED；可能已触发模型但结果不确定时 generation_status 为 UNKNOWN；无明确取消的中途断线为 UNKNOWN。实际仍运行的下游任务完成前会保留许可。凭据、租约和 revision 不能跨轮替代。

## 已有验证与边界

- [首次会话历史隔离验收](server-chat-history-acceptance-20261114.md)：冻结版本本地后端 60 项通过；ECS 独立 API/侧库普通及 Agent 各三轮共 6/6 完成，重启后回读和幂等 13/13。ECS 使用只读源库和独立会话库，非正式服务切换；报告所列源码漂移使该结果不能自动外推到其后的全部 dirty 工作区。
- [后续并发与取消验收](hybrid-concurrency-implementation-and-validation-20261114.md)：本地定向集成 229 项及真实 Chrome 27 项通过。浏览器实验使用真实 API/Store、合成 Provider；两个会话并行，取消 A 后 A 保存 CANCELLED、B 完成。独立 ASGI 回归分别覆盖主动取消和直接断线、部分回答、慢退出上游的许可归属。`var/hybrid-concurrency-20261114/browser-20261114-193457-7c6a9ee1/browser-results.json` 为浏览器证据。
- 隔离检索候选 R7 不包含完整服务端会话/历史链，不能用 R7 已运行推断正式服务器已迁移会话。上述测试与浏览器项目源代码均有对应冻结身份和时间；本次未重跑测试，也未重新校验正式端。

## 尚不能宣称已解决的事项

- 早期冻结历史验收中，英文问题在 6 次理解里有 5 次被改写成中文；这是当时真实观测，当前版本是否仍复现 **NOT RUN**。同一验收中默认 Agent `top_k=10` 因工具消息预算失败，最终 6 轮成功使用 `top_k=3`；当前默认参数和当前问题复现情况须重新验证，不能把低 top_k 通过当默认已修复。
- 早期 6 轮中 3 轮完整回答包含当前引文未支持的补充内容；后续另一组 3 题真实 Qwen 冒烟也记录了一处引用指向不支持该结论的来源。这些是当时具体结果，不证明历史存储损坏，也不证明当前所有输出均有此问题。引用正确性和模型语言质量仍需逐题验证。
- 真实用户浏览器全量旧历史是否导入、正式服务是否已有这四张表及运行了哪版前后端、正式服务当前参数与历史窗口均待现场只读核验。不得把隔离侧库自动合并进正式数据库。

## 主代理现场补充：正式 ECS 尚未迁移

2026-11-14 22:36+08，主代理对正式 `/opt/amrag/backend/local.db` 以 SQLite `mode=ro` 查询 `sqlite_master`，并读取8200端口的真实OpenAPI：没有 `chat_sessions/chat_turns/chat_turn_references/chat_evidence_bodies`，也没有 `/chat/sessions`、`/chat/turns` 或历史导入路由。现有聊天接口只有无状态 `/api/v1/chat/completions` 与 `/api/v1/chat/rewrite`。

因此：**本地实现及此前隔离验证已完成，正式后端尚未迁移。** 不能把“源码上传到某个实验目录”当成正式接入。用户实际浏览器旧数据是否已显式导入仍为NOT_RUN；本轮不访问日常浏览器profile、不自动导入、不创建正式表。

这次正式API PID为778457，已不同于前轮728757；main.py、api/chat.py、services/llm.py、services/chat.py四文件也已发生其他工作的更新。主代理保留这些更改，本轮不覆盖或重启正式服务。原始只读回执：`var/hybrid-followup-20261114/followup-live-audit.json`。若新本地前端连接这版正式API，新会话路由尚不可用；前端不会静默转回浏览器写历史。
