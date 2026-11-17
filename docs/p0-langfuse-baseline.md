# P0 Langfuse 与评测基线（2026-10-28）

## 当前交付与边界

默认 RAG_OBSERVABILITY_ENABLED=false；未修改 PDF 路线、Hybrid 权重、Rerank 阈值和 Agent 0.8/3 次质量重试。观测异常不重试业务、不吞业务异常。源码尚未提交或部署正式服务。

可选依赖经 PyPI 查询及实际安装/import：Langfuse 5.0.4、OpenTelemetry SDK 1.45.1。官方 Context7 的示例版本较旧，最终用安装包 inspect.signature 核对 client、start_as_current_observation 和 API 读回方法。US 配置已 auth_check 成功。Cloud 与自托管由 LANGFUSE_BASE_URL 切换；没有强制自托管。

## 实际源码调用顺序

普通问答：ChatService.chat_stream → _chat_stream_normal → 可选 rewrite_query → _retrieve_with_new_service → RetrievalService.search（cache 命中提前返回）→ 单路或 Hybrid → 去重 → batch_rerank（可选）→ expand_context（可选）→ 构造引用和系统上下文 → 图片描述（配置允许时）→ LLMService.generate_stream。

Agent：chat_stream → _chat_stream_langgraph → AgentWorkflow.analyze_intent → 闲聊 generate_chat 或 agent_node → run_tools → search_knowledge_base → 检索/工具内重排/扩展 → process_tool_output → grade_documents → 返回 agent_node。质量评分只看截断的相关性上下文，不是最终正确性评分；低于 0.8 时最多注入 3 次重试提示，之后仍由模型决定是否调用工具。Agent 可以覆盖部分查询策略参数。它没有调用普通问答的 _retrieve_with_new_service。

独立 Recall 路由在 backend/app/api/recall.py，调用 RetrievalService 并按接口参数运行重排，不能用它替代 Agent 正式链验收。两条路径的参数与实际阶段必须分别记录。

入库：upload_file 仅上传并记 PENDING → 手动解析入口以 BackgroundTasks 调 process_file_parsing → FileParser.parse 与各格式解析 → 待确认 → process_submitted_chunks → 保存 chunk → BM25 → Embedding → Chroma。存在 Celery 包装器，但当前 HTTP 手动解析入口不是统一 Celery 投递。当前 PDF 默认仍 legacy，enhanced 逐页分类/Tesseract/表格已有实现。

## 埋点清单

| 来源 | 观测内容 |
|---|---|
| observability.py | opt-in span/async/stream 装饰器、ContextVar 父子关系、SDK lazy client、本地脱敏 JSONL、默认不存全文 |
| files.py、parse_tasks.py、parser.py | 上传 file_id、解析配置/计数、分块计数、BM25/向量写入成功；异常沿原业务传播 |
| retrieval.py | Vector/BM25 候选及顺序、归一化、融合、去重前后、缓存命中 |
| rerank.py | 每批输入输出、阈值过滤、回退原始分数、扩展的 parent/sibling/neighbor ID |
| chat.py | 主 QA root、配置、最终上下文引用/指纹、引用候选、捕获的执行错误 |
| agent_workflow.py | 意图/节点/tool/评分 span、实际查询指纹与可选隔离快照引用、实际参数、retry_count、质量评分、可获得的 usage |
| llm.py | 请求指纹与输出上限、模型、可获得的 finish_reason/usage、空答案/截断；不改变请求参数来索取 stream usage |
| index_lineage.py | 文件源码 SHA、已保存 chunk ID+内容哈希集合的观测指纹 index_version；不等于物理向量库版本，也不能证明 DB/Chroma 一致 |

过滤原因可由前后候选集合与原阈值定位；当前 VectorStore 内部被阈值提前删除的候选不可见，应记 UNKNOWN，不能宣称每个过滤原因都完整覆盖。扩展事件记录新增 ID，而不是把父子/邻块当成新独立检索命中。

## 脱敏和配置

复制 backend/.env.langfuse.example 为 .env.langfuse.local；后者被 Git 忽略且已经建好供负责人填写。必须由隔离启动进程显式加载，文件不会自动改变正式服务。US 为 https://us.cloud.langfuse.com。

默认上传 ID、顺序、数值评分及哈希；query、Prompt、文档正文等字符串只留指纹与长度。SDK 使用独立 OTel provider 和 mask，避免自动采集所有框架 Prompt。RAG_SNAPSHOTS_ENABLED 默认 false；明确开启才写 RAG_SNAPSHOT_DIR 下的隔离快照，本地文件权限 best-effort 0700/0600，Windows 仍应由部署者设置 ACL。不上传快照正文或本地路径。密钥不写入报告/日志，ECS 验证从 SSH stdin 仅送到进程环境，未落盘。

## 评测与历史回放

evaluation.py 纯函数比较 Gold 的同粒度证据单位：完整覆盖 PRESENT、部分 PARTIAL、明确观测到而无覆盖 ABSENT、Gold 或阶段缺失 UNKNOWN。邻接阶段保留/恢复/丢失与 coverage_delta 分开记录。核心正确、全文忠实、引用正确、证据召回、空、跳过、截断、延迟、token、cost、remediation_success 均是独立字段；没有判断器或价格则 null，不生成虚假 0。

normalize_location 支持 PDF page/bbox、Word heading_path/table_row、Excel sheet/row/column/cell、CSV line、TXT/Markdown heading、image bbox；缺失是 null，不推断原文位置。当前解析器未产生的 Sheet/单元格等信息不会凭空出现。

scripts/evaluate_observation_baseline.py 将实验版本与 arm 组合，按 qid+version 做对比键。历史最终评审回放已生成 var/review250-20261028/p0-baseline.jsonl 与 summary。历史文件没有中间阶段 IDs，因此阶段全部 UNKNOWN 是正确结果，不是新实时 Trace。

## 实际验证与遗留

本地定向套件：test_observability、test_langfuse_sdk_export、test_evaluation_baseline、test_observation_stages、test_index_lineage、test_llm_answer_limit、test_score_publication；15 passed、1 个既有 Pydantic 警告，退出 0。覆盖默认关闭、脱敏、导出失败、并发 10 个根跨度隔离、原流式顺序/异常、检索/重排不改返回结果、证据状态、索引指纹变化，以及真实 SDK/OTel 的内存导出、未知评分与幂等 ID。向量/模型测试使用替身，不视为线上验收。最终执行证据见 p0-test-record.md。

ECS scripts/run_p0_ecs_trace_smoke.py 的 --preflight 退出 0，零调用，验证冻结数据和 10 题选择。实际代码 staged 于 /opt/amrag/experiments/p0-langfuse-20261028-code-r1；单独虚拟环境安装 SDK 并桥接原应用依赖。首轮在 import openai 前失败，无模型调用；第二轮保留日志并独立运行。

实时 smoke 已完成，直接检索→重排→扩展→非流式生成：3 Hotpot、3 中文、4 PDF，查询向量复用冻结缓存且记为缓存。4096 输出预算和 smoke Prompt 属于新实验，不与旧 2000 评测混算。它不是 HTTP/SSE 或 Agent 全链测试。Cloud 通过 observations.get_many 读回，最终证据在 var/p0-observability/cloud-readback-final.json，10 个 trace_id 与结果一一对应。

另已完成真实入库与查询关联测试：隔离上传、解析、切块、BM25、Chroma 均成功；仅复用与原文完全匹配的真实缓存 Embedding，新增模型调用为 0。随后真实向量查询取回同一 chunk，两个独立 Trace 的 file_id、source_sha256、index_version 完全一致；Cloud 两条 Trace 均已读回，证据见 var/p0-observability/ingest 和 cloud-ingest-readback.json。这验证了后加的 lineage 实现，不代表已对所有文件格式重跑解析。本轮未执行全 250 题重跑。

尚未完成 HTTP/SSE、真实模型 Agent 重试路径的端到端验收。Cloud Score 已实际写入并只读回验：10 题共 124 个确定性分数，涵盖证据状态、已知覆盖、空/跳过/截断、耗时和生成 Token；未知判分不上传数值 0。全文忠实度、引用正确性和最终补救成功需要有依据的判定，目前为 null，不能称作完整评分验收。功能接线与小规模真实 Trace 闭环已可用，P0 全部验收仍待补齐正式路径和有依据的答案判分。

## 下一阶段优先级

P0：用少量固定题补正式 HTTP/SSE 和 Agent 重试/引用链验证；提供可核查的隔离答案快照，再分别判核心正确、全文忠实、引用与补救结果。入库与查询 lineage 已验证；物理索引完整性仍需单独核验。Cloud Score 已用稳定 ID 实现幂等提交，未知证据状态用 CATEGORICAL UNKNOWN，未知正确性/成本跳过数值提交。

P1：补齐提供商 stream usage、失败重试每次成本、字段级引用判定、各格式缺少的定位；分开报告判分结果和指标未知项。

待进一步调查：现有缓存键不含全部参数/索引版本、非原子索引更新等风险，另行评估；缓存风险会影响实验可信度，实验应禁用缓存并记录索引指纹，正式业务修复需单独任务。本轮不修算法或阈值，不开发自动 RSI 改码。

官方参考：https://langfuse.com/docs/observability/best-practices；https://github.com/langfuse/langfuse-python 。SDK 5 已通过本地安装签名核验，Trace 读回应使用 observations.get_many，不能沿用旧 API trace.get 示例。

## 本轮真实 smoke 完成记录

ECS 第二轮 10/10 结果均 COMPLETED，日志 FINISHED、无 Traceback；独立派生评分命令退出 0。没有持有原子进程退出码文件，因此不把结果计数冒充主进程退出 0。实际 events 有 10 个 llm.response，1 个 length、无 failure 事件。

四个带页级 Gold 的 PDF：前三题 raw/merged/rerank.filtered/context 均 PRESENT；第四题前阶段 PRESENT，context 只覆盖 1/2 Gold 页，为 PARTIAL。这里 rerank.filtered 是阈值过滤后的全部候选，不是 Top-20；只能定位丢失发生在“阈值候选→Top-K选择/上下文”这一段，不能断言 expand_context 丢弃了正确页。源码已为后续运行新增 rerank.selected，当前 10 题没有这个事件，不能追补为已观测。

其他六题有答案 Gold 但无足够的来源单位 qrel，因此状态 UNKNOWN。runner 不执行正式引用输出，cited 全部 UNKNOWN。未保存原始答案正文（仅 hash），新 10 题的正确性与补救是否成功不作推断；需在明确的隔离快照配置下再进行少量验证。

Cloud 读回已完成：cloud-readback-final.json 中 10 条根 Trace 与 ECS results 的 trace_id 完全一致，10 条均包含 search、Vector、BM25、fusion、dedup、rerank、expand、generation。实际 10 个响应均有 usage，但 cost 均 UNKNOWN；其中 1 条正文为空且 length，结果的 COMPLETED 表示请求返回，不等于回答有效。SDK+OTel 实际内存导出测试另外退出 0，不等于模型或 Cloud 运行。

实际 bounded 请求：19 次 rerank、10 次生成、0 次新增 Embedding（查询向量复用冻结缓存）。生成 usage 汇总 prompt=100775、completion=15610、total=116385；无价格表所以金额为 UNKNOWN。补充发生过一次本机 Cloud 读回 ReadTimeout，增大只读审核请求超时后成功读回 10 条，并未重发任何模型请求。

## 四类结论与源码位置

**代码已确认**：扫描页改进早已存在，本轮未替换。pdf_pipeline.py 的 classify_page / parse_page 按页以原生字符量和图片覆盖率路由，local_ocr 使用 PyMuPDF get_textpage_ocr 与 Tesseract eng+chi_sim；native_tables、scanned_tables 分别处理文字表格与栅格表格。parser.py 的 _parse_pdf 将页面片段转换为切块并写 manifest；pdf_provenance.py 的 source_metadata 仅从匹配原文的来源 Hash 恢复定位。config.py 的 PDF_PARSER_MODE 默认仍是 legacy，不能把 enhanced 实现存在当作正式已启用。

**有实现、部分生效待验证**：chat.py 的 chat_stream、agent_workflow.py 的 analyze_intent / agent_node / run_tools / grade_documents 已接埋点；10 题仅验证真实检索生成核心路径，未覆盖正式 HTTP/SSE、Agent 的每条分支。rerank.py 的 included_sibling_ids 与 rerank.selected 为后续更细定位而新增，不能反填历史事件；当前 10 题所有扩展事件 parent_id 均为空，不受旧 sibling_ids 过度计数风险影响。各格式统一定位在 evaluation.py normalize_location，实际源格式没有提供的字段仍未知。

**文档声明但代码尚未确认**：没有把上游 README 的能力声明作为验收依据。实体来源文件的多栏阅读顺序、扫描表格质量及每种 Office 文件的端到端定位仍需真实材料运行确认；已有历史评测只证明当时冻结代码和数据的结果，不证明本次新接线全覆盖。

**明显缺失或进一步调查**：retrieval.py RetrievalCache._make_key 只有 query/kb_ids/search_mode/top_k，缺 score_threshold、融合权重、Embedding 与索引版本；clear 方法存在，但在 app 内未检出调用。缓存命中提前返回且不产生 Vector/BM25 新阶段，评测不能伪造这些阶段。retrieval.py search/_hybrid_search 可捕获失败后降级或返回空；rerank.py _rerank_via_api 可改用 chat，batch_rerank 可回退原分数；agent_workflow.py run_tools 可将异常转错误工具结果。这些是现存行为，本轮仅补失败/回退事件，不改策略。VectorStore 内部提前过滤不可见、流式 usage 与每次重试成本不完整、价格缺失、跨存储更新非原子等均需进一步核验。

## 最小下一轮评测

保持 12 题回归与冻结数据，先选 4 个文档（数字、扫描、混合、真实表格）和每份 2 道有原页 Gold 的题，在隔离环境比较 legacy/enhanced。固定切块、Embedding、模型、Prompt、检索配置，禁用检索缓存；检查解析文字、真实页/区域 → 入库 chunk → 检索 → 上下文 → 引用的证据覆盖与答案核心正确性，单独报告 OCR/表格错误，不用总分掩盖。

解析端到端确认后，在同一小题集依次比较 Vector/BM25/Hybrid、Rerank 开关、普通问答/Agent；每次只改一个因子，按 qid+experiment_version 比较，记录失败、跳过、空、截断及调用增量。先不要大规模重跑 250 题。
