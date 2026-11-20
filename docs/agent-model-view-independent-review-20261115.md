# Agent 模型视图：独立离线回放审查

日期：2026-11-15。范围为已保存的一次真实应用层生成输入，使用当前本地候选源码；不启动 ECS、不联网、不调用模型，也不修改原始回执或知识库。该审查不判断答案正确性、Citation Precision、模型 Token 数、实际线上延迟或费用。

## 证据与结果

- 使用 `scripts/replay_agent_model_view.py` 恢复失败记录中实际 `actual_application_messages` 和工具 schema；用 `messages_from_dict` 恢复后与保存的消息逐字段一致。输入为既有回执，不重写 Prompt、不引入 Gold 或根据题号选择正文。
- 唯一被测工具结果包含 9 项文本来源。模型请求应用层 JSON 从 **42,359 字符 / 43,967 UTF-8 字节**变为 **17,969 字符 / 19,421 字节**；32,000 字符原有限额下，原输入超限，候选视图通过原有 `enforce_request_budget`。这是一个输入的结构回放，不是新生成结果。
- 完整 canonical context 逐字相同；9 个已选正文仍位于 context；`source_id` 顺序、已检查的 location/page/content_type、可读标题、消息角色与 ID、非工具消息和工具 schema 保持一致。调用前的原始消息、归档引用及构造的 registry 代理未被修改。
- 本次被检查的 9 个旧归档 `qa500_import.source_label` 不进入模型视图，但原始后端工具消息仍保留；项目负责人已将该字段认定为整理标记。该结论不表示所有来源元数据都没有业务含义。
- registry 是从已保存工具消息中的完整 citation 值重建的**归档代理**，不是独立读取当时运行态 `state.source_registry`，不能仅凭此证明真实运行态 registry 匹配或数据库当前身份有效。实时隔离探针需要另行捕获实际 registry、投影视图和实际 SDK 请求核验。
- 已知位置字段通过核对。此前对 `source_metadata` 顶层通用键可能被误删的风险已针对候选代码修正：定向反例核对直接嵌入的 `id`、`score`、`generation`、`members` 等业务值以及未知嵌套对象；最终 90 项定向测试通过。未知来源的全部语义仍不能只凭一例和合成反例完全证明。

## 执行与可复核入口

```powershell
.\.venv\Scripts\python.exe scripts/replay_agent_model_view.py --backend-root backend --receipt var/hybrid-followup-20261114/followup/current-receipts/68d4bd03546448a5b334a7dcb18d07a9.json --out var/agent-model-view-20261114/saved-input-replay.json
```

实际退出码 **0**；结果 `status=PASS`、`budget.status=PASS`、`network_attempts=0`。脚本在导入与执行前阻断 socket 网络连接；PASS 只针对 JSON 列明的检查项。最终结果：[saved-input-replay.json](../var/agent-model-view-20261114/saved-input-replay.json)，SHA256 `9fb45833d52295e22c9c84c0fa31f450feaba4dc2655f5cc51da066943f7573b`；对应投影源码 SHA256 `3ee53e4b13646c86cc31bc4a191b0d0224abb51dc99e5b73b7f41b5bb1b4f68b`。脚本 SHA256 `67b447b2f745c61943595303a6b5194800c2bdaeb18124b673646c6018dbca1f`。早期 v1/v2 回放文件仍保留，不覆盖。

独立审查曾发现对通用键的递归移除可能误删语义字段；实现负责人随后收紧过滤边界并加入顶层及嵌套反例。最终定向测试 **90 passed、0 failed/errors/skipped**，原始记录为 [semantic-boundary-tests.log](../var/agent-model-view-20261114/semantic-boundary-tests.log) 和对应 JUnit 文件。最终回放基于上述最终源码哈希；后续真实隔离运行见下节，不能视为正式部署或线上验收。

## 三次 ECS 隔离真实 Agent 回执复核

以下复核依据 `var/agent-model-view-20261114/model-view/live-agent/` 的真实 SSE 回答、同 ID 的 `receipts/` 及开发集 Gold；不是正式部署或大样本质量结论。三次均为 Agent、`use_rewrite=false`、最终状态 COMPLETED；因未开启改写，不能据此判断查询改写语言是否修复。

**模型视图与来源身份：**实际运行态 registry 各有 3、9、6 项，共 18 项；逐项核对对应原始工具 citation 与 registry 完全相等，全部正文存在于投影 context；SSE 来源的 `source_id`、`chunk_id`、正文与 registry 对应；最终实际网络调用中的工具消息与投影模型视图相等。三份记录均报告 registry 和原始工具消息投影前后不变。该结果证明本次所捕获链路内的编号、正文和模型请求一致，不等同于各句事实均有引文支持，也不替代当前源数据库独立身份核验。

| 开发题 | 最终请求字符数（投影前→后） | 回答与引用复核 |
|---|---:|---|
| `qst_0032` | 31,375→16,660 | 默认 30 分钟、理由充分可延至 1 小时与 Gold 一致；`[1]` 的实际正文直接支持两项数值。 |
| `qst_0017` | 43,105→17,983 | 2.7→1.9、下降 0.8 与 Gold 一致；`[2]` 支持分数，`[1]` 支持版本变更。原请求超 32,000，投影后通过现有限额。 |
| `terra-ecs-b1-024` | 23,851→12,368 | 两个位置的核心回答与 Gold 一致；**回答没有任何数字行内引用**。其所说的“文章2”是来源正文内部的文章序号，不是系统来源 `[2]`。包含相关资料的是系统来源 `[1]`；其中“端口”细节来自同一正文的文章1，文章2本身只写打印机属性。不能将来源映射 VERIFIED 当成这条回答的引用通过。 |

`qst_0032` 的 SSE 回执为 [0aa17a4d766b4cbd8d1dc33965a5f839.json](../var/agent-model-view-20261114/model-view/live-agent/0aa17a4d766b4cbd8d1dc33965a5f839.json)，模型与 registry 审计为 [对应审计](../var/agent-model-view-20261114/model-view/receipts/0aa17a4d766b4cbd8d1dc33965a5f839.json)；`qst_0017` 为 [SSE](../var/agent-model-view-20261114/model-view/live-agent/52768093e4be40a68868fa0c5d8eb8ae.json) 和 [审计](../var/agent-model-view-20261114/model-view/receipts/52768093e4be40a68868fa0c5d8eb8ae.json)；打印机题为 [SSE](../var/agent-model-view-20261114/model-view/live-agent/5d710d28769d4c04bc5b6ea02968fa32.json) 和 [审计](../var/agent-model-view-20261114/model-view/receipts/5d710d28769d4c04bc5b6ea02968fa32.json)。三题只是开发样本冒烟；回答无引用的问题需要明确保留，不能通过修改输出检查或 Prompt 假称已解决。
