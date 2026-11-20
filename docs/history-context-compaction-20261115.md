# 历史上下文与 Agent 大结果按需读取验收

日期：2026-11-15（Asia/Shanghai）。项目：D:\RAG knowledge。状态：本地工程与 ECS 隔离验收 PASS；真实模型质量/延迟/费用收益 NOT_RUN；正式服务未替换，未提交/推送。

## 一、固定参考与适配边界

WeKnora 官方干净浅克隆固定 cbdcebabf48c5ee2d6868b370326aa8341703f62，位置 var/hybrid-concurrency-20261114/reference。Lite 使用 SQLite、FTS5/sqlite-vec，但普通聊天/Agent 历史机制通过通用消息仓库工作，不是数据库专属算法。本项目保留现有 SQLModel/SQLite、Chroma 和 jieba BM25，不迁移 SQLite FTS5、不重建索引。

普通 RAG 参考 common.go::prepareMessagesWithHistory、search.go::getSearchResultFromHistory、merge_history.go::filterHistoryResults：历史问答作为背景，取最近一个有引用的轮次，词重叠/较小词集 >=0.3，最多3条，历史分数折扣0.6。Agent 参考 observe.go::HistoryAsSent/buildMessagesWithLLMContext、service/agent_history.go::LoadAgentHistory：默认不复用旧知识库工具正文，保留对话；RetainRetrievalHistory 开启时保留工具历史，不存在“全过程3条”的统一限制。

用户文章固定 tiance002/learn-claude-code 提交0dcafa2ae053a1ddd6a72f265431104b08a5aa13，正文SHA256 bfb6c3df3728c92847feb7c37cd16e63491589c9ed0249babbdcbcc2b4b23c29。已从固定SHA再次下载并比对原字节一致。文章是独立设计参考，不是WeKnora源码。

本轮实现可恢复的工具转存、索引与旧预览压缩、保护完整调用/结果对和活跃问题。未增加LLM历史摘要请求，未照抄文章30k/50k/200k阈值。继续保留项目5轮历史默认值和32000应用序列化字符预算；不是可靠Tokenizer计数，也不宣称实现了WeKnora全部token校准与持久化compaction checkpoint。

## 二、实际行为

### 普通RAG

服务端读取历史，沿用知识库范围、当前ACTIVE发布和原文核验。仅最近一个带可复用引用的轮次参加补充；按token交集/较小集合达到0.3，排除本轮已有相同来源/正文，最多3条且受正文预算限制。旧引用不当作本轮编号。

保留本项目新证据优先的合并位置；记录history_original_score、history_discounted_score=score*0.6和history匹配类型，缺分数保持None。不修改原检索分、rerank_score，不把这个折扣伪称实际重新Rerank；也不混加BM25原始分和向量分。因此机制参考WeKnora，但不是其最终排名逐项复刻。

最终生成提示中明确历史问答是对话背景，事实引用以本轮来源正文为准。没有新增答案字符串/数值硬校验。

### Agent

AGENT_RETAIN_RETRIEVAL_HISTORY=false为默认值：不自动注入历史引用工具正文、不再每次搜索后拼接历史3条。仍提供裁剪后的历史问题和回答用于任务延续，旧知识工具结果在历史视图中省略，要求当前检索。

显式开启保留时，服务端验证过且当前范围内的历史原文可以注册为可读来源，不使用普通RAG每次3条补充限制；通过保存结果索引按需读取，仍受请求预算。不是完整还原WeKnora全部历史工具轨迹：本项目复用现有后端引用/正文存储，未建第二套会话数据库。

### 大结果

AgentToolResultStore：请求级私有目录，完整原始UTF-8工具结果落盘；随机不透明r句柄与SourceRegistry的sN来源标签分开。模型不接收文件路径。POSIX目录0700/文件0600；Windows沿用可信父目录ACL。全文SHA、总8MiB、最多32条结果，来源必须在当前请求Registry内。

判断落盘使用已验证、去重后的模型工具视图大小，默认超过6000字符；不是重复正文的后台原始JSON大小。小结果直接发送。初始索引最多10张来源卡片，标题最多160字符、预览80字符，定位元数据过大时明确省略。预览标注EXTRACTIVE_INCOMPLETE_NOT_FULL_EVIDENCE，不能冒充完整原文送达。保留总来源数和下一页offset。

read_tool_result按句柄+来源标签或来源分页读取完整原文，最多6次本地读取。需要时还可使用已有read_knowledge_context读取当前来源周边。已读取但后来被预算省略的正文可再次读取。跨请求句柄、未登记来源及任意路径均不可读取。过大的整来源返回CANNOT_FIT_WHOLE_SOURCE，不剪一半却声称完整。

所有原始工具消息/Registry保留；压缩只改变实际模型视图。空间紧张先缩短可恢复的较早索引卡片，再处理旧对话背景；不破坏工具调用和结果配对。保持最多6个实际父块。只把真正进入当前模型请求的完整原文计入model_evidence_source_ids；未读/被省略者仍为PARTIAL/省略，不因已登记或落盘而标DELIVERED。

磁盘操作在线程中完成；取消时先等已开始的有界磁盘操作结束，再清理，避免写入与清理竞争。完成、异常、取消均finally关闭。保存不可用或超限时回到既有整来源预算路径，不另发模型请求。临时目录不可用不改变正式检索阈值。

## 三、修改文件和兼容

已有文件：backend/app/core/config.py、backend/.env.example、services/conversation_context.py、grounded_generation.py、agent_workflow.py、agent_context_budget.py、agent_evidence_projection.py、chat.py。
新增：services/agent_tool_results.py、tools/read_tool_result.py。
定向新增/适配测试：test_history_context_policy.py、test_agent_tool_results.py、test_agent_tool_result_workflow.py、test_agent_context_budget.py、test_agent_on_demand_context.py、test_agent_evidence_led_graph.py及相关历史测试。

没有新增表、API、前端字段或任务队列；保留当前引用与请求级SourceRegistry、provider约束、原模型配置、检索权重及阈值。新配置见.env.example。历史数据未删除。当前分支codex/bm25-stats-write-optimization-20261112，HEAD7c7415f85a063e3f7645f676bd22f7c8f766a732；原工作区已有大量未提交修改，本任务差异独立保存在var/history-context-20261115/task-changes.patch，不等于git整个dirty差异。

## 四、实测与退出码

1. 普通历史/真实SQLite存储专项：
```powershell
& '.venv\Scripts\python.exe' -m pytest backend/tests/test_history_context_policy.py backend/tests/test_server_conversation_store.py -q --tb=short --junitxml=var/history-context-20261115/history-policy-junit.xml
```
退出0，59passed。核对最新引用轮次、重叠分母、最多3条、去重、未知分数和正文预算。

2. HTTP/存储/联网引用等关联集成：
```powershell
& '.venv\Scripts\python.exe' -m pytest backend/tests/test_history_context_policy.py backend/tests/test_server_conversation_store.py backend/tests/test_server_conversation_api.py backend/tests/test_web_source_registry.py backend/tests/test_web_search_chat.py backend/tests/test_agent_source_projection.py backend/tests/test_agent_tool_results.py backend/tests/test_agent_tool_result_workflow.py -q --tb=short --junitxml=var/history-context-20261115/integration-junit.xml
```
退出0，122passed；该次发生于最终有界索引补修前，相关最终Agent测试见第3项，不混称同一版本全部重跑。

3. 最终Agent19模块套件实际命令，工作目录 `D:\RAG knowledge\backend`：
```powershell
& ..\.venv\Scripts\python.exe -m pytest tests/test_agent_tool_result_workflow.py tests/test_agent_tool_results.py tests/test_agent_context_budget.py tests/test_agent_evidence_projection.py tests/test_agent_source_projection.py tests/test_agent_source_registry.py tests/test_agent_evidence_led_graph.py tests/test_agent_direct_first_retrieval.py tests/test_agent_engineering_repairs.py tests/test_agent_stream_guard.py tests/test_agent_history_signal.py tests/test_agent_on_demand_context.py tests/test_agent_request_parameters.py tests/test_agent_evidence_guard.py tests/test_agent_quality_outcomes.py tests/test_dynamic_routing_agent.py tests/test_query_agent_context.py tests/test_agent_grounded_answer.py tests/test_agent_tool_observation.py -q -ra --tb=short --junitxml=../var/history-context-20261115/agent-workflow-tests.xml *> ../var/history-context-20261115/agent-workflow-tests.log
```
3. 最终Agent19模块套件：退出0，261passed/4skipped/0失败/0错误。原始命令和完整结果见agent-workflow-tests.log/.xml，测试文件清单可从JUnit逐项读取。4项跳过均为旧test_agent_evidence_guard.py依赖本机缺失的历史response.sse，记为未验证，不计通过。覆盖大小触发、来源50分页、4轮各50来源、读后又省略再读、6父块、默认/保留历史、真实模型请求捕获、落盘失败、错误/取消与收尾。各套件重叠，不相加。

4. 本地可移植HTTP/SSE探针：ecs_history_probe.py，退出0，56断言；7用户请求/15次替身SDK调用/0外部调用。普通2题、Agent内联1题、强制转存2题、保留历史2题。实际使用FastAPI会话接口、SQLModel/SQLite、LangGraph、LLMService/ChatOpenAI工厂，网络边界仅替换供应商响应；合成Atlas知识库，非正式QA。

5. ECS运行：
```bash
bash /opt/amrag/experiments/history-context-a26bfd9c/run.sh
```
退出0，SOURCE_HASHES_AND_RESOURCES_PASS，56工程断言PASS；同样7用户请求/15替身SDK调用，真实外部模型/Tavily均0。run_id history-context-a26bfd9c。本地/远端增量归档SHA一致：3043e9f7273831f8c37532f9b9d1a65ce8faa37afd2b656792758066308fa53d。所有17个兼容文件SHA与清单比对通过。

## 五、实测开销：不能把单次缩短当作总体收益

同一4855字符单来源，模型响应为替身。A内联阈值32000；B故意降至2000触发新路径，这是工程对照，不是生产阈值或质量实验。默认6000下该小来源保持直接内联。

|项|A直接内联|B强制转存再读|
|---|---:|---:|
|模型SDK调用数|2|3|
|首次检索后请求messages字符数|6918|2455|
|本题全部模型messages字符合计|8209|12047|
|最大单次messages字符数|6918|8301|

首次看到结果时缩短64.51%，但本题最终仍需完整读取唯一来源，总字符增加46.75%，多1次调用。不能宣称总体省Token或更快。更可能受益于多候选只需读取少量来源/长任务反复回放场景，真实收益仍待固定开发集对照。

以上是messages序列化字符，不含工具schema的该表指标；实际产品32000预算仍包含schema。随机句柄长度可能使本地/远端相差几个字符，不涉及正文不同。真实输入Token、推理延迟、费用、正确率收益均NOT_RUN/UNKNOWN。替身usage固定10不能当作真实账单。

## 六、保留的失败与审查

旧真实图测试曾期待9k工具正文始终在context，第一次更改后失败；已改为通过新读取工具再核对远处事实，而非删除事实断言。初次可移植探针因合成源末尾空格与产品规范化后的引用不一致而失败；修正合成入库资料使其本身不带尾空格后通过，未改产品原文来迎合测试。原失败日志local-initial.log保留。

独立审查未发现跨库、标签混淆、假送达或取消收尾的阻断问题。主代理追加发现并修复两项工程风险：按后台重复JSON触发落盘导致小结果多请求，以及多轮索引本身过大；又核实预算省略后的原文可以重读。源码与回执已交叉核对。

## 七、资源复用与收尾

复用ECS既有虚拟环境与只读源码基座；不安装依赖，不复制真实语料/生产SQLite/Chroma，不重算向量。仅建立小型合成SQLite和请求级临时工具文件。测试结束工具临时目录为空；复制的隔离源码2461213字节已删除，保留增量payload、结果、SSE、合成SQLite、请求和manifest用于恢复。

ECS测试前/后MemAvailable2200/2179MiB；可用磁盘12350148608/12348391424字节，清理后12349243392字节。共享主机波动不能归因为本测试精确开销，峰值未测。未越800MiB/8GiB护栏。正式API测试前后PID1029438，无重启或流量切换。

两个早期本地合成测试空目录0文件0字节仍保留：%TEMP%/rag-agent-results-50fv7max、rag-agent-results-b8me727p。删除被自动审批拒绝，提示blocked by policy且无具体理由；未绕过。当前最终测试没有新增泄漏。

## 八、证据、恢复与下一步

本地var/history-context-20261115/：reference-identity.json、baseline.json、before/、task-changes.patch、ecs-final-manifest.json、ecs-final-payload.tar.gz、agent-workflow-tests.log/.xml、history-policy.log/-junit.xml、integration-tests.log/-junit.xml、local-final-bounded-index/、ecs-evidence.tar.gz、ecs-readback/、comparison.json。ECS路径/opt/amrag/experiments/history-context-a26bfd9c/保留结果和payload；代码副本已清理，复现前从manifest.source_base复制纯源码并解压payload即可，不能操作正式目录。

本轮未运行真实云端Qwen质量测评、完整60题、浏览器交互或Langfuse Cloud回读，不把合成PASS冒充实际回答提升。未引入LLM摘要第四层或完整会话checkpoint迁移。下一步最小实验应在同一开发题、索引、模型和预算下对比直接内联/按需读取，分别统计事实覆盖、引用、累计输入Token、工具调用和耗时；先验证多候选稀疏读取是否收益，再决定正式启用。当前仅本地候选和ECS隔离结果，不自动替换服务。

## 本机可复制的HTTP/SSE复验命令

先进入项目根目录。下列命令每次生成独立输出目录，不覆盖完成结果；所有模型响应均替身，不调用云端。
```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH='D:\RAG knowledge\backend;D:\RAG knowledge'
$env:PROBE_RESULTS='replay-' + [guid]::NewGuid().ToString('N')
& '.venv\Scripts\python.exe' 'var/history-context-20261115/ecs_history_probe.py'
```
