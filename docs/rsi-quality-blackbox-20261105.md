# RSI 黑盒质量试点：六个候选的真实问答、代价与限制

日期：2026-11-05。本轮已完成基线与六个候选，8 题各重复两次，共 112 次真实用户问答、184 次业务 Provider 请求。**六个候选均由运行中的模型提出；下面的质量数字来自独立逐题 AI 复核，不冒充整批云端 Judge 成功，也不是负责人逐题确认。** 未启用候选、未提交推送、未切换正式服务。小批 Judge 核验另行记录。

## 1. 目标与实际改动

本次使用既有 RSI 控制器、不可变候选对象和谱系机制，对个人知识库产品进行有限黑盒质量比较。业务模型固定为 SiliconFlow 的 `Qwen/Qwen3.5-4B`。生成 Worker 只获得公开问题、历史、固定偏好及已授权的来源片段；Gold 由外部评估流程持有，不进入业务模型请求。

本次可执行候选范围如下。

| 候选范围 | 实际入口与行为 | 边界 |
| --- | --- | --- |
| Prompt | `prompt_addendum` 经真实 `LLMService` 工厂的可选属性，进入普通模式的最终 `build_grounded_prompt()` | 空增量保持原提示词；不改变证据选择、模型路由或答案校验。该钩子不适用于 Agent 提示词，Worker 拒绝 Agent 与非空该增量的组合。 |
| Memory | `memory_addendum` 附加在实际 `resolve_display_preferences()` 产生的表达策略之后 | 仅在固定显式偏好存在且产生有效策略时附加；不改偏好值、来源优先级、问题或检索参数。当前轮明确指令仍由产品原有解析流程处理。 |
| Workflow | 通过真实 HTTP 请求的 `mode=chat/agent`、`use_rewrite` 选择既有路径 | Agent 是含意图、工具选择、检索、评分与最终生成的组合流程；普通模式的 rewrite 控制不被既有 Agent 路径消费。 |

此处实现的是以上三类受限候选。此前 Tavily 联调、RSI 机制准备和其他对象讨论分别保留其证据，不能据此声称本次已经重写或验证全部五类对象，也不能把旧实验结果并入本次样本分母。

本次产品侧改动仅为新增 Worker 与普通最终提示词的可选纯函数钩子；未新增答案内容校验器。已有未提交产品改动已保留。相关文件：

- [产品 Worker](<D:/RAG knowledge/scripts/rsi_quality_worker.py>)
- [普通最终生成与纯函数钩子](<D:/RAG knowledge/backend/app/services/grounded_generation.py>)
- [Worker 定向测试](<D:/RAG knowledge/backend/tests/test_rsi_quality_worker.py>)
- [RSI 执行适配器](<E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_quality_execute.py>)
- [候选注册与谱系](<E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_quality_registry.py>)
- [试点控制流程](<E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_quality_loop.py>)

## 2. 真实执行链与隔离方式

每个 arm 使用一个独立进程，在内存 SQLite 中建立一次真实 BM25 索引，再通过 FastAPI `ASGITransport` 调用产品原有 `POST /api/v1/chat/completions`，读取真实 SSE 事件。链路为 HTTP DTO → ChatService → 服务端 KB 范围 → 产品 BM25/上下文构造 → 产品模型工厂 → 有界回环透传 → SiliconFlow → 产品 SSE。普通模式和 Agent 均复用产品实现，不通过导入旧实验驱动执行。

ASGI 应用仅挂载实际 chat router，不执行生产应用 lifespan。Worker 不打开生产数据库，不新建或复制 Chroma，不调用外部 Embedding、Rerank 或网页搜索，不复制整套语料、数据库或磁盘索引。冻结片段仅在进程内建种子；不同允许范围被映射为真实独立 KB，分别为 80、2、1 个片段，总计 83 条内存种子。相同范围复用同一 KB。

Worker 的默认 CLI 为预检，只有 `--live` 才允许真实调用。输出目录必须尚不存在，防止覆盖或静默续跑。每次真实请求先保留 `UNKNOWN` 占位，再记录原始工厂请求、实际发送正文、响应、Provider usage、finish reason 和耗时。发生 UNKNOWN 不重发；后续未运行记录继续保留在分母。单题截止时间为 180 秒，模型请求次数由传入上限硬限制。费用保留为 `UNKNOWN`，不能把缺失 usage 或账单当作零费用。

Worker 的模型密钥仅从继承的 `SILICONFLOW_API_KEY` 或 `M2_AB_API_KEY` 读取；产品配置初始化前关闭 dotenv。控制器在自己的私有配置边界内取得密钥后，仅通过子进程环境传递。落盘请求头使用允许列表，不保留 Authorization、Cookie 等敏感字段。

## 3. 冻结基线配置与数据

| 项目 | 实际基线配置 |
| --- | --- |
| 数据版本 | `rsi-quality-20261105-frozen-v2` |
| 问题与重复 | 8 个问题，每题 2 次，共 16 条计划记录 |
| 业务模型与 Provider | `Qwen/Qwen3.5-4B`；`https://api.siliconflow.cn/v1` |
| 模式与 rewrite | `chat`；`use_rewrite=false` |
| 检索 | `fulltext`、`top_k=10`、`context_window=0` |
| Rerank 与分数门槛 | `rerank_enabled=false`；`score_threshold=0` |
| 普通最终生成 | `temperature=0.7`、`max_tokens=4096`、`enable_thinking=false` |
| 固定偏好 | `answer_style=concise`、`knowledge_level=intermediate`、`output_preferences=[]`，均标记显式来源 |
| 当前轮覆盖 | `rsi-eval-01/02` 的当前轮详细要求解析为 `answer_style=detailed`、来源 `current_turn` |
| 基线增量 | Prompt、Memory 增量均为空 |

冻结资料及基线证据：

- [公开生成输入 v2](<D:/RAG knowledge/var/rsi-quality-20261105/freeze/inputs-v2.json>)，SHA256 `6069a6d72ae63ae324ccfa344bc7e86b0b5c523ccf469af0f49145b6adeaa904`。
- [独立评估资料](<D:/RAG knowledge/var/rsi-quality-20261105/freeze/evaluation.json>)，SHA256 `aa122746f234bedf28ef1d1a153a1af2868ab020eecaadd54a4d7d2b808903ac`；不进入业务 Worker。
- [基线 manifest](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/manifest.json>)，绑定产品文件指纹、固定配置与输入。
- [实际执行 job](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/execution/job.json>)。
- [逐题结果与原始 SSE](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/execution/product/results.json>)；同目录 `record-0001.json` 至 `record-0016.json` 与 `calls/` 保留具体请求、响应和 usage。
- [控制器基线汇总](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/worker.json>)。

基线完成后的只读审查确认：16 条记录的服务端 KB 范围、固定检索参数、模型请求参数和实际当前问题均匹配冻结输入；解析后的偏好策略均出现在实际最终 system message。全部返回引用均属于所选 KB，片段内容与冻结种子在去除空白差异后相同；重新按引用列表渲染的编号证据与实际提示词证据完全相等。所有行内数字引用均位于返回列表范围内。所有冻结 `backend/app` 文件及 Worker 指纹仍与基线 manifest 相同。

## 4. 执行命令与已获得结果

### 工程定向检查：PASS，模拟 Provider 与真实业务接线分开标注

实际工作目录为 `D:\RAG knowledge`。执行：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_rsi_quality_worker.py backend/tests/test_normal_grounded_answer.py backend/tests/test_grounded_candidate_snapshot.py -q 2>&1 | Tee-Object -FilePath 'var/rsi-quality-worker-dev-20261105/final-targeted-tests.log'; exit $LASTEXITCODE
```

退出码 `0`，`30 passed, 7 warnings in 31.25s`。警告为现有 Pydantic/LangChain 弃用提示。原始日志：[final-targeted-tests.log](<D:/RAG knowledge/var/rsi-quality-worker-dev-20261105/final-targeted-tests.log>)。

这些测试仅模拟外部 Provider 返回；普通/Agent 工厂、回环 HTTP 序列化、ChatService、真实 BM25、HTTP/SSE、偏好解析、输出隔离、EMPTY、UNKNOWN 不重试及保留计划分母均由相应真实代码执行。**这些测试不能替代真实云模型质量结果。未实现的命令不得报告通过。**

### 真实基线业务执行：RETURNED，质量验收待比较

以下为 `process.json` 保存的实际命令；目录已存在，不应原样再次执行：

```powershell
& 'D:\RAG knowledge\.venv\Scripts\python.exe' 'D:\RAG knowledge\scripts\rsi_quality_worker.py' --input 'E:\codex_workspace\2026-11-03\task\rsi-work\var\rsi-quality-20261105\baseline-live\baseline\execution\job.json' --output 'E:\codex_workspace\2026-11-03\task\rsi-work\var\rsi-quality-20261105\baseline-live\baseline\execution\product' --live
```

Worker 退出码 `0`，16/16 条 `RETURNED`，实际 Provider 请求 16 次。Provider 返回记录中的模型标识均为 `Qwen/Qwen3.5-4B`。`RETURNED` 表示请求有结果，不代表语义答案、引用支持或整体质量已通过。

| 基线指标 | 实际值 | 口径 |
| --- | --- | --- |
| 业务记录 / Provider 调用 | 16 / 16 | 8 题 × 2 次；保留完整分母 |
| 输入 Token | 37,850 | 实际 Provider usage 合计 |
| 输出 Token | 3,990 | 实际 Provider usage 合计 |
| 平均单题耗时 | 11.3543 秒 | 从问题 HTTP 请求开始到响应结束的 monotonic elapsed |
| P50 单题耗时 | 6.5133 秒 | 同一组 16 条问题耗时 |
| P95 单题耗时 | 29.7025 秒 | 同一组 16 条问题耗时；小样本描述值 |
| 费用 | UNKNOWN | 未取得确定账单，不按零计 |

### 基线评审异常与恢复

后台基线 Judge 请求使用 `gpt-6-luna`、`reasoning.effort=max`、`max_output_tokens=8192`。HTTP 返回 200，但实际 `response_status=incomplete`，`incomplete_details.reason=max_output_tokens`，输出 8192 Token 全部计为 reasoning；原始收据标记 `UNKNOWN`，控制流程按失败停止，日志中退出码为 `1`。这是评审失败，不能因为业务 Worker 退出 0 而报告评审成功。

原始 [Judge receipt](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/judge/receipt.json>)、[response](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live/baseline/judge/response.json>)、[基线控制日志](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/baseline-live-command.log>)均已保留。未重试该 UNKNOWN 请求。

随后使用已冻结 Gold、原始业务回答、实际交付的引用证据进行独立审查恢复；恢复新增模型调用 **0 次**。审查为 `AI_REVIEW`，不等同于原 Judge API 请求成功，也不等同于负责人确认。保留 [首次独立审查](<D:/RAG knowledge/var/rsi-quality-20261105/baseline-independent-review.json>) 与 [恢复审查](<D:/RAG knowledge/var/rsi-quality-20261105/baseline-recovery-review.json>)，包括原判与有依据的异议说明。

## 5. 指标、引用与已知限制

`final_context_chars/bytes` 仅统计普通最终提示词中实际编号资料文本：来自真实 prompt-builder 输入，随后验证其确实位于实际发送的 system message；包括编号、来源名和位置文字，不包括问题、偏好和主提示词。`prompt_messages_chars/bytes` 为整个实际 `messages` 数组的规范序列化长度；`request_chars/bytes` 则为包括模型参数等字段的完整实际请求正文。三者不得混用。Agent 的精确证据长度保留 `UNKNOWN`，仍可报告整个 messages 的实际长度。

普通回答中的 `[n]` 对应 `citations[n-1]` 及提示词同号资料块，不代表数字 chunk ID。KB 命名空间前缀用于隔离，原始来源映射须通过保存的 job/manifest 回查。Word/Excel 没有可靠页码时保留 `page_number=None` 与未知位置；本次未验证前端引用点击，也不把片段标识存在当作语义支持成立。

需随候选比较保留以下限制：

1. **Agent 温度与流程差异。** 既有 Agent 工具/最终调用默认温度为 `0.5`，意图为 `0.01`，评分为 `0.1`；普通 rewrite 为 `0.3`，普通最终生成为 `0.7`。透传不覆盖温度。Agent 是多因素流程比较，不能称所有 arm 的全部请求温度都固定 `0.7`，也不能把收益仅归因于单一提示词。
2. **扩展名缺失导致 native Excel 路径未覆盖。** 控制器以来源 `title` 作为 `FileDocument.name`，冻结标题没有 `.xlsx` 等扩展名。产品 `prepare_spreadsheet_grounding()` 按引用文件名扩展名筛选，因此本次未执行原生 Excel 补充证据与通常的工作表定位逻辑。当前测量范围为实际 BM25/文本 RAG。不得在基线之后改名再与旧基线混比；如需覆盖，应建立新版本。
3. **仅问题层面的留出。** DEV 与 evaluation 按问题划分，共享冻结材料；不是独立文档留出，也不能当作跨领域泛化结论。8 题两次重复只支持有限探索，不构成正式统计显著性或生产质量保证。
4. **AI 评审与成本不确定。** Gold 与审查均须保留来源、争议和负责人待确认状态。基线 Judge 的截断失败与 Token 消耗不能抹除；独立恢复不将历史 UNKNOWN 改写成成功，费用仍为 `UNKNOWN`。
5. **离线接线与真实服务分别验收。** 产品侧 30 项工程检查已执行；控制器最新定向检查 40 项通过，两者不与其内部子集累计。112 次真实云端问答已经完成；负责人验收和生产切换未执行。对所有回答均做了独立复核，云端 Judge 的结果另外报告。
6. **观测范围。** 本轮证据来自本地保存的真实 HTTP/SSE、模型请求／响应和候选账本；没有进行这批实验的 Langfuse Cloud 写入与回读验收，不声称存在已核验的 Cloud Trace。

## 6. 六个候选的实际结果

全部分母均为每组 16 条：4 道开发题、4 道未提供给改进 Agent 的评估题，各重复两次。空检索、保护性回复和未完成任务都没有从分母删除。`PASS / PARTIAL / FAIL / UNKNOWN` 分开统计；核心正确要求覆盖冻结 Gold 的语义要点，不要求逐字一致。全文忠实度检查补充主张，引用支持检查实际引用是否支撑相应说法。

统一 [逐题复核包](<D:/RAG knowledge/var/rsi-quality-20261105/complete-review-packet-v2.json>) 按问题汇总 Gold、完整冻结量表、实际有效偏好、定位、七组回答、引用、各维度理由、原始回执路径及哈希。JSON 已分行缩进，重复来源正文放在 `citation_catalog`，每条引用用 `content_sha256` 对应。原始账本保留，根会话的裁定另列 `adjudications`。v2 只补齐量表与有效偏好元数据，没有改变已有答案或评分；无任何已知样本的指标总计记为 null，不能把未知当零。

| 组别 | 唯一候选改动 | 核心 PASS / PARTIAL / FAIL / UNKNOWN | 完整核心答案率 | 忠实度 PASS / PARTIAL / FAIL / UNKNOWN | 引用支持 PASS / PARTIAL / FAIL / UNKNOWN |
| --- | --- | --- | ---: | --- | --- |
| 基线 | 原 Prompt、原偏好策略、普通模式 | 14 / 1 / 1 / 0 | 87.5% | 9 / 6 / 1 / 0 | 9 / 7 / 0 / 0 |
| 01 | 最短但完整的生成补充指令 | 14 / 2 / 0 / 0 | 87.5% | 10 / 6 / 0 / 0 | 7 / 9 / 0 / 0 |
| 02 | 主张与引用对应、限定证据范围 | 16 / 0 / 0 / 0 | 100% | 8 / 8 / 0 / 0 | 7 / 9 / 0 / 0 |
| 03 | 显式长度偏好及当前轮优先策略 | 16 / 0 / 0 / 0 | 100% | 8 / 8 / 0 / 0 | 4 / 12 / 0 / 0 |
| 04 | 语言／解释深度偏好的边界措辞 | 16 / 0 / 0 / 0 | 100% | 8 / 8 / 0 / 0 | 7 / 9 / 0 / 0 |
| 05 | 普通模式开启现有查询改写 | 12 / 0 / 4 / 0 | 75.0% | 14 / 2 / 0 / 0 | 10 / 2 / 0 / 4 |
| 06 | 使用现有 Agent 分支 | 13 / 1 / 2 / 0 | 81.25% | 7 / 5 / 2 / 2 | 5 / 9 / 0 / 2 |

**核心全覆盖不等于全文可靠。** 例如方案 02、03、04 都覆盖了本轮 Gold，但仍有 TAC 缩写扩写、无来源支持的流程推断、币种和年份补充。没有任何一个方案因此被自动宣布优于正式方案。方案 03 的一条回答开头误写“四季度”，后文明确写出“项目活跃的每个日历年季度”并覆盖全部必要点；根会话没有因这个孤立措辞把整个核心答案判错，保留其忠实性／表达问题与原始 PARTIAL 评语。

| 组别 | 开发集核心 PASS / 8 | 评估集核心 PASS / 8 | 平均秒 | P50 秒 | P95 秒 | 输入 Token 总计 | 输出 Token 总计 | Provider 次数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 基线 | 7 | 7 | 11.35 | 6.51 | 29.70 | 37,850 | 3,990 | 16 |
| 01 | 6 | 8 | 11.34 | 10.78 | 30.25 | 38,506 | 2,392 | 16 |
| 02 | 8 | 8 | 12.20 | 7.63 | 48.05 | 38,730 | 3,761 | 16 |
| 03 | 8 | 8 | 11.38 | 9.46 | 22.36 | 38,650 | 3,781 | 16 |
| 04 | 8 | 8 | 10.44 | 6.47 | 23.95 | 38,682 | 3,820 | 16 |
| 05 | 6 | 6 | 8.03 | 6.22 | 19.44 | 33,621 | 2,048 | 28 |
| 06 | 7 | 6 | 27.32 | 19.55 | 90.33 | 189,843 | 3,777 | 76 |

上述 Token 为全部阶段的实际 Provider usage，包含改写、工具选择、评分与生成；不只取 SSE `done.usage`。耗时为整次产品 HTTP/SSE 请求，包含失败或保护性回复。P95 使用实际样本的 nearest-rank 方法；每组仅 16 条，因此 P95 是该组最大值，不表示整体服务 P95。所有金额均为 **UNKNOWN**，没有账单依据不估算真实费用。

上下文与完整请求的变化：

- 基线及 01–04 的实际资料正文平均均为 **5,782.125 字符**，逐条相同。两个 Prompt 候选的补充指令在 32/32 个实际请求中出现；两种偏好策略也在 32/32 个请求中出现，且偏好值、来源优先级和资料正文保持一致。证据见 [Prompt 送达复核 v2](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/prompt-delivery-check-v2.json>)、[Memory 送达复核](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/memory-delivery-check.json>)。v1 读取了请求外层包络而非 `actual_request`，已明确标为无效检查，不作为产品失败或验收证据。
- 完整 `messages` 平均字符数从基线 **6,287.375** 增至 01 **6,525.375**、02 **6,617.375**、03 **6,568.375**、04 **6,595.375**。这是指令增量，并非资料上下文变长。
- 05 只有 12 次有实际生成资料正文，平均 **6,229 字符**；其余 4 次未生成，不能填零或与 16 次基线直接作等分母长度收益比较。
- Agent 的精确资料正文长度为 **UNKNOWN（16/16）**。可核对的完整模型 messages 为 14 次，平均 **19,776.714 字符**；另外两次没有可确认的最终回答请求。不能把完整消息长度当作纯证据长度。

具体可解释的取舍：

1. 01 的输出 Token 减少约 **40.1%**，但总 Token 只减少约 **2.3%**、平均耗时基本不变；两次季度流程回答均漏了必要频率。不能只凭输出短判成功。
2. 02 的核心覆盖从 14/16 到 16/16，平均耗时增加约 **7.5%**；引用和补充事实没有全面改善。它是值得继续验证的生成 Prompt 候选，但当前证据不支持自动启用。
3. 03、04 是偏好**表达策略**，没有改变 Redis 存储或新增长期事实记忆。虽然本轮核心答案均完整，仍存在无依据年份或解释。两次重复不能证明跨会话记忆学习；04 的平均耗时较低也不能据此声称稳定性能提升。
4. 05 的四次失败为季度流程题和下季度计划题的两次重复。实际改写将英文关键词换成纯中文，Fulltext/BM25 返回零条；计划题还把 `next quarter` 改成“本季度”。安全拒答与空检索一致，但端到端答案失败。更低耗时部分来自不再执行最终生成，不能算优化成功。该结论仅适用于此次 Fulltext，不外推为 Hybrid 的确定表现。
5. 06 的平均耗时约为基线 **2.41 倍**，输入 Token 约 **5.02 倍**、调用数 **4.75 倍**，核心完整答案反而少一条。出现正确人名附带证据未支持的别名、未调用工具而被拦下、以及目标季度混淆。原始模型中被产品压制的内容没有被当成用户实际看见的幻觉。

没有按本批结果连续修改候选直到得分变好。六个方案都是相同父代的独立候选，只运行一次实验批次、每题两次重复；未依据评估集答案改候选。

## 7. 版本、保留资源与下一步

产品检出仍在 `codex/pdf-parser-migration`；RSI 控制器检出为 `codex/rsi-receipt-decisions`（本次只读核对 HEAD `aeb2c89bc66d724472621c581107093d7dd2ad32`）。基线产品记录 HEAD 为 `57f9972b8477b0c4aaf7fac329b911420fafc95e`；真实运行指纹同时包含既有未提交代码，不能只凭该 SHA 重建完整运行版本。具体文件版本以保存的 `source_fingerprint` 为准。原有分支、未提交改动、冻结来源、Gold、原始结果、失败评审和 UNKNOWN 记录均保留。

修改普通生成文件前的 [快照](<D:/RAG knowledge/var/rsi-quality-worker-dev-20261105/grounded_generation.before.py>) SHA256 为 `1E40746C5C06C293034FAA64A550223CACE41B07C4AA7C82AB861D159DD7C8B5`；冻结 Worker SHA256 为 `494cc6dbd81bde6041be8e76d4e0e1435b2c9d0194f7095af733979cafbec290`。

复用了现有 Python 环境、冻结资料与片段；新建的是小型 job/manifest、独立运行输出、请求/响应与日志证据。进程内 SQLite 随进程结束释放，未保留磁盘数据库或新 Chroma；没有停止生产服务或清理其他任务资源。目录级清理前后磁盘总量未单独测量，不作节省空间数值声明。必要证据继续保留，不因试点结束自动删除。

当前没有生产模型/参数切换、自动晋升、验收 Tag 或发布动作。本轮只保留候选与证据。下一步首先解决自动 Judge 的批大小问题，并以相同证据范围复核其误判；若扩大 RAG 机制实验，优先记录“查询改写改变时间范围、丢失词法检索信号”这一真实反例，在明确的改写范围内另立候选，不悄悄修改当前正式 Prompt 或阈值。

## 8. 自动评分的实测故障、小批核验与已修补项

原基线 Judge 及六个候选 Judge 共 **7 次**均收到了 HTTP 200，但 `status=incomplete / reason=max_output_tokens`，没有返回评分。原基线为 8,192 输出 Token；候选批为 16,384，供应商回报这些输出全部为 reasoning。不是业务模型 Qwen 空回答，也没有证据把它归因为模型上下文容量不足。旧基线 receipt 的 `UNKNOWN` 原文保留，派生记录依据原 response 标注实际 `TRUNCATED`，没有重发。

控制器将未返回的评分保留为 UNKNOWN。整批流程的 `PILOT_COMPLETE`、退出码 0 只表示候选执行和归档完成，**不表示评分成功或质量通过**。程序基于当时可用的评分保留基线，经验文件仅为 `UNVALIDATED`。本报告的独立评分没有回填、覆盖那批云端 UNKNOWN。

另一次工程故障发生在诊断完成后：Agent 返回了有内容的嵌套 JSON 对象／对象列表，旧接线只接受文本，提前退出。现已支持结构化描述，并通过请求、response、模型身份及 usage 哈希绑定复用这次已成功的诊断，没有第二次收费调用。原目录 `candidates-live` 与新目录 `candidates-resumed` 均保留。

负责人额外批准两次后，使用 [固定单题计划](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/targeted-review-plan.json>) 只评 `rsi-eval-01`：基线两次回答、方案 02 两次回答，**不重新运行 Qwen**。这属于选中已知错误的定向 Judge 校准，不是新的独立留出验证。

两次请求均完成，输出有效评分：[小批结果](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/targeted-review/results.json>)。与独立复核的四条结果比较：

| 维度 | 四分类标签一致 | 主要差异 |
| --- | ---: | --- |
| 核心正确性 | 3/4 | 基线明确把主要期限说成“本季度”，Judge 判 PARTIAL，冻结量表要求错误主要主张判 FAIL。 |
| 全文忠实度 | 3/4 | 同一条时间矛盾被较轻处理。 |
| 引用支持性 | 4/4 | 均识别到正确核心引用之外存在未支持的扩写。 |
| 偏好遵从 | 0/4 | 本轮明示要求中文详细回答，却继续因长期 concise 偏好扣分。 |

仅看“完整正确／非完整正确”二分类，核心判断是 4/4 一致；这不能掩盖等级差异和偏好误判，也不能用一个已知核心错误样本宣称 Judge 已校准。完整差异见 [校准对照 JSON](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/judge-calibration-comparison.json>)。

**已确认并修复的评测输入缺陷：** 原接口将 `evaluation.json` 转成逐题 Gold 时丢掉了顶层冻结 `rubric`，也没有把实际产品捕获的有效偏好送给 Judge。`rsi-quality-judge/v2` 现保留并传入原始 rubric 一次，同时传入 `answer_style=detailed`、`source=current_turn` 等实际解析结果。未修改 Gold、原评分、正式 Prompt 或答案校验规则。历史小批请求对应 v1；v2 只完成离线真实材料检查与必要回归，**新的云端验证 NOT_RUN**，因为本轮已到 14 次角色调用上限。

## 9. 执行记录、调用账本与可复现入口

| 实际执行 | 退出码 / 关键结果 | 证据 |
| --- | --- | --- |
| 产品 Worker 与普通生成相关测试 | 0；30 passed | `D:\RAG knowledge\var\rsi-quality-worker-dev-20261105\final-targeted-tests.log` |
| 控制器／registry／旧循环定向测试 | 0；40 passed | 子代理实际 pytest 工具回执；范围为下列三个文件，未和之前的 21/31/36 子集相加。 |
| 基线真实调用 | Worker 0；控制器 1，Judge 截断 | `baseline-live-command.log` 与 `baseline-live/baseline` |
| 已有基线独立复核恢复 | 0；新增调用 0 | `baseline-recovery-command.log`、`baseline-recovered/baseline/source-failure.json` |
| 首次候选流程 | 1；有效诊断的结构化格式被误拒绝 | `candidates-live-command.log`、`candidates-live/diagnosis` |
| 复用诊断后的六候选流程 | 0；PILOT_COMPLETE；全部业务结果已保存 | `candidates-resumed-command.log`、`candidates-resumed/loop-result.json` |
| 单题评审模块必要测试 | 0；2 passed | `backend/tests/test_rsi_quality_review.py`，fake transport，不能代替真实模型结果。 |
| 两次小批真实 Judge | 0；REVIEW_COMPLETE；新增 Qwen 0 | `targeted-review-command.log`、`targeted-review/results.json` |
| v2 量表／有效偏好及受影响控制流程回归 | 0；19 passed；实际文件离线投影检查 0 | `evaluator-v2-targeted-tests.log`；只跑 quality_loop 与 quality_review。包含之前的三项选择测试、小批两项及与 40 项范围重叠的测试，不能相加。 |
| 统一逐题包与统计 | 0；112 条，七组各 16 | `review-packet-v2-command.log` 与报告第 6 节的 JSON |

上表未写绝对路径的运行证据均在 `E:\codex_workspace\2026-11-03\task\rsi-work\var\rsi-quality-20261105\`。工程测试使用替身，真实问答和小批 Judge 使用云端真实 Provider；两类结果分别报告。

本轮实际测试命令：

```powershell
# 工作目录 D:\RAG knowledge
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_rsi_quality_worker.py backend/tests/test_normal_grounded_answer.py backend/tests/test_grounded_candidate_snapshot.py -q

# 工作目录 E:\codex_workspace\2026-11-03\task\rsi-work
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_quality_loop.py backend/tests/test_rsi_quality_registry.py backend/tests/test_rsi_loop.py --disable-warnings --tb=short
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_quality_review.py --disable-warnings --tb=short
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_quality_loop.py backend/tests/test_rsi_quality_review.py --disable-warnings --tb=short
```

现在可复制的零调用检查命令（不会重复历史问答或创建新的付费请求）：

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_loop.py --config experiments/quality/pilot.json --out var/rsi-quality-preflight-only --preflight
```

真实六候选运行使用了现有 `run_rsi_loop.py` 的 `--quality-stage full --resume-evaluated-baseline ... --reuse-completed-roles ... --execute --approved-model-calls 10`。这是历史已执行参数，不是重复发送授权。后续可以在明确的新实验与额度内继续使用相同入口；单题评审模块只读取保存的回答，不是第二套 RAG 执行器。

[调用核算 JSON](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/call-accounting.json>) 核对了 **14 个唯一的角色 Provider ID**：

- Qwen 业务：184 次，415,882 输入 Token、23,569 输出 Token。
- OpenAI 角色：14 次，307,742 输入 Token、141,883 输出 Token，其中 136,985 为供应商返回的 reasoning Token。包括截断的消耗，没有因复用而重复累计 usage。
- 金额：UNKNOWN。没有账单，不把模型选择比例、Token 或“免费模型”的口头前提换算成真实账单。

运行依赖版本保存在 [runtime-versions.json](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/runtime-versions.json>)，没有安装／升级依赖。为了恢复已有未提交代码，本轮保存了仅 224,454 字节的 [产品源码快照](<E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-quality-20261105/frozen-product-source.zip>)，56 个源码文件全部与运行指纹相符，不含 env、数据文件或凭据。实际执行的控制器源码在 `controller-final-source`；评测 v2 的三份改动源码另存 `controller-evaluator-v2-source`。没有复制语料库或 Chroma 索引。

本轮新增或接入的主要文件为 D 项目的 `scripts/rsi_quality_worker.py`、普通生成的纯函数钩子与定向测试、`config/runtime-secrets.env.example` 的空白 SiliconFlow 配置项，以及 E 控制器项目的 `rsi_quality_registry.py`、`rsi_quality_execute.py`、`rsi_quality_loop.py`、`rsi_quality_review.py`、原 CLI/离线入口分派、`experiments/quality/pilot.json` 和对应测试。保留了双方原有未提交修改，没有自动提交／推送。实际密钥仍在被忽略的配置文件中，不在报告和源码快照中。

## 10. 本轮能关闭与不能关闭的结论

| 项目 | 结论 | 证据边界 |
| --- | --- | --- |
| 实际模型提出三类候选，程序登记、物化、执行、归档 | PASS | 真实 diagnosis/analysis/implementation 原始请求响应、六份候选 artifact、各自父代及 observation_id。不是开发会话预写候选。 |
| 黑盒问答与代价对照 | PASS | 112 次回答、184 次业务调用、逐题独立复核和实际请求捕获；仅本轮文本回放范围。 |
| 同一基线／同一成功角色复用、失败原文保留 | PASS | 无重跑基线、无重发成功诊断或历史不明请求，存在独立恢复目录和哈希核对。 |
| 每批 16 条的自动 Judge | FAIL | 七次输出预算耗尽，不能提供可信评分。 |
| 单题两重复的 Judge 接线 | PASS | 两次真实调用都有结构化结果；不等于 Judge 语义可靠。 |
| Judge 语义校准 | FAIL / 待复验 | 四条偏好判断全部误判，主要时间错误分级不一致；已修复输入契约，v2 实测 NOT_RUN。 |
| 自动经验晋级、自动生产替换 | NOT_RUN | 经验仅 UNVALIDATED，候选仅归档；没有启用任何方案。 |
| E1c 正式数据／正式业务效果 | BLOCKED / NOT_RUN | 本轮没有新材料或正式 E1c 实验，旧阻断没有被这些问答结果抹除。 |

程序已经能完成一轮受控候选实验并在评分不足时保留基线，但**尚不能称为无需人工复核的可信全自动 RSI**。首要后续工作是用已保存答案验证 v2 Judge，避免重做问答；其次才在可靠评分前提下扩大候选样本或派生新一轮。六个候选均不自动投入正式服务。
