# 工程修复、动态路由与 RSI 基线核验

2026-11-03，Windows 本地 `D:\RAG knowledge`。本报告记录本轮工作树，实验队列见 [首轮 RSI 实验清单](engineering-rsi-first-experiments.md)。

## 基线、范围与顺序

- 目标分支 `codex/pdf-parser-migration`。开工 HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`，相对本地远端跟踪分支 ahead 1，工作区起始干净。参考提交 `8fca08ad669b3ba8b2fb6fe0da50cb9c2c54b48d`。用 `git diff --name-only 8fca08ad..HEAD` 核实了新增路由、Agent 接线与实验脚本。未切换分支。本仓库有 `AGENTS.md` 和历史 `.planning/`，无根 `progress.md` 和 `docs/adr/`；另一个 E 盘项目的进度不作为这里的基线。
- 先冻结原编号并复现 A，再处理 B 的证据送达、C 的错误与重试、D 的入库与展示；存储、召回、Agent 三条分工各有一名写入者。动态路由与前端边界在同轮核验，最后联测。替身均使用合成内容，数据库为临时 SQLite。依赖复用目标 Python 3.13.0、pytest 8.3.5、SQLAlchemy 2.0.55、Celery 5.6.0、httpx 0.28.1、Langfuse 5.0.4、Node 24.18 及已有前端依赖。D 环境缺 FastAPI、LangGraph、Chroma、openpyxl、tabulate。定向集成在 D 环境末尾追加同 ABI 的 E 环境，表格测试另使用已装纯 Python tabulate。LangChain Core 实际版本 1.7.0，和仓库 requirements 锁定的 1.2.2 有差异。
- “已修且验证”指该行所述输入和入口；SQL、DOCX、表格、浏览器及 FastAPI 为真实本地组件，模型、向量库及云端均按下文标注。语义和核心要点按业务效果判断，没有增加面向用户回答的严格校验。通过的命令不用于推断线上正确率。

## 合并台账

|原编号|状态|真实触发、处理、验证范围与剩余问题|
|---|---|---|
|1|已修且验证|结构化 DOCX 的标题、前言、多 run 正文原先写两遍，首标题偏移。现在结构模式在段落结束时写一次。生成实际 DOCX 检查正文次数和标题切片。见 `parser.FileParser._parse_docx`、`test_source_and_transport_contracts`。|
|2|待对照实验|默认 legacy、enhanced 才逐页分流仍是显式配置。见 `FileParser._parse_pdf`。OCR 和扫描表格按 E4 分组。|
|3|已修且验证|Word 的估算页、Office 的组号在 Recall 回包或最终上下文可变成“第 N 页”。单/多库 Recall 按文件类型处理，聊天和工具的最终来源同样区分 PDF 页与 Office 定位。未为旧 Word 重建真实页码。见 `api.recall`、`context_builder.build_chat_context`、`retrieval_tool`。|
|4|已修且验证；行数待对照实验|真实 XLSX 22 个数据行的第二块定位到工作表第 22–23 行，CSV 含引号内换行按逻辑数据记录计。解析时记录 sheet、数据行/记录与列范围，按源文件和块内容复用已有哈希 sidecar；手改正文不能匹配旧定位。无清单的旧数据未知；每块 20 行不变。见 `_parse_excel/_parse_csv`、`pdf_provenance.source_metadata`。|
|5|交付状态已修且验证；召回质量待对照实验|Agent 工具仍保留未核验图像的元数据和位置；有文字时以文字生成，混合/纯图片可提示需核验，不将描述作为已证事实。普通模式没有正文的图片也不再因哈希报错。图内信息的召回质量另做 E4。见 `build_chat_context`、`_register_sources`、`_guard_knowledge_answer`。|
|6 / N1|已修且验证|相同 `parent_id=chunk_1` 曾把异文件正文拼入结果。现在按 SQL 种子→文件→知识库以及请求允许范围检查；兄弟查同文件，普通聊天、工具、单库/多库 Recall 使用同一扩展逻辑。禁用扩展也检查索引标签与 SQL 归属。SQLite 反例含同库异文件、跨库、伪造种子和同文件正例；HTTP 检查正文。`retrieval._enrich_results`、`rerank.expand_context`。旧局部 parent_id 是解析组标识，不能当作可全局寻址的父行 UUID；引用定位使用实际成员 ID。|
|7|已修且验证；真实存储需环境验证|旧版单文件换模型或 add 遇到维度异常可删整库。现有非空库拒绝不兼容模型，实际向量批次先校验；新 ID 写入成功后再切换 SQL Chunk/BM25，最后按旧 ID 清理。写入失败用旧 A、旧 B 查询、文件状态及错误原因作断言。证据为 SQLite + 向量存储替身；跨 SQLite/Chroma 的进程崩溃原子性、真实后端异常类与并发窗口未验证。已有向量的文件不接受会覆盖旧索引的手动预览重解析。`tasks.parse_tasks.process_{file_parsing,submitted_chunks}`、`vector_store.add_documents/validate_dimension`。|
|8|已修且验证|删除及重解析后，旧 Chunk 的 BM25 条目与统计一并扣除；重复提交同一文档检查总数不累加；HTTP DELETE 后检查文档计数。见 `BM25Index.delete_chunk_index`、`api.files.delete_file` 和入库任务。|
|9|已修且验证|发现浏览器按 camelCase 读后端 snake_case，提交时又漏掉层级。现在单文件预览传原 Chunk ID 和最终编辑正文，服务端只从该文件自己的 Chunk 恢复可信结构/位置；跨文件 ID 被拒。多文件确认通过相同的入库处理。GET/PUT/POST 各保留一个 handler。FastAPI TestClient 覆盖实际 GET→按浏览器形状提交、另一文件、编辑和多文件 vectorize。见 `api.files`、`services/file.ts`、两个预览页面、`test_index_api_safety`。|
|10|需环境验证|Collection 创建未显式设置距离度量，现有查询为 `1-distance/2`。历史向量是否单位归一、哪些库用何种距离，没有从本地替身证明。保留公式，E2b 先查元数据再比较阈值。`VectorStoreService.query`。|
|11|当前版本已修；废弃实现已删除|复核后删掉没有执行路径的旧缓存类、构造和两项无效缓存设置。对外 `use_cache` 参数仍可传入，检索会重新执行，因此可能重新生成 Embedding。同参数失败后再调用能读到新结果。见 `RetrievalService.search`、`test_retrieval_cache_safety`。|
|12|已修且验证|原字符集合能合并同源 100 与 001。现在不同数字序列或带符号数值保守保留，真正重复正文仍合并；不更换相似度算法、不调整阈值。`retrieval.deduplicate_results`。|
|13|已修且验证|空库、存储报错、单路失败及双路失败分开。两路正常无命中为 NO_RESULTS；单路失败导致覆盖不确定为 UNKNOWN；两路故障为 RETRIEVAL_ERROR。融合后异常和结果未知不再次发送相同向量查询。API 及本地检索替身验证调用次数；真实 Chroma 未运行。`_existing_collection`、`RetrievalService.search/_hybrid_search`。|
|14|已修且验证；归一化待对照实验|无 jieba 时合法中文字符可形成有效词项，不会因过滤单字符静默变为空。等分归一及不同库分数可比性留 E2。`BM25Index` 的 tokenizer、`retrieval.normalize_scores`。|
|15|已修且验证|两批一成功一空/失败，保留可用候选及其原始顺序依据，未知分数为 null；真实评分正常按阈值过滤。普通、Agent、单/多库 Recall 有针对性验证。未知供应商结果单独终止；已知端点故障才走原有降级。`rerank.batch_rerank`、`ChatService`、`search_knowledge_base`。|
|16 / N2|已修且验证|邻块曾吞掉另一个独立命中；长兄弟排在前面会挤掉种子。按独立种子构造组，优先容纳完整种子，最后按实际成员去重。父组预算容不下种子时显式遗漏。字符预算只约束该拼接过程，不声称覆盖全模型窗口。`expand_context`、`build_chat_context`。|
|17|待对照实验|普通模式改写仍以最后用户问题为主。原查询保底列入 E3；历史感知的 Prompt/Memory 修改后置。`ChatService._chat_stream_normal`。|
|18|需环境验证|模型工具参数会消耗输入，目前路由计入其字符估算；API 未建立覆盖所有实际模型、消息模板、工具、媒体与输出预留的一致 tokenizer 契约。未猜测统一窗口或改变输出上限。`model_router.extract_features`、`grounded_generation`、`context_builder`。|
|19 / N4|已修且验证|连续两个 Document 1 可错指来源。工具结果进入模型前按本次回答分配稳定编号，重复来源复用；本轮工具合并后仍只评分实际截取的 2000 字符，返回评分范围，终端展示同一来源映射。真实编译的 LangGraph + 工具/模型替身覆盖两轮、重复及单工具失败。`AgentWorkflow.run_tools/process_tool_output/grade_documents`、`_register_sources`。|
|20 / N5|原问题当前版本已修；衔接已修且验证|知识库和锁定设置由服务端注入。仅把模型提出的阈值改低但实际查询参数相同，拒绝重复执行；区分提出值和实际值。保留提示与锁定参数之间的产品取舍。见 `AgentWorkflow.run_tools`。|
|21|已修且验证|服务端限制初次加最多 3 次重试，并覆盖一条模型消息请求多个工具；重复有效检索、超过上限或结果未知不会隐式再调用。不会把被拦下的工具当成有效补救。`AgentWorkflow.run_tools/agent_node`。|
|22|已修且验证|评分故障、无法解读与实际低分分开。没有可靠分数返回 UNKNOWN/无数值，不再用 0.5 触发重试；超时按已有 UNKNOWN 策略终止。等价数值写法可接受，回答文本不受此检查。`AgentWorkflow.grade_documents`。|
|23|当前版本已修|旧异步流重试装饰器已移除，真实流中断无自动重复付费。`llm.LLMService`、`provider_error_payload`。|
|24 / N6|已修且验证|收到 error 后旧客户端在 EOF 追加 done，覆盖错误正文。现在每次连接只有一个终态；正常 done、异常 EOF、TRUNCATED、UNKNOWN、显式取消分别处理。Chrome 检查用户可见的部分正文。见 `frontend/src/services/chat.ts:createChatStream`、Chat 页面 `onError/onDone`。|
|25 / N3|已修且验证；核验策略待产品决定|在普通生成及 Agent SSE 中披露 `validation_status=NOT_RUN` 与证据交付状态；页面可显示回答和引用未经逐项核验。关闭严格校验的负责人决策继续执行。评分正确不由“成功生成”推导。|
|26|待产品决定|长期/经验记忆未纳入修复。对话历史与跨会话经验持久化是不同能力。`AgentWorkflow`、前端聊天 store。|
|27|待产品决定|主动计划与 Skill 后置。工具调用和重试上限不证明已构建计划执行机。`AgentWorkflow`。|
|28|待对照实验|BM25 扫描及 Hybrid 串行的规模拐点未测，不报 P95。见 `BM25Index.search`、`RetrievalService._hybrid_search`、E6。|
|29 / N9|已修且验证；真实网络需环境验证|Embedding（含 requests 多模态）、VLM 默认验证 TLS，可设 `PROVIDER_CA_BUNDLE`。日志去掉原始请求/响应和 SSE 正文；流终态日志仅携带请求 ID、类型、状态、耗时。Mock HTTP 检查 CA 传递；Chrome/Node 用私有标记检查日志。没有测试用户真实网关或证书。|
|30|已修且验证；真实 Worker 需环境验证|原任务重试前写 FAILED，后续投递直接退出。现在按版本条件领取；预算内保持可重试，耗尽才失败，过期和重复投递不会覆盖新任务。直调 Celery task + SQLite 测试；当前公开 vectorize 入口使用 BackgroundTasks，旧 Celery 领取需要有效的文件版本。见 `tasks.vectorize_tasks.vectorize_file_task`。|
|N7|已修且验证|扩展抛异常或成员映射不可靠时，返回可确认种子并单列 `SEED_ONLY` / `UNKNOWN`；交付可靠种子、交付扩展和核验回答分别记录。测试直接检查 normal SSE、Agent 工具与两种 Recall 回包。|
|N8|已修且验证|曾仅凭 `parent_chunk_id` 将普通子块认作原生 PDF 表格补充。离线验证器现在要求显式 `source_type=source_native_table_expansion`，真正补充证据仍需原表凭据。`grounded_validation.validate_grounded_answer`、`test_pdf_table_grounding`。验证器未接入线上生成门。|

## 动态模型路由补审

- 路由仍默认 OFF。普通生成的真实请求由 `generate_grounded_candidate` 构造，Agent 在 `agent_node` 选择角色。角色绑定失败、供应商结果未知及截断使用不同状态；替身检查没有隐式再次调用。对工具消息只看正文曾漏掉模型实际收到的工具参数；现在在 L 的**字符估算**中包含工具参数。仍无计费 tokenizer 证明，未调整路由阈值或权重。
- Agent SSE 不再把“未知 Token”写成固定 0，能确认的 Agent 节点调用给出 `usage_scope=AGENT_NODE_CALLS`。不把这个局部值充当整条链的总额；意图、评分及模型服务明细仍分别看已有 Trace。跨模型的总账单未知。
- 历史 AFFINITY_V2 为单次生成的冻结输入回放。新的 `scripts/summarize_affinity_pairs.py --generation-records` 将评审回答哈希与相同 qid/实验臂的回包核对，再按供应商实际模型归属 Token。没有模型身份/用量就从这项配对指标排除并报告数量。
- 对已有 178 个问题，双方均完整且同输入 167 个；其中动态组 GLM 49 个、Qwen 118 个。GLM 输入 **603,706 / 2,127,124**，减少 **71.62%**；输出 **49,903 / 130,466**，减少 **61.75%**；总计 **653,609 / 2,257,590**，减少 **71.05%**。前数为动态臂 GLM 用量，后数为全 GLM。按两种模型合计则动态臂是 2,378,181 Token。这些是匹配槽位的用量，不能折算成实际账单。
- 同输入、双方完整且核心可判的 163 题，动态 141/163（86.50%），全 GLM 146/163（89.57%）。11 对未完整、另 4 对核心 UNKNOWN。属于本轮可读回的**历史**结果；修复后没有重新生成答案，当前准确率、延迟收益均 NOT RUN。

## 存量数据与使用边界

存量盘点只提供只读步骤。在一致的只读 SQLite 快照上按 KB 比较 DocumentChunk、ChunkIndex 和 IndexStats 的实际文档数/词频；按文件列表和索引 ID 核对只读 Collection 的数量、维度、度量、模型标识和版本。抽查带旧页码但无来源清单的 Office、重复 parent_id 和孤立向量。不应根据库大小或字符串相同删除文件；本轮未运行存量扫描。并发重建、进程在 SQL/Chroma 分界崩溃和旧向量清理失败仍需要隔离恢复验证。报告不授权自动迁移。

## 验证记录与复现

命令均在项目根目录执行。为了隔离动态追踪和 SQLite，使用下面的 PowerShell 前置配置；分组测试共用同一套代码和已装依赖。测试中的本地 HTTP 为 TestClient 或被拦截的浏览器请求。退出码是测试程序退出码。

```powershell
$env:DATABASE_URL='sqlite://'
$env:LANGFUSE_ENABLED='false'
$env:RAG_TEST_EXTRA_SITE_PACKAGES='E:\RAG quention\.venv\Lib\site-packages'
$proof=New-Item -ItemType Directory -Force var/engineering-consistency-20261103
$env:RAG_OBSERVATION_LOG=Join-Path $proof.FullName 'synthetic-events.jsonl'
$env:RAG_SNAPSHOT_DIR=Join-Path $proof.FullName 'synthetic-snapshots'
$env:PYTHONIOENCODING='utf-8'
$testBootstrap="import sys;sys.path.extend([r'E:\RAG quention\.venv\Lib\site-packages',r'D:\Drivers\anaconda\Lib\site-packages']);import pytest;sys.exit(pytest.main(sys.argv[1:]))"
$modules='source_and_transport_contracts paired_glm_usage evidence_repairs index_safety_regressions index_api_safety agent_engineering_repairs agent_evidence_guard agent_grounded_answer agent_request_parameters agent_stream_guard agent_tool_observation agent_quality_outcomes rerank_unknown retrieval_cache_safety retrieval_outcomes retrieval_source_dedup retrieval_tool_status e20_context_delivery normal_evidence_guard normal_grounded_answer dynamic_routing_agent dynamic_routing_chat dynamic_routing_generation dynamic_routing_observability model_router router_affinity_replay router_task_interactions affinity_paired pdf_table_grounding grounded_validation grounded_candidate_snapshot'.Split(' ') | ForEach-Object { "backend/tests/test_$_.py" }
.\.venv\Scripts\python -c $testBootstrap -o addopts= -q $modules -p no:cacheprovider --disable-warnings --tb=short
```

|入口|结果与证据|
|---|---|
|源文件、TLS、表格及离线验证器，`test_source_and_transport_contracts`|修前用实际 DOCX/XLSX/CSV 及 HTTP 客户端替身，原 4 个反例退出 1；修后加自定义 CA，用 `test_source_and_transport_contracts.py test_grounded_validation.py test_pdf_table_grounding.py` 及 `test_paired_glm_usage.py` 分组复核。观测为解析器、HTTP 配置与确定性结果，不是远程网关握手或真实模型。|
|Agent 修前/修后|HEAD 加载到内存执行反例，8 项失败，退出 1。历史红灯日志含一条后来按负责人口径放宽的评分呈现样例，不作为必须误拒的验收。Agent/路由替身回归阶段 180 passed，退出 0；其后的评分兼容组 63 passed，图片与 Agent 组 61 passed，各退出 0。文件 `var/engineering-consistency-20261103/agent-*.log`。这些组样本交叠，不能相加。|
|来源、Rerank、故障、缓存|直接测试 SQLite 种子→文件→KB 和两个 Recall HTTP 路由；检查回包实际 content、来源、failed batch 与 null score。单独复现过融合后重复向量调用、双通路失败及存储错误被当空结果。不使用正式索引。|
|入库、删除、Worker|涵盖 HTTP 提交后状态接口读回、现有 A/B 文件、Chroma 拒写、SQL 异常、BM25 统计、Celery 重试及版本条件。真实 SQLite、FastAPI；模拟 Embedding/Chroma，Celery task 直接调用。测试日志会输出预期故障，断言的是保留的旧状态和明确错误。|
|前端事件处理器|在 `frontend` 执行 `node --test tests/chat-stream.test.mjs`。修前 8 failed，退出 1，包括 `error→EOF` 同时调用 error 和 done；修后 8 passed，退出 0。|
|浏览器与真实前端构建|在根目录执行 `& 'E:\RAG quention\.venv\Scripts\python' frontend/tests/chat_browser_smoke.py --browser 'C:\Program Files\Google\Chrome\Application\chrome.exe'`。Vite 临时目录构建和 Chrome 页面四场景退出 0；断流时部分正文可见，同时显示失败、截断或中断。首次冒烟测试选到了组件隐藏 textarea，调整测试选择器后通过，无需更改页面功能。|
|全前端静态检查|在 `frontend` 执行 `.\node_modules\.bin\tsc.cmd --noEmit --pretty false`，开工基线退出 2。已有 Chat/Settings/其他组件的类型和未用变量问题；Vite 构建和实际页面验证单列。|
|真实运行与费用|真实供应商、生产索引、ECS、真实 Celery Worker 与 Cloud 链路 NOT RUN。本轮新模型调用 0；没有用真实用户问答生成回归数据。|

历史配对统计可在根目录复现：

```powershell
$f='var/dynamic-routing-20261101/affinity-v2-final'
.\.venv\Scripts\python scripts/summarize_affinity_pairs.py "$f/paired-reviewed-final.json" --generation-records "$f/paired-final.json" --output "$($proof.FullName)/historical-glm-usage.json"
```

复用与收尾：复用已有虚拟环境和前端依赖，未复制语料或 Chroma，没有创建服务容器。浏览器构建、测试页面、数据库及文件夹使用临时资源。早期一组替身快照测试向已跟踪事件文件末尾追加了 20 条合成事件；确认原 524 行全部匹配 HEAD 后，将 20 条保存于 `var/engineering-consistency-20261103/synthetic-early-events.jsonl`，原文件 `git diff --exit-code` 返回 0。后续输出单独写入本轮证据目录。

## 最终验收与废弃代码清单

|范围|结果|
|---|---|
|合并回归|第一轮 354 passed、6 failed（退出 1）；逐项查明并修复 Office 页码、图片来源与空正文问题，两条已过时的检索预期改为核对实际通路与不重发。后来合并组 **376 passed、1 failed、0 skipped**（退出 1）。原始记录分别为 `backend-combined.log`、`backend-final.log`。|
|最后的代码清理|使用同一 Python 启动方式，对 `agent_engineering_repairs、agent_evidence_guard、agent_stream_guard、dynamic_routing_chat、dynamic_routing_generation、evidence_repairs、retrieval_cache_safety、retrieval_outcomes、retrieval_tool_status、e20_context_delivery、normal_grounded_answer、normal_evidence_guard、index_api_safety、index_safety_regressions、source_and_transport_contracts、paired_glm_usage` 的 `test_*.py` 运行，**212 passed，退出 0**。统一所有入库准备步骤后再跑 `test_index_safety_regressions.py test_index_api_safety.py`，**6 passed，退出 0**。检查了删除文件时向量服务故障会留下原文件和 SQL 记录。见 `backend-after-cleanup.log`、`index-final.log`。这些组与合并组有重叠。|
|浏览器与静态检查|最后一次 Node 流协议 8 passed、Chrome 页面 4 个场景通过，均退出 0。全前端 tsc 仍退出 2；用相同本地 TS 编译器，将变更的前端源码在内存中替换成 HEAD 对比，原有 48 条诊断，当前 45 条，新增加 0 条。见 `frontend-typecheck-comparison.json`、`frontend-typecheck-final.log`。|
|未关闭测试|Windows 上 `test_grounded_candidate_snapshot.py::test_candidate_snapshot_is_opt_in_and_events_do_not_contain_raw_text`期待本地明文快照 ID。现有 `trace_schema._private_root_fd` 仅接受经过权限核验的 POSIX 目录，Windows 拒写；因此断言失败。独立测试退出 1；用例、快照策略及生成模块相对 HEAD 均未改动。该结果影响 Windows 私有快照回读，不阻断生成。没有据此打开快照或放松 ACL，实际 POSIX 部署验证 NOT RUN。|

本轮确认并删除了以下旧入口：

- 删 `backend/app/api/chunks.py` 及其 router 注册。原文件里的三个 URL 由先注册的 `api/files.py` 处理；测试验证每个方法/路径只有一个 handler，调用者保留原 URL。
- 删 `ChatService._chat_stream_agent`。正式 Agent 仅由 `chat_stream → _chat_stream_langgraph` 进入，普通模式不再额外实例化一个未使用的 AgentWorkflow 或向量服务。删无调用者的 `_load_or_caption` 和它的图片缓存配置，不删除任何图片或历史描述。
- 删没有调用路径的 `RetrievalCache`、旧键及无效的 TTL/启用配置；Graph 删未接入边的 `ToolNode` 对象。检索、工具及引用仍保留实际独立调用者需要的入口。
- 删单文件自动入库中独立准备 Embedding 的分支以及手动预览里多余的向量删除。自动与手动最终都由 `process_submitted_chunks` 校验并仅生成一次实际向量批次。旧向量按已退休的确切 ID 清理。保留显式整库管理 API；未对原始资料、索引或历史评测进行清理。

**未解决的使用限制。** 已索引文件的手动重解析会返回 HTTP 409 并解释原因；当前一个文件没有独立的可编辑草稿索引。如需重新入库可走自动重解析，若以后需要“保留旧版同时预览新稿”，单独决定草稿设计。SQLite 与向量存储不共享事务；异常退出可能留下无 SQL 对应的暂存或已退休向量。真实 Chroma、Worker、生产数据范围及性能未做验证。Windows 的私有快照和全前端静态构建分别有上述限制。

**可复用基线。** 代码/配置和相关测试的本地文件指纹为 `71b7efd58b21a8cc032acc5cda8bd6f80b6c9e18d39ab9f0b7b0dd252c51ef67`（47 个变更路径，含删除状态，不包含报告自身），清单在 `var/engineering-consistency-20261103/worktree-manifest.json`。测试证据保存在同目录；未创建另一份可执行项目。本机 D 盘可用空间开工约 105.1 GiB、收尾约 133.7 GiB，两者是整盘观测，差额不能归因于本任务。无 ECS 或生产变更、无新提交或推送。

**建议先跑 E1c。** 用现有冻结上下文确认逐成员选择能否缩短上下文，同时留住答案字段、表头与单位。队列中每个候选都有分母、预算和停止条件；当前不以替身通过率推断真实质量提升。


