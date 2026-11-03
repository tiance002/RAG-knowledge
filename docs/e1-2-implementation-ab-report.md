# E1.2 Langfuse 原生化实施与 A/B 验收报告

日期：2026-10-29。范围：第二阶段批准的混合方案 B。状态：**隔离合成资料与真实 HTTP/SSE 应用链路验收 PASS；真实外部模型、真实媒体 Cloud 上传及正式服务上线 NOT RUN。**

本报告中的模型是可计数的替身，应用、FastAPI ASGI/SSE、LangGraph、Langfuse SDK 和 Cloud API 为真实组件。没有用替身回答证明真实模型正确率，也没有新增或重发历史 UNKNOWN 请求。所有原始回读、日志、快照及失败证据保存在忽略提交的 `var/e12-20261029/phase2/`，本报告不包含私有原始正文或凭据。

## 1. 版本、环境与实施范围

- 本地仓库：`D:\RAG knowledge`，分支 `codex/pdf-parser-migration`；HEAD `722f000bd874d1fa17bfe94385612f045218f74e`。E1.2 修改尚未提交、推送；其他会话的未提交文件未覆盖。
- E1.1 A/B 基线使用该 HEAD 的独立源码归档，不是仅在新代码中切换旧开关。
- R4 是完整本地矩阵及六条 SSE Cloud 验收版本，应用源码哈希 `fd84b8d07678d52a84907b763b9c34b0936f78e359f1899a737c9ab7aa7cace2`；归档 SHA256 `1b11ee53001d99e41e2cc0e34d2b4125f564ab2a9c3f242e5ed2c872b3cd3254`。
- R5 是最终工作区版本，R4→R5 运行代码仅将观测初始化锁从 `Lock` 改为 `RLock`，并增加构造失败不死锁测试；应用源码哈希 `2d090f6878efb4bf7c6fd4aa447bd9b1c65c6a8f354843d1be03d21054e727fd`；归档 SHA256 `97d00f6d6bc37de02cbd03193e792b1338a7e7523079c7a1b1dcdbfd8d4cfdbc`。最终两条 Cloud 冒烟独立记录，不能冒称完整矩阵在 R5 重跑。
- 使用 ECS 已有隔离环境：Python 3.12.3、Langfuse 5.0.4、OpenTelemetry 1.45.1、LangChain 1.2.0、langchain-core 1.2.2、LangGraph 1.0.5、OpenAI 1.1.5。未升级全局或正式环境依赖。
- ECS 目录 `/opt/amrag/experiments/e12-20261029` 只增量保存代码和合成证据；SQLite fixture 在内存，没有复制历史知识库、PDF、向量库或生产数据库。复用既有虚拟环境，没有再建一套虚拟环境。
- 未修改检索算法、权重、Rerank/Agent 阈值、解析路线、模型默认值和业务 Prompt；未重启或切换正式服务；未启用 Prometheus/Grafana。

R4 的 50 个应用文件在工作区、归档和 ECS 间逐文件核对通过：`phase2/code-identity-r4.json`。R5 清单见 `phase2/staging-r5.json`。所有哈希用于区分未提交源码版本，不以 HEAD 代替实际工作区身份。

## 2. 实际源码改动

|文件|主要函数/位置|变更及原因|
|---|---|---|
|`backend/app/services/observability.py`|`_sdk`、`span`、`event`、`candidates`、`generation`、`tool_observation`、`langchain_callback_config`|唯一 SDK/TracerProvider；模型调用生命周期；原生类型；阶段合并；真实父子归属；受控 Callback；脱敏/过滤；初始化失败不阻断业务|
|`backend/app/services/trace_schema.py`|结构化字段许可集|保留可读的受控 action、decision、next_action，保留必要身份及阶段信息|
|`backend/app/services/llm.py`|Provider 调用和流式生成封装|Generation 包围实际调用；记录实际消息、模型参数、返回、usage、finish reason；流式取消/部分结果；直接 SDK 与 Callback 避免双计|
|`backend/app/services/grounded_generation.py`|Provider HTTP 调用处|传入实际 messages 和 model_parameters，避免从检索候选重建假 Prompt|
|`backend/app/services/agent_workflow.py`|模型及工具执行处|传递官方 Callback 上下文；保留实际 Agent 调用次数和工具父子关系；不改变 Agent 决策|
|`backend/app/services/retrieval.py`|检索入口及业务阶段|原生 Retriever 操作；保留有序候选、融合、去重、排序和 Top-K 的结构化阶段|
|`backend/app/services/rerank.py`|批次执行及异常分数记录|真实 Rerank 操作；失败候选观测原始分数来源，不将回退值伪装成模型 Rerank 分数|
|`backend/app/services/chat.py`|问答根、上下文构建与 SSE 结束|根身份/实际输入输出；Context 生命周期及最终实际送达；保留异常和 SSE 终态；未改变 original_content 替换行为|

新增三个定向测试文件：`test_e12_native_observability.py`、`test_e12_model_integration.py`、`test_e12_acceptance_harness.py`；新增可复用入口 `scripts/run_e12_acceptance.py`。未创建独立 Trace 前端、数据库或媒体平台。

开关：`RAG_TRACE_SCHEMA=e12` 选择本次实现；未设置仍走既有 e11。正文策略默认 `RAG_E12_CONTENT_POLICY=LOCAL_ONLY`。`INLINE` 只对可信 EVAL manifest 明确授权的安全文本开放，普通请求不能通过自报 metadata 提权。

## 3. 最终 Trace 树与节点映射

普通 Fulltext 的实测树为：

```text
qa.request                         CHAIN
├─ retrieval.search                RETRIEVER
│  └─ retrieval.bm25               RETRIEVER
├─ context.build                   CHAIN
└─ answer.generate                 GENERATION
```

本次 Hybrid + Rerank 合成场景为 5 个 Observation：根、retrieval.search、rerank.batch、context.build、answer.generate。Vector/BM25/融合详情保存在检索阶段数组。这个数字由实际执行路径决定，不是强行满足设计预测的 5 节点目标。

两轮 Agent 实测 21 个 Observation，其中 4 个 Generation、2 个真实工具执行节点。包括规则意图分析、3 次 Agent 模型决策、2 次工具/检索、输出处理和 1 次实际质量模型调用。首轮质量判断未调用模型，不虚增 Generation。

|E1.1 记录|E1.2 归属|保留的诊断信息|
|---|---|---|
|模型 response 后瞬时事件/零散元数据|实际调用外层唯一 Generation|真实开始结束、输入输出、参数、状态和可确认 usage|
|融合、归一化、去重、排序、Top-K 的独立小节点|Retriever 内有序 `metadata.stages`|候选 ID、顺序、分数、阈值、移除原因和每次变化|
|候选各节点重复正文|源内容描述及稳定 `content_ref`|源内容 Hash、定位、可回读引用；引用不重新哈希破坏身份|
|选中、扩展、构建、送达的零散记录|Context 节点及其阶段|选中块、父/邻块变化、最终消息实际内容身份|
|手动模型/工具重复包裹|官方 Callback 或手动原生 Observation 二选一|每个实际模型/工具执行唯一权威节点|

`score_kind/score_value` 已修正：成功 Rerank 对应实际 rerank_score；失败批次候选为 original_retrieval，rerank_score=null，并保留 fallback_score_origin。业务排序和返回未因此改变。

授权纯文本 LangChain 路径使用官方 CallbackHandler，复制现有 AsyncCallbackManager 后增加 handler，保留既有业务配置/handler；`run_inline=True` 保证工具与内部 Retriever 共享上下文。LOCAL_ONLY、缺少授权或不安全媒体路径采用原生手动 Observation，并记录回退原因。未叠加 OpenInference 自动 instrumentation。

## 4. 本地同条件 A/B 与业务等价性

完整对照：旧 HEAD 的 E1.1、新 R4 E1.2、关闭观测三种。八个场景全部满足业务等价：实际回答哈希、引用、Provider 请求哈希、规范化 Agent role/content/tool-args、调用次数和 transport_state 对应一致。证据：`phase2/final-local-ab.json`。没有只比较最终回答就宣称 Prompt 相同。

|场景|E1.1 Observation / Generation|E1.2 Observation / Generation|实际替身模型调用|E1.2 Tool|
|---|---:|---:|---:|---:|
|Fulltext|26 / 1|5 / 1|1|0|
|Hybrid + Rerank|46 / 1|5 / 1|1，另 Rerank 1 次|0|
|Agent 两轮|63 / 4|21 / 4|4|2|
|UNKNOWN|23 / 0|5 / 1|1|0|
|空答案|26 / 1|5 / 1|1|0|
|截断|26 / 1|5 / 1|1|0|
|无结果|13 / 0|4 / 0|0|0|
|取消|22 / 0|5 / 1|1|0|

UNKNOWN/取消的旧版无 Generation 是旧生命周期漏记，不能解释为未发出模型请求。新版开始调用就创建 Generation，异常结束仍保留。空答案和截断保留现有 EXTRACTIVE_FALLBACK；本轮没有把回退当作模型成功。

附加真实操作探针执行现有 `deduplicate_results`、`expand_context`、ChatService：20 候选去重为 7；父/邻块目标字段在扩展后出现，但被既有 original_content 替换后未进入实际模型请求。三种模式去重 ID、扩展内容哈希、实际请求和 SSE 哈希一致，证明可以定位“扩展恢复→最终送达丢失”，不把扩展成功算作 Gold 已送达。证据：`phase2/closure/actual-ops-{e11,e12,off}/result.json`。

本地应用段耗时，单位 ms，每格只有 **1 次样本**，使用内存 exporter，不含导入/启动：

|场景|E1.1|E1.2|关闭观测|
|---|---:|---:|---:|
|Fulltext|56.669|59.771|20.029|
|Hybrid + Rerank|59.934|59.153|21.307|
|Agent 两轮|139.440|158.391|156.076|
|UNKNOWN|45.757|47.276|20.047|
|空答案|51.928|54.546|20.138|
|截断|54.647|54.474|19.989|
|无结果|31.005|32.055|16.292|
|取消|44.363|48.345|19.530|

不据此报告 P95、生产延迟改善或稳定的性能差异。Cloud 冒烟包括 SDK 初始化和 flush，不能与本表混为同一种延迟。现有 flush 行为保留，未证明批处理减少了真实 HTTP 请求量。

## 5. Cloud A/B：Observation、字节和 Units

同一 LOCAL_ONLY 策略，完整导出的 E1.1 R2 与 E1.2 R4 对照：

|场景|Observation|Generation|序列化字节 E1.1→E1.2|字节下降|对象口径 Units E1.1→E1.2|
|---|---:|---:|---:|---:|---:|
|Fulltext|26→5|1→1|36,646→32,219|12.08%|27→6|
|Hybrid + Rerank|46→5|1→1|64,294→50,620|21.27%|47→6|
|Agent 两轮|63→21|4→4|77,966→72,197|7.40%|64→22|

**字节定义**：Cloud API 回读每个 Observation 的 `{input,output,metadata}`，按 UTF-8、排序键、紧凑 JSON 序列化后求和。不是 OTLP 传输流量、压缩后网络字节或服务端实际占用。

**Units 定义**：按回读到的 `1 trace + Observation + Score` 对象计数；各 Trace 的 Score 均完整分页确认 0。这是可复核的对象口径，不是账单导出，不声称货币费用下降。真实 Token/费用：本轮替身没有供应商 usage，UNKNOWN/NOT AVAILABLE，不能当作真实账单为零。

可确认的重复快照引用字节下界：Fulltext 448→384，Hybrid 1,024→384，Agent 320→832。Agent 此项增加，因此不能宣称所有冗余均下降；真实多轮消息历史也不能作为无意义重复删掉。此下界不是全部正文冗余的精确值。

旧版 UNKNOWN Trace 云端只收到 1 个节点，而本地有 23 个，日志明确为 exporter 超时/重试耗尽/关闭。保留原始失败，未重发请求；该样本不进入字节收益表。新版 UNKNOWN Cloud 的 5 节点完整。

## 6. Cloud 回读、内容与身份交叉核验

项目：US Cloud，`cmuido3u800zyad0con8gaqw1`。以下链接需项目访问权限。

|R4 场景|Trace|Cloud 实际数量|状态|
|---|---|---|---|
|普通 RAG / LOCAL_ONLY|[c94d4829…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/c94d4829b18b5d538c05e22d43b7039d)|5 Observation / 1 Generation|PASS|
|Hybrid + Rerank|[98337fbe…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/98337fbefb647b466b13dc3c2dc1f8ed)|5 / 1|PASS|
|两轮 Agent / LOCAL_ONLY|[14dc042e…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/14dc042e413ed998ed7972a962b20519)|21 / 4，2 Tool|PASS|
|UNKNOWN|[162cf9c5…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/162cf9c5f953867e63812e68df3f05bc)|5 / 1|PASS，业务为 UNKNOWN|
|普通 RAG / 授权 INLINE|[cc1e7e56…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/cc1e7e56832428d7e1db18cf9e64c0f7)|5 / 1|PASS|
|两轮 Agent / 授权官方 Callback|[0ceeaf33…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/0ceeaf33d8953f1052180d47be346696)|21 / 4，2 Tool|PASS|

证据目录：`phase2/readback-r4-*`，官方 Agent 使用 `readback-r4-agent_two_rounds-inline-v3/summary.json`。检查唯一根、所有父节点存在、同一 trace、实际 SDK parent 与本地 descriptor parent、Generation 数量、实际上下文/引用身份、根回答哈希和有序候选阶段。LangChain 消息按实际 role/content/tool-args 做 **PASS_CONTENT_PROJECTION**，不是谎称框架不同消息序列化字节相同。最初严格比较的失败回读与 v2/v3 均保留。

300 候选 Cloud 压力探针不是答案质量评测：[46571d29…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/46571d2944d1d735227c58b71624a8f3)。Cloud 2 个 Observation 中 300 个候选 ID/顺序/分数、299 个移除 ID/原因/顺序及 Top-1 全部对应本地，无字段裁剪；`phase2/bulk-cloud-readback/summary.json` PASS。

LOCAL_ONLY 并不意味着只在本地留数组：Cloud 保留诊断用结构化候选和内容 Hash/引用。36 个本轮受控快照引用经现有 CLI 回读，退出码均 0，哈希全部一致；`phase2/snapshot-cli-cloud-r4.json`。未授权正文、密钥和隐藏推理未进入 Cloud；授权 INLINE 输入输出可直接回读。

历史身份只读复核：2 个旧问题的 12 个 Score 的 ID、value、来源、rubric/reviewer/subject 均未变，`phase2/historical-scores-verify.json`。旧 Cloud 根未保存可独立证实的 answer hash，关联依据是不可变本地 case 与 Cloud root identity，`cloud_answer_hash_confirmed=false`；不把此项夸大为 Cloud 旧正文哈希已核对。未伪造新的 Dataset/Experiment Run；没有既有链接的仍保留 NOT_LINKED。

UI：已在获准合成资料的 R2 INLINE Trace 中实际看到原生树、根输入及 “The sample value is 42. [1]” 输出；满足尝试用户可读 UI 检查。最终 R4/R5 UI 自动化受浏览器工具超时影响，未继续反复尝试，标为 BLOCKED；最终字段核验以 Cloud API 为准。没有伪造截图。

R5 最终普通 Trace：[cfd95dac…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/cfd95dacf2859e836336830c71de451f)；官方 Agent Trace：[929c7cb9…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/929c7cb9bd4de70a0e6cabb75b4ffb12)。两次运行退出码 0，实际外部模型请求 0；最终 API 回读结果见本报告后附的 R5 闭环记录。

## 7. 生命周期、安全、采样与失败验收

|验收项|结论|边界与证据|
|---|---|---|
|直接 Provider SDK 唯一 Generation|PASS|真实 OpenAI ChatCompletion 对象替身；实际调用外层起止；12 输入/7 输出/19 总 Token fixture 映射正确，不代表真实供应商账单|
|LangChain 官方 Callback 唯一 Generation/Tool|PASS|授权文本四次模型、两次工具；无手动重复 Generation；工具内 Retriever 正确归属|
|UNKNOWN 不重复调用|PASS|直接 SDK、官方 Callback timeout 均一次调用，结束 UNKNOWN；未重试历史 UNKNOWN|
|流式中断/取消|PASS|真实 ASGI http.disconnect；根及 Generation 为 CANCELLED，保留部分输出，不伪造 done 成功|
|观测关闭业务一致|PASS|八场景回答、引用、实际请求和计数一致|
|导出失败不阻断业务|PASS（业务）/FAIL（遥测，预期）|受控 exporter 返回 FAILURE，回答完成，一次替身调用；记录 root_not_unique/generation_count 错误，没有把丢 Trace 标 PASS|
|SDK 初始化失败不死锁|PASS|R5 实际 SDK constructor 抛错，线程在约定时间内返回，业务继续；修复原 Lock 重入问题|
|0%/100% 采样边界|PASS（真实 SDK 内存导出）|显式唯一 TracerProvider + TraceIdRatioBased；0→0 节点、1→2 节点；没有修改生产采样率|
|跨 Callback/手动上下文|PASS|真实 StateGraph 与 Cloud 两轮工具链；无孤立或重复节点|
|Masking/Filtering|PASS（定向合成资料）|组合官方默认过滤与隐私限制；SDK 处理媒体之前拦截未授权内容；正文策略不能由普通请求提升|
|媒体引用|部分 PASS / Cloud NOT RUN|合成 PNG bytes/dataURI 得相同 LangfuseMedia ID；未授权媒体拒绝测试通过；没有证明 Cloud 上传/跨 Trace 去重或真实图像模型路径|
|Batching 减少网络开销|NOT RUN|使用 SDK flush_at=50、flush_interval=5；未测真实压缩请求数，不做收益声明|
|真实模型质量/费用|NOT RUN / UNKNOWN|未调用外部模型，未重跑 E1 30 题或 250 题|

唯一 TracerProvider 的创建与持有者为本进程 `_sdk()`；不再依赖对自定义 Provider 无效的 sample_rate 参数。固定验收使用完整采样。进程内单例配合重入锁，不因观测初始化异常卡住主业务。

## 8. 执行记录与复现入口

下列日志为不同阶段的实际执行结果，有交集，**不能相加为独立测试总数**。早期已被后续修复替代的失败也保留。

|实际执行|退出码/结果|证据|
|---|---|---|
|受影响模块回归（R1）|0；128 passed，5 Windows/POSIX 条件 skipped，6 既有 Pydantic warnings|`phase2/affected-regression-r1.log`|
|修复后定向回归（R2）|0；28 passed，6 warnings|`phase2/final-targeted-r2.log`|
|E1.2 三文件定向回归（R4）|0；31 passed，1 warning|`phase2/final-native-r4-tests.log`|
|官方 Callback timeout 定向|0；1 passed，11 deselected|`phase2/callback-unknown-final.log`|
|R5 SDK 初始化失败定向|0；1 passed，14 deselected|`phase2/sdk-init-deadlock-final.log`|
|本地旧/新/关闭观测矩阵|各运行退出 0；8/8 等价|`phase2/final-local-ab.json`、`closure/local-r4-*`、`local-r1/`|
|Cloud R4 六场景|各运行及最终回读退出 0|`phase2/ecs-cloud-r4.log`、`readback-r4-*`|
|Cloud R5 两场景|运行退出 0|`phase2/ecs-cloud-r5.log`、`final-r5/`|
|300 候选 Cloud 回读|0；PASS|`phase2/bulk-cloud-readback/summary.json`|
|受控 exporter 失败|0；业务完成、遥测失败如实记录|`phase2/export-failure-r4.log`、`closure/export-failure-r4/controlled-result.json`|
|差异空白检查|0|`phase2/static-diff-final.log`|

实际 ECS 单次入口形式（cwd 为对应代码归档目录；沿用已有隔离 Python/PYTHONPATH）：

```text
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python scripts/run_e12_acceptance.py --execute --schema e12 --case fulltext --content-policy LOCAL_ONLY --output-dir /opt/amrag/experiments/e12-20261029/cloud-r5-fulltext --cloud
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python scripts/run_e12_acceptance.py --execute --schema e12 --case agent_two_rounds --content-policy INLINE --output-dir /opt/amrag/experiments/e12-20261029/cloud-r5-agent-inline --cloud
```

输出目录必须新建，避免覆盖历史。`--preflight` 为零调用预检。`--cloud` 只允许 Langfuse US 的遥测网络流量；模型端点均替身。无 `--cloud` 时外部网络全部阻断。Cloud 模式本地 result 内内存 exporter 的 observations=0 **不是云端数量**，实际数量必须看 API 回读。

## 9. 未完成项、风险与后续唯一实验

1. **NOT RUN**：真实外部 Provider、真实 Embedding/Rerank/VLM 网络服务、生产持久数据路径、真实媒体上传及正式服务切换。本轮隔离验收不能替代这些上线检查。
2. **UNKNOWN**：真实费用、账单 Units、真实用户性能分位数。没有用零模型调用推断整条应用无成本。
3. **BLOCKED（独立历史项）**：非空 Hybrid 的 Agent 有效质量补救、Judge 可靠性仍需各自证据，不随本次观测验收关闭。
4. **技术债**：官方 Callback 的部分受控覆盖与 SDK 版本有关；当前锁定实测 5.0.4，升级时需重跑定向生命周期/隐私/父子关系测试。未为此扩建框架。
5. 本轮发现并准确观测的 `original_content` 替换损失未修复，这是用户明确边界。当前不能把 Context 扩展成功当作答案所需证据已进入模型。

下一项只建议一个 A/B：在小批冻结父/邻块证据题上，对照“现有 original_content 替换”与“保留实际扩展上下文送达”。固定模型、Prompt、索引、检索权重和阈值，先零生成核验目标字段在实际消息中的覆盖，再经预算批准运行少量回答，比较核心正确性、全文忠实性、引用和增量 Token。**本轮没有实施此业务改动。**

验收结论限定于本报告实际运行范围；未打 Tag，未提交、推送或部署，不以少量合成样本宣布生产全面验收。

## 10. R5 最终闭环记录

两条最终 Trace 已通过 Cloud API 只读审计，审计命令退出码均 0：

|最终版本场景|Cloud 数量|代码/父子/内容核对|分页 Score|证据|
|---|---|---|---:|---|
|R5 Fulltext|5 Observation / 1 Generation|PASS|0（1 页）|`phase2/readback-r5-fulltext/summary.json` 及 `scores-and-code-summary.json`|
|R5 官方 Callback Agent|21 Observation / 4 Generation / 2 Tool|PASS；消息为 PASS_CONTENT_PROJECTION|0（1 页）|`phase2/readback-r5-agent-inline/summary.json` 及 `scores-and-code-summary.json`|

两条根的 code_hash 均为 R5 应用源码哈希。`phase2/code-identity-r5.json` 进一步对清单中的 50 个应用文件逐一核对本地与 ECS 归档，全部一致，SSH 命令退出 0。最终锁修复没有用旧版本 Cloud 验证冒充最新代码。

收尾再次实际执行下列命令，退出码分别为 0、0、0；第一条输出 `1 passed, 14 deselected`，日志 `phase2/closure-r5-test.log`：

```powershell
$env:PYTHONPATH='D:/RAG knowledge/backend;D:/RAG knowledge/.venv/Lib/site-packages;D:/RAG knowledge/var/p1-metrics-python'
$env:PYTHONIOENCODING='utf-8'
& 'E:/RAG quention/.venv/Scripts/python.exe' -m pytest backend/tests/test_e12_native_observability.py -q -k actual_sdk_initialization_failure_cannot_deadlock_business
& 'E:/RAG quention/.venv/Scripts/python.exe' -m py_compile backend/app/services/observability.py backend/app/services/llm.py backend/app/services/agent_workflow.py backend/app/services/chat.py backend/app/services/grounded_generation.py backend/app/services/rerank.py backend/app/services/retrieval.py backend/app/services/trace_schema.py scripts/run_e12_acceptance.py
git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check
```

因此最终工程状态为：**本轮批准范围内的最小原生化及隔离 A/B 闭环 PASS**；表 7/章节 9 中的 NOT RUN、UNKNOWN、BLOCKED 继续保留。没有新增待负责人决定的评分争议问题。
