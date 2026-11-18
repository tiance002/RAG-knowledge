# RSI Token Budget 审计与修复

## 结论

三个 Luna max Proposer 都耗完了 **4,096 个总输出 Token**。回执中 4,096 个全部记为 reasoning，最终可见输出为 0。三个请求的 HTTP 响应均为 200，响应 JSON 已完整保存。Responses API 返回 `status=incomplete` 和 `incomplete_details.reason=max_output_tokens`，没有 `finish_reason` 字段。三个响应均只有 reasoning 项，从未生成最终 JSON、完整 Patch 或进入 AST 检查。分类为 **A，总输出预算耗尽**，该额度全部被推理用掉，并无另一个独立推理上限的证据。

| 角色 | 模型／推理 | 请求上限 | 输入／总输出 | 推理 | 供应商终止 | 截断 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| Diagnostician | Luna／none | 1,024 | 1,724／113 | 0 | completed，契约内弃权 | 否 |
| P1_DEDUP | Luna／max | 4,096 | 1,747／4,096 | 4,096 | incomplete，max_output_tokens | 是 |
| P2_BUDGET | Luna／max | 4,096 | 1,749／4,096 | 4,096 | incomplete，max_output_tokens | 是 |
| P3_DEPENDENCY | Luna／max | 4,096 | 1,758／4,096 | 4,096 | incomplete，max_output_tokens | 是 |
| Curator | Luna／none | 1,024 | 1,438／55 | 0 | completed，契约内弃权 | 否 |
| Meta | Luna／none | 1,024 | 1,115／332 | 0 | completed，契约通过 | 否 |
| Format Repair | 本轮禁用 | 无请求 | N/A | N/A | N/A | N/A |

前六行来自本轮实际 wire payload 与 provider receipt。可见输出只有在无可见内容时可确认 0。其余角色没有独立可见 Token 计数。总输出减推理是可见输出的上界，因为供应商可能另外计入格式 Token。[逐角色回执路径、哈希和原始账本观测](evidence/token-budget-audit.json)。

## 调用链核对

| 路径 | 请求和限制 | 响应、费用及失败归属 |
| --- | --- | --- |
| 六角色 `role_profiles.py` → `openai_roles.py` → `http_worker.py` → OpenAI Responses | Profile 冻结角色映射，实际请求按角色绑定 `max_output_tokens`、`reasoning.effort` 和请求哈希。HTTP 子进程写入原始回执 | Budget 先按实际总输出结算，再写 Invocation 终态并进入契约判定。三个历史截断账本都有已知用量，历史 Observation 将其归为 CONTRACT_ERROR |
| `rsi_roles.py`、`rsi_quality_loop.py` → OpenAI Responses | 各自使用旧配置的 `max_output_tokens`，quality 入口默认 8,192，旧实现没有复用六角色额度 | 统一识别输出状态，保留各入口的历史状态字段。quality 的旧可选恢复只在授权配置、确认截断、费用用量已知且提升上限的 Judge 路径触发 |
| `rsi_proposal_agent.py` → `rsi_siliconflow_transport.py` → Chat Completions | 旧单 Proposer 发送 `max_tokens=1200`，关闭 thinking。这是配置证据 | `run_rsi_loop.py` 解析 `choices[].finish_reason` 与 Chat usage，先记原始回执和终态，再判断能否进 Patch |
| 配对 QA 生成与 RAG worker | `qa_validation.py` 的外部 generator/评分回调，以及质量 pilot 的回答 worker，使用各自协议 | 属于独立评测或业务回放输出。本次只审查边界，没有改写其请求、Gold、评价契约或统计硬门 |

历史六角色的 4,096 来自独立 DEV_DEBUG 配置。正式旧默认是 Diagnosis 1,024、Proposer 3,072、Curator 和 Meta 2,048。旧 `OpenAIRoles` 允许一次已知用量的 Format Repair，沿用各角色原上限。它曾把供应商截断也送入普通契约修复。DEV_DEBUG 已批准轮只有一次尝试，当时没有发生 Format Repair。

## 版本化预算和终态

| Profile | Diagnosis | Proposer | Curator | Meta | Format Repair |
| --- | ---: | ---: | ---: | ---: | --- |
| `rsi-token-budget/v1-legacy` | 1,024 | 3,072 | 2,048 | 2,048 | 各角色沿用同上 |
| `rsi-token-budget/v1-dev-luna-max-4096` | 1,024 | 4,096 | 1,024 | 1,024 | 禁用 |
| `rsi-token-budget/v2-luna-max`，待验证 | 8,192 | 16,384 | 8,192 | 8,192 | Proposer 8,192，其余 4,096 |

Luna 的公开上下文窗口为 1,050,000，模型最大输出为 128,000 Token。Responses 的 `max_output_tokens` 同时约束推理和可见输出。独立推理上限没有公开请求参数，本实现只发送该 API 支持的字段。新 Profile 冻结角色额度及端点语义，要求指定 Standard 层和预检 guard。现行正式默认额度保持旧版本。[Luna 模型限制](https://developers.openai.com/api/docs/models/gpt-6-luna) · [Token 计数语义](https://developers.openai.com/api/docs/guides/token-counting)。

终态根据供应商证据与本地契约区分 `OUTPUT_TRUNCATED`、`INCOMPLETE_STRUCTURED_OUTPUT`、`INCOMPLETE_PATCH`、`MODEL_STOPPED`、`API_ERROR`、`TIMEOUT`、`UNKNOWN`。没有利用率阈值判定截断。确认截断会保留原始响应、记录实际费用，跳过普通 Format Repair 和静态回放。在固定三槽裁决中该槽保持 `INCONCLUSIVE`，不写成候选策略失败。普通格式修复仅处理收到且用量确认的非截断输出，使用独立 attempt 和新请求哈希。

## 单请求验证预演

只恢复历史 P1，保留模型、max 推理、原公开输入和 Schema，把输出上限从 4,096 改为 16,384。生成新的完整 JSON，避免拼接不完整 Patch 或向下一次请求转发 reasoning。计划将请求与原有用量回执绑定，产生新请求 ID，并记录批准前的账本前缀哈希。独立恢复最多一次、零重试，达到原累计 USD 5 或新单请求上限时停止。

- 按实际预演请求体和 Luna Standard 保守缓存写入费率，该请求上界 **USD 0.009192625**。拟议单次授权上限为 **USD 0.011**。
- 验收依次核对原始响应终态，实际推理与总输出，最终 JSON 和 Patch 结构，AST 与签名。只有完整且静态合格的 Patch 才做 DEV14 复跑。
- 若仍出现截断，保留失败与费用后停止。更小的结构化输出、分段 Schema 或安全续写需要新的契约设计，不能将两段不完整内容合并成已验证 Patch。

审计期间发送模型请求 **0**。原账本保持 680 事件、累计上界 USD 1.377789290，审计前后 SHA256 均为 `6b9bbace89243236d7a92467b9a588ad37d6033d8b1c7b1ed8829c7bd41ec5db`。保护文件 6/6 匹配，历史原始请求、响应和观测库保持原件。当前预演等待另行批准。
