# E2.0 父块/邻块扩展证据送达优化与 A/B 验收报告

日期：2026-10-30。**普通模式证据送达修复及 Cloud 核验 PASS；获准额度内最终 3 份真实 GLM 回答的核心、完整性和忠实度复核 PASS，另有 1 次 UNKNOWN 保留未重发。只有 Word 有完整 A/B，核心都正确；Excel 只有 B 返回，因此“提高最终回答质量”的因果结论为 UNKNOWN。** 当前结论与指标见第 11 节。第 1–9 节为零模型阶段，第 10 节为首次停止记录，均不能替代最新状态。

## 1. 版本与范围

分支 `codex/pdf-parser-migration`，基线 HEAD `722f000bd874d1fa17bfe94385612f045218f74e`；E1.2 原有未提交修改全部保留。本轮没有切换正式服务、修改生产数据库、重启监控或重发历史 UNKNOWN。公开版本不包含本机路径、私有快照、逐题原始账本或一次性真实模型运行器。

以当前源码和 E1.1/E1.2 报告为准。目标仓库不存在 `progress.md`、`docs/adr/`；本轮使用独立计划。基线应用哈希 `2d090f6878efb4bf7c6fd4aa447bd9b1c65c6a8f354843d1be03d21054e727fd`，最终应用哈希 `cd386b62b5cdb919e4f2bc67011744a865d679cf86c2dabc19b24bcccca91725`。核验清单与私有运行材料仅本地受限保存，不随本报告公开。本轮复用既有 ECS Python、Langfuse 5.0.4/OTel 1.45.1，没有安装或升级依赖，没有新建持久测试数据库。

ECS 只增加小型代码归档和合成 Trace 证据。所有 SQLite fixture 在内存；真实隔离索引只读查询，导出 72 个有界候选/相邻块约 0.7 MB，不是复制整库、原始文件或向量索引。历史数据未删除。

## 2. 原始问题与最小修复

原路径：`ChatService._retrieve_with_new_service` 调用 `rerank.expand_context`，后者返回组合正文及种子 `original_content`；`_chat_stream_normal` 组装引用时用 original_content 替换组合正文。因此扩展阶段找到的证据可能在 Provider 请求中消失。

改动前，`test_e11_context_delivery.py -k expanded_neighbor_does_not_reach_final_model_message` 实际通过，退出 0。随后私有 `before_prototype.py` 在一次真实扩展的不可变合成快照上分别执行真实 ChatService：A 缺邻块目标，B 原型以实际成员组织后送达目标且页码正确，退出 0。两项均在业务修复之前完成；不是事后改写失败证据。

|修改文件|函数/原因|
|---|---|
|`backend/app/services/context_builder.py`（新增）|`source_member`、`build_chat_context`：使用扩展器实际选中的成员；每个引用仍是原始 Chunk ID，Context Group 不冒充原块；验证正文 Hash、文件、成员顺序与组合正文对应关系；缺失或损坏映射时退回可确认种子并标 UNKNOWN|
|`backend/app/services/rerank.py`|`expand_context(..., include_source_members=False)` 新增显式可选成员注解；普通模式传 True，Agent 保留 False。只附加已被原算法选中的源成员，不改变窗口、父块字符限制、seen_ids、顺序、融合或重排|
|`backend/app/services/chat.py`|`_retrieve_with_new_service` 请求注解；`_chat_stream_normal` 使用成员级引用，缺失 provenance 不盲发组合文本。新增进程级 `RAG_CHAT_CONTEXT_STRATEGY`：ORIGINAL_CONTENT 为 A，默认 EXPANDED_CONTENT 为 B；不是用户可提权的请求参数|
|`backend/app/services/grounded_generation.py`|`delivered_evidence_records` 和实际 Provider 边界记录原始成员 ID、Group 关系及实际消息；Generation 能回溯成员。沿用原 Prompt、temperature=0.7、max_tokens=4096|
|`backend/app/services/observability.py`|仅增加严格枚举的 context_strategy、arm、关系和来源/Token 状态可读性许可；沿用 E1.2 类型与 Context 节点，不为每块新建 Observation|
|`backend/tests/test_e20_context_delivery.py`（新增）|来源成员、真实 Provider 消息、跨页、重复/损坏/缺失映射、限制和观测失效的定向测试|
|`backend/tests/test_e11_context_delivery.py`|原历史缺陷测试显式选择 ORIGINAL_CONTENT，继续证明 A 的旧行为；未修改 Gold 或历史账本|
|`scripts/run_e20_delivery_ab.py`（新增）|可复用零外部模型重放入口，冻结输入、一次扩展、A/B 实际消息、网络阻断及独立输出目录|

B 保留实际成员的原始 ID、文件、正文顺序、已知位置及 Hash；重复 ID 只发送一次，冲突身份标 UNKNOWN。邻块没有独立的检索或 Rerank 分数，相关分数置未知，不复制种子的分数冒充邻块分数。Office 索引里的页占位值单独记为 indexed_page_number，不冒充已证实的原文件分页；未知表格行、单元格或段落位置不补造。

这些是来源身份校验，不是重新引入已停用的严格答案句式/数值审查。没有新增独立 LLM 审查器。

## 3. 真实题目资格与固定条件

材料为本地冻结的历史候选快照（不公开）。107 条历史 SSE 候选正文哈希与只读隔离索引一致，候选顺序与当时的 `retrieval.after_dedup`、`qa.citations` 一致。题目、Gold、原文件校验值和逐项来源定位保留在本地受限账本，不随仓库公开。

实际样本为 5 道 Word 表格字段题和 1 道 Excel 单元格题。不是只以页面命中或数值字符串命中评分：确定性检查还要求同一源 Chunk 中的对应表字段/行关联。具体题目、Gold 和文件定位仅保存在本地受限账本。

**覆盖不足须保留**：真实 Office 171 块和 PDF 42,430 块的非空 parent_id 数均为 0，没有合格的真实父块恢复案例。本次真实题也没有证明“全部初始种子都缺 Gold，仅扩展才能找到”的案例。父块、跨页、部分 Gold、无关和重复成员等边界使用合成测试，不能算进这六题真实分母。

采用历史候选重放，**没有重新执行当前实时 Vector/BM25/Hybrid 或 Rerank**。每题将完整原有候选列表保持顺序输入一次当前 `expand_context`，保存不可变快照，再由 A/B 分别构建消息。候选数量依次为 20/20/20/20/20/7，不人为只留下缺 Gold 的种子。

固定参数：top_k=20、window_size=1、max_parent_length=4000，rewrite=False、rerank=False；两组均关闭独立的 Spreadsheet/PDF 原始来源补充，以隔离本轮变量。历史事件未记录足够的 Rerank 配置，其历史状态仍为 UNKNOWN，不能宣称逐字重现了当时全部配置。

两组问题/Gold、来源、候选、分数、排序、扩展快照、引用模板、Prompt 模板、替身返回及生成参数相同；区别仅为最终 Context 采用种子还是实际扩展成员。使用固定 Provider 替身，最终一轮 12 次替身请求；外部 Chat/Embedding/Rerank/VLM/Judge 请求均为 0。

完整历史索引指纹 NOT_CAPTURED。此次已导出且实际使用的 72 块子集指纹 `84a3117a4e27e41f228980eda31927a5ba59135b1e9b5c3624a9b59dae602c36`，明确限定为该子集，不冒称完整索引版本。

## 4. 零模型证据送达结果

最终零模型阶段证据为本地受限账本；每个 arm 一条记录，共 12 次替身请求，并另存完整实际消息与引用。原始消息、完整来源正文和逐题账本不随仓库公开。所有正确性/忠实度评分字段明确 NOT_RUN。

|qid|Gold 粒度|A ContextRecall|B ContextRecall|Provider 输入新增字符|Context 正文新增字符|
|---|---|---:|---:|---:|---:|
|001|Word 表格字段|1/1|1/1|24,312|22,405|
|008|Word 表格字段|1/1|1/1|3,566|1,866|
|010|Word 表格字段|1/1|1/1|3,680|1,895|
|014|Word 表格字段|1/1|1/1|3,680|1,895|
|020|Word 表格字段|1/1|1/1|3,566|1,866|
|031|Excel 单元格|0/1|1/1|66,067|65,747|

Gold Evidence Coverage 与 Context Evidence Recall 在本次每题只有一个必需字段单元的定义下相等，不能伪装成两个独立改善指标。按粒度分别统计：

- Word 字段 ContextRecall：A 5/5=100%，B 5/5=100%；送达失败率 0/5→0/5。
- Excel 单元格 ContextRecall：A 0/1=0%，B 1/1=100%；送达失败率 1/1→0/1。仅一个单元格样本，不是 Excel 总体正确率。
- 初始种子缺失且扩展可恢复的 Gold Unit：所有六题分母均为 0，Expansion Recovery Rate **NOT_APPLICABLE**，不填 0% 或 100%。
- 初始候选种子保留：Word 100/100→100/100；Excel 6/7→7/7；以“该题所有初始种子是否均保留”为题级指标时，Word 5/5→5/5，Excel 0/1→1/1。
- 证据送达配对：5 题两组都送达、1 题 A 未送达/B 送达、0 题 A 送达/B 丢失。**这不是答案正确性配对**。

031 的机制尤其容易误判：Gold Chunk 在初始 rank 2，rank 1 扩展时将其吸收为邻块并通过 seen_ids 抑制其独立返回；A 随后只发 rank 1 的 original_content，丢失该字段。B 发出真实邻块，字段才送达。它是**分组后送达丢失的恢复**，不是首次检索没找到 Gold，也不是新增召回算法的收益。

全文忠实性、Core Correctness、Completeness、Citation Support、Citation Coverage 的真实回答结果均 NOT_RUN；六对真实回答为 NOT RUN，不能填入 A 对 B 对/A 错 B 对等正确率格。当前只有“源成员定位及消息包含性”检查 PASS，不等于未来模型生成的引用一定支持其全部回答。

## 5. 成本、长度与局限

平均 Context 正文增加 15,945.67 字符，平均实际 Provider 输入增加 17,478.5 字符。最终送达引用成员总数由 106 增至 221（Word 100→205，Excel 6→16）。非 Gold 成员不自动等同于无关噪声；无关证据数量目前 UNKNOWN，不能把 115 个新增成员全部判无关。

031 的实际输入从 38,732 增到 104,799 字符，增加 170.6%。这一代价不能被证据送达 PASS 掩盖。原代码没有通用输入 Token 裁剪器；本轮未新增或提高两组预算。保留原 max_parent_length 字符规则，边界测试确认被裁掉的成员不谎称已送达。真实六题没有父关系，不触发该父块裁剪。

模型实际 Tokenizer 未配置，本机及 ECS 默认 tiktoken 缓存也未就绪，没有为了计数暗中下载模型或调用 API。因此真实 context_token_count/输入输出 Token/费用均 UNKNOWN；字符数、UTF-8 字节数如实记录，不用字符换算冒充实测 Token。

最终本地每题/每臂各一次，替身请求的普通服务段均值：Word A 6.136 ms、B 5.203 ms；Excel A 3.264 ms、B 5.381 ms。包含本地消息构建和替身，不包含真实模型网络/计算，存在预热影响，不能推断真实延迟变化、P95 或性能显著性。原始每题耗时保存在账本。

## 6. Langfuse 核验

仅使用获准合成文本做两臂 Cloud 写入，真实冻结文档正文没有为本次 Cloud 测试上传。最终应用哈希与本地最终版本相同。真实六题重放未伪造 trace_id，账本保留 null。

- [A：只送种子](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/ee6140d07119198889193b1b6589a4d8)
- [B：送达种子及邻块](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/bb3a2939dfba211df488174c258bbbb5)

每臂实际 3 个 Observation：qa.request CHAIN、context.build CHAIN、answer.generate GENERATION；一次替身 Provider 对应一个 Generation。这里是候选快照重放，没有发生实时检索，未造 Retriever 节点。source_members、种子/扩展/送达 ID、页码/文件/Hash、context_strategy、正文长度、截断状态及映射状态可回读；每个邻块没有新建 Observation。

Cloud Generation 的 input.text 解析后与本地独立保存的实际请求 messages 完整相等；根输出与服务事件一致；父子关系、run_id、qid、dataset、experiment_id、arm、code_hash 全部对应。评测 Gold 注解没有注入 Prompt，源文本中存在答案是正常证据。Provider 边界状态为 PREPARED_FOR_PROVIDER，避免声称请求准备等同于已被远端处理。

Cloud API 查询各臂均 HTTP 200，3 节点、Score 0、无后续游标；只读回验未创建评分。证据：本地只读回读摘要及原始 REST 响应（未公开）。本轮 Cloud 夹具没有注入私有 SENTINEL，该项 NOT_RUN，未拿不存在的测试宣称通过。

首次 R1 的来源/Token 状态被 sanitizer 哈希，明确为 FAIL；增加受控枚举后重跑 R2b 并回验 PASS，原失败完整保留。CLI 读请求两次 fetch failed/退出 4，随后使用官方只读 REST/HTTPX 完成回读；没有把 CLI 失败隐去，也未重发模型请求。

## 7. 测试命令、退出码与原始失败

以下命令在仓库根运行；使用本地项目 Python 环境。不同测试批次有重叠，不能把数量相加。依赖私有快照的重放命令和快照本身不公开，不能仅凭公开仓库重现该数据集实验。

|实际命令|结果|日志|
|---|---|---|
|`python -m pytest backend/tests/test_e11_context_delivery.py -q -k expanded_neighbor_does_not_reach_final_model_message`（修复前）|退出 0；1 passed，1 deselected|`before-reproduction.log`|
|本地修复前合成原型复现|退出 0；合成 A/B 真实消息送达证明|证据保存在本地受限目录|
|`python -m pytest backend/tests/test_e20_context_delivery.py backend/tests/test_e11_context_delivery.py backend/tests/test_agent_tool_observation.py backend/tests/test_retrieval_tool_status.py backend/tests/test_e12_model_integration.py backend/tests/test_e12_native_observability.py -q`|退出 0；59 passed，110 弃用警告，无跳过|`final-regression-r1.log`|
|`python -m pytest backend/tests/test_e20_context_delivery.py -q`（最终来源分页/状态测试补齐后）|退出 0；17 passed，118 弃用警告，无跳过|`final-e20-tests-r2.log`|
|本地冻结快照 A/B 重放|退出 0；6 对、12 个实际替身请求、外部模型 0|命令输出及快照保存在本地受限目录，未公开|
|`python -m py_compile backend/app/services/context_builder.py backend/app/services/rerank.py backend/app/services/chat.py backend/app/services/grounded_generation.py backend/app/services/observability.py scripts/run_e20_delivery_ab.py backend/tests/test_e20_context_delivery.py`|退出 0|收尾命令记录|
|`git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check`|退出 0|收尾命令记录|

17 个 E2 测试覆盖种子、父级兄弟、前后邻块、部分字段、重复、无关成员可见性、跨页、字符限额、无新增 Token 裁剪、空/缺失/篡改映射、最终 Provider 消息、真实引用成员、Agent 默认路径、Langfuse 不可用及观测开关业务等价。没有把所有边界都说成真实数据实验。

保留的失败包括：重复 ID 的来源冲突未标 UNKNOWN、缺 DB 映射误标 VERIFIED、历史 A 的 reason/hash 断言不一致；均完成最小修复后复验。首次重放的 Windows asyncio 内部 socketpair 被网络护栏误拦；后续仅在内部 socketpair 构造期间放行 loopback，其他出站仍阻断。Cloud R2 控制器路径替换误改 SDK 路径、其残留日志导致新文件保护拒绝，发生在模型夹具启动前；更正路径并采用独立 R2b 输出，未覆盖旧日志。

一次早期合成测试未隔离 RAG_OBSERVATION_LOG，向默认本地 `storage/observations/events.jsonl` 追加了夹具事件。已改用 pytest tmp_path；未删除或截断原日志，无法证明原长度时未擅自“清理”。本轮最终结果只读取独立输出目录，不将该默认日志混入统计。

## 8. 零模型交付时的逐题材料、预算与停止条件（历史）

原题、冻结 Gold、来源定位、完整 Provider 消息、逐题账本、快照及真实调用原始事件均仅保存在本地受限目录。本报告只保留汇总指标和结论，不附原始评测数据，也不公开可还原逐题内容的文件。

当时建议两类代表样本做 A/B，最多 4 次 Chat；沿用明确指定的 `zai-org/GLM-5.3-Flash`，temperature=0.7、max_tokens=4096。真实运行前先做零调用模型身份与请求来源核对，不更改默认模型。

四条冻结请求的**消息内容**共 219,351 UTF-8 字节，不含角色字段、JSON 序列化及 API 开销；实际 Token 未知，预算中保守预留 225,000 输入 Token 占用。这是预算估计，不是已证明的 Token 上界，也不证明已符合模型上下文窗口；真实调用前须先确认配置及上下文容量，无法确认则保持 BLOCKED，不发送。输出合计上限 16,384 Token；货币费用 UNKNOWN。Embedding/Rerank/VLM/Judge 均 0 次。不为了让 B 可运行单独减少候选或提高上限。

任一 UNKNOWN/超时、明确模型错误、来源/快照/配置不一致即停止后续请求；不重发，不扩大题目或重复次数。供应商 usage 超出预留则停止剩余请求。未经批准，真实生成保持 NOT RUN。

## 9. 零模型阶段的结论（后续真实结果见第 11 节）

- **扩展送达是否修复：PASS（已验证范围）**。确定性成员映射及六题重放中，B 保留种子并恢复 031 丢失字段；真实父块数据覆盖不足仍保留。
- **最终答案是否改善：NOT RUN**。必须经预算批准做真实配对回答并分别复核核心、完整性、忠实性及引用，不能由 ContextRecall 代替。
- **是否增加无关上下文：UNKNOWN**。总成员和长度明确增加，尤其 031；未完成相关性标注，不强行计噪声百分比。
- **是否增加成本：输入字符/字节已增加，真实 Token 与费用 UNKNOWN**。不能以零外部模型调用宣称该业务方案零成本。
- **Agent 是否需要单独处理：需要**。Agent 使用独立工具/上下文组装路径，本轮默认输出及关联测试保持原样，未声称自动修复 Agent。
- **下一项机制：先验证“保留扩展是否改善回答”；若送达已恢复但噪声/输入成本过大，再单独设计预算内成员选择 A/B**。本轮不顺带改变选择规则、阈值、Prompt 或输入裁剪。

当前可关闭的是本轮普通模式已证实的 original_content 送达缺陷及其来源观测问题；不能关闭真实质量收益、父块真实数据覆盖、Token/费用或 Agent 送达问题。正式服务保持原状。

## 10. 批准后的真实执行与停止记录

负责人明确批准两类冻结样本的 A/B 共 4 次真实 Chat。使用受限的一次性实验入口（未公开）；没有修改应用源码。应用哈希仍为 `cd386b62...`。运行复用 ECS 现有 SDK 虚拟环境，以独立 session 进程启动；首次启动器未回收直接子进程的操作系统退出码，**NOT_CAPTURED**，不能把启动退出 0 当作实验通过。

零模型预检通过：四份实际 messages/扩展快照哈希一致，9 份实际原文件 SHA256 及 KB 范围一致；使用现有 GLM 配置和凭据，未改正式默认模型。供应商 `/models` 只读 GET 返回 200，存在批准的模型 ID。官方 [SiliconFlow 模型说明](https://www.siliconflow.cn/news/ok5ja7brf921kgf1644yyedz)确认支持 1M 上下文；数据库通用 context_length=4096 未作为服务端容量。官方当日价格为缓存输入0.23、普通输入0.8、输出2.8元/百万Token；这些是价格来源，不是本次账单。

首个 Excel A 条件进入本地传输封装后抛出 RuntimeError，没有保存到任何供应商 HTTP 响应；最终服务事件为 error，done 缺失。运行器按停止条件中止，未执行后面三次，也未自动重发：

|项目|实际记录|
|---|---|
|获准计划|4 次 Chat|
|本地 Provider 发送尝试|1|
|已确认到达供应商的请求数|UNKNOWN|
|已收到的供应商响应/已确认生成完成|0 / 0|
|UNKNOWN|1（Excel A）|
|NOT RUN|3（Excel B、Word A、Word B）|
|可评分真实答案|0，正确率分母0，NOT_APPLICABLE|
|成功生成后的空答案/截断|NOT RUN；本次没有收到模型答案，不能当作空答案或错误答案|
|供应商实际Token及费用|UNKNOWN；日志内累加0不证明账单为0|

原运行器错误地把调用 inner transport 之前的计数命名为 model_requests_sent。它只是本地发送尝试计数，**不证明发生了线上的模型 HTTP 请求**。原文件保留，统计以本节口径为准。

零模型复现定位了运行器网络护栏缺陷：AnyIO 向 getaddrinfo 传入 bytes 主机名，原护栏只接受 str，因而抛出 `unapproved network destination`。`guard-failure-reproduction.json` 保存实际 GET `/models` 的调用栈和 bytes 类型参数，生成调用0。历史首个尝试只保存 RuntimeError 类型，缺少详细 cause/连接轨迹；因此不能以之后的复现反向伪造其确定的“从未发送”，继续保守保留 UNKNOWN。

最小修复只作用于实验运行器：将合法 ASCII bytes 主机名规范化再验证白名单；补记错误消息并修正发送尝试统计命名。`backend/tests/test_e20_real_runner.py` 实际运行退出0、7 passed；覆盖两个合法域名的 str/bytes 与恶意域名/错误端口拒绝。修复版在 ECS 上真实异步 GET 供应商模型列表及 Langfuse 健康检查均为 HTTP200，生成调用0；证据 `guard-fix-real-get-preflight.json`。修复验证没有发新的 Chat。

原始请求、事件、attempt ledger、停止摘要和运行日志仅保存在本地受限目录；旧运行器也未随公开代码提交。供应商没有返回 usage，不产生伪数值Token/费用；该次 Cloud 导出也因护栏失败，不能宣称真实调用 Cloud Trace 验收通过。

当时保留 Excel A 的 UNKNOWN 且不重发，仅继续尚未执行的 Excel B 和 Word A/B，共最多 3 次。这样只能获得 Word 一对完整 A/B 与 Excel 单臂结果，不能据此宣称 Excel 的配对正确率改善。

## 11. 明确批准续跑后的最终结果

负责人随后明确批准“继续未执行的 3 次”。运行器依据原停止摘要强制跳过 Excel A，仅执行 Excel B、Word A 和 Word B；不会重放 UNKNOWN。续跑由 ECS 独立 supervisor 运行，实际子进程退出码 **0**。原4次额度未增加，当前没有待执行生成任务。

本轮运行的是**真实 Provider + 普通 ChatService 事件链**，使用冻结候选/扩展快照；没有新增正式 HTTP 入口或实时 Hybrid 回归，不能将服务事件文件称为正式 HTTP 网络传输验收。模型、temperature=0.7、max_tokens=4096、Prompt模板和来源均保持批准条件；真实实际 messages 与批准快照逐条完全一致。

### 11.1 逐题答案、状态与分母

|题目/组|核心答案|执行/核心判定|输入Token|输出Token|其中reasoning|应用服务段耗时|
|---|---|---|---:|---:|---:|---:|
|Excel A|没有收到答案|UNKNOWN，不判空或错|UNKNOWN|UNKNOWN|UNKNOWN|原失败单独保留|
|Excel B|核心答案正确|PASS|18,565|265|119|7,076.568 ms|
|Word A|核心答案正确|PASS|5,745|182|71|6,422.963 ms|
|Word B|核心答案正确|PASS|7,285|277|171|6,498.627 ms|

以上为 **2个问题、3个已返回答案**，不是3个独立问题。3个响应均 HTTP200、实际返回模型 `zai-org/GLM-5.3-Flash`、finish_reason=stop、GROUNDED_GENERATION/COMPLETED，没有空答案、截断或跳过。原 UNKNOWN 不纳入答案正确率分母，也不假设其没有计费。

- AI 证据复核的 Core Correctness：**3/3=100%**；Completeness：3/3；Faithfulness：3/3；已标注引用支持性：3/3。
- A组有效样本1/1，B组2/2，分母不同，不能用两个100%推断总体或配对收益。
- 真正配对的Word：两组都对1对；A错B对0对；A对B错0对；两组都错0对。Excel完整对0，未完成/UNKNOWN对1。
- 当前最终空答案0/3、截断0/3、跳过0/3；本地尝试共4，其中3次有供应商响应、1次UNKNOWN。首次停止时的3个NOT RUN已经经批准执行，不能再重复计作最终NOT RUN。

这些判断来自**AI逐条核对冻结 Gold、实际消息、实际引用与原始来源**，不是用户亲自标注，也没有调用未校准 Judge。逐条复核材料及原始停止账本保留在本地受限目录，不随报告公开。

### 11.2 补充内容与引用复核

Excel B 的补充分类、单位及换算均在实际引用的同一表格中有来源支持；未发现混入其他对象或时期。连续表格解释和随后明确的同表分解列表采用组引用口径，可沿用[2]，不要求每行重复引用。

Word A/B 的核心百分比、预算和成本均由实际引用支持。回答的项目身份和标题也确实出现在**实际送入模型的同一原文件成员**中，因此补充内容忠实且与问题有关，不判编造。但回答没有引用标题对应的成员，存在成员级引用覆盖缺口。

为避免把正确性、忠实度、支持性和覆盖率混为一谈，本次按随逐题账本保存的**声明组**计算覆盖：Excel为“核心值/表格口径与换算”加4个收入分类，共5组，覆盖5/5；Word为“36%、预算、成本、项目身份/标题”共4组，A/B各覆盖3/4=75%。Word覆盖完整性状态FAIL仅表示未覆盖第4组，**不表示核心答案错误或事实捏造**。这些是本次声明组口径，不是通用平台引用率，也不是新加的运行时答案校验规则。

### 11.3 Token、延迟与费用

3个成功响应报告输入 **31,595**、输出 **724** Token，输出usage中包含reasoning **361** Token；隐藏推理正文未保存。纯Context的独立Token数量仍UNKNOWN，不能把含系统Prompt/用户消息的prompt_tokens全部叫Context Token。

唯一完整Word配对中，B输入较A增加 **1,540 Token（26.81%）**，输出增加95 Token，核心答案不变、引用覆盖缺口也未消除。应用服务段耗时增加75.664 ms；实际Provider段增加99.130 ms。每臂只有一次、存在预热和网络波动，不报告P95或显著性。Excel无有效A，不能计算真实模型Token或回答质量的配对增量。

3个成功响应缓存命中均为0。按前述供应商公开价，费用估算分别为：Excel B **0.015594元**，Word A **0.0051056元**，Word B **0.0066036元**，成功部分合计 **0.0273032元**。这是价格×返回usage的估算，不是账单；原UNKNOWN尝试的用量和费用未确认，**全轮实际账单仍UNKNOWN**。Word的估算费用增加0.001498元。

### 11.4 真实 Cloud Trace 核验

|真实执行|Trace|Cloud实际数量/状态|
|---|---|---|
|Excel031 B|[5158ac28…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/5158ac28402f287c40186de0edfdf31b)|3 Observation、1 Generation，PASS|
|Word014 A|[5da7d1d2…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/5da7d1d209c71a84d6e5360e78e3fbbf)|3 Observation、1 Generation，PASS|
|Word014 B|[9c3fb64b…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/9c3fb64bda92f5cdb60d27d1b7f7441b)|3 Observation、1 Generation，PASS|

官方只读API回验命令退出0，各Trace节点为root/context.build/GENERATION，父子、qid、arm、run/experiment/code/index身份、actual input Hash、根/Generation输出Hash、模型和usage均对应本地实际事实。source mapping为VERIFIED；送达ID分别与SSE格式服务事件、Generation及Context一致。实际调用只有一次Generation，未重复累计Token。

LOCAL_ONLY按批准策略保留Hash及受控快照引用，不将真实文档正文、完整Prompt或隐藏思考正文上传Cloud。抽查未发现完整问题及所选64字符来源片段；这是有界检查，不是所有潜在泄漏的证明。质量评分当前保存在本地AI复核账本，三个Trace的Score完整分页均为0；不将“未写Score”误当质量为0。

原始回读和摘要：本地受限的 Cloud 回读材料。此前CLI fetch失败后沿用官方HTTPX只读API回退。日志出现SDK“No active span”提示，已以实际Cloud回读核对本轮关键节点完整；提示的内部成因尚未单独归因，作为非阻断技术债记录，不为此重发模型或扩大观测建设。

### 11.5 收尾验证与结论

`python -m pytest backend/tests/test_e20_real_runner.py -q` 最后一次退出0、**8 passed**，日志 本地测试日志；新增的第8项验证只跳过历史UNKNOWN、只允许剩余3项继续，原始总尝试上限4不变。续跑预检退出0、模型调用0，明确 skipped_cases=031A、planned_new_attempts=3。实际续跑子进程退出0，原始命令保存在 本地受限的最终进程退出记录，未只引用启动器退出码。

完整逐题账本、实际答案、Gold、来源定位及原始消息均保存在本地受限目录，不属于公开报告附件，也未加入本次提交。

**本轮结论**：证据送达修复PASS，返回答案的核心与忠实性复核PASS，Word项目标题引用覆盖不足已记录；真实质量提升仍UNKNOWN。唯一完整对照未显示质量增加且Token增加，Excel B虽然正确，也不能补推不存在的A。Agent独立路径、真实父块覆盖、完整配对质量收益仍不能关闭。下一项建议是小范围研究**固定上下文预算内的扩展成员选择与贡献成员引用**，优先减少已经实测的Token增长；本轮未实施该机制，不增加调用。
