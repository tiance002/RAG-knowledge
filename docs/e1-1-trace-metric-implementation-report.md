# E1.1 Trace 与评测可复盘闭环交付

本轮完成 V1 规范与 Langfuse-only 补充指令约定的最小闭环：错误题复盘、可解释候选变更、扩展与实际模型消息对照、Agent 多轮记录、历史已审 Score 关联、隐私和真实 Cloud API 回读。正式服务未切换，模型/Prompt/检索权重/阈值/解析器未变，未提交或推送。外部 Chat、Embedding、Rerank 调用均为 **0**。本报告不将替身运行视为真实模型质量评测。

## 版本、规范与原始证据

- HEAD `a834f8f7006bc1e9b1fb998fc9f8cb220f79d80a`，分支 `codex/pdf-parser-migration`，工作区有本轮未提交改动及既有运行产物。
- V1 原件 `C:/Users/22088/Downloads/RAG_Langfuse_Trace与评测指标规范_V1.md` 与 docs/e1-1-trace-metric-spec-v1.md SHA256 一致：`1eeba819410a473ff21419506161008d01b9a20e11fe20d1b997af30ecd3b847`。补充指令见 docs/e1-1-langfuse-only-supplement.md。
- 最新验收 r7b 归档 SHA256 `07477019f14f207dcccfb6e1a768c8b13645df54c38bddec93029e0c72af9f9a`；打包时 tracked diff SHA `b90cbdedf3783345e47b6c704375dc83bbecdc8eaa9d885ed58e3e55ded6e621`。未跟踪源码由归档和实际 app hash 覆盖，不把 tracked diff 当全部工作区版本。
- r7b staged app SHA `11990dc3012c91ca7ca255cb8c1da9c7e658d7b0114d28e90eeadcbadfa717b5`；Cloud根 code_hash `565804b8af3977e25e99a155a586931dcad9da450beee17567ea75accb1292f8` 使用 app 相对路径算法，与 staged hash 路径前缀口径不同，已分别从归档独立重算；当前全部归档路径与工作区字节一致，见 var/e11-20261029/current-source-archive-verification-r7b.json。完整运行清单 runtime-manifest-r7b.json 包含 Python、SDK、来源、索引范围、flags、请求数与退出码。
- ECS Python 3.12.3、Langfuse 5.0.4、OTel API/SDK 1.45.1、FastAPI 0.115.5。复用既有隔离 venv，没有升级全局依赖。正式 API 进程使用 /opt/amrag/.venv/bin/python3，观测相关环境标志未设置；该环境缺 Langfuse 与 Prometheus client，与隔离验收环境不同，生产默认观测关闭。
- r7b 数据集 e11-synthetic-v1，来源 SHA `1befc3af173779a9d03a29be8674c158b15347f9db78194a7f3ac7fdb08fe274`，观测到的文件块指纹 `ed2285597932caf4e99d5725c152c490fabb402a6277b521e1479b88873537d6`，只描述合成 SQLite 一文件一块与假 BM25，不是正式 Chroma 物理版本。

## 实际修改与字段覆盖

| 文件/函数 | 已完成 | 范围与限制 |
|---|---|---|
| observability.py span/event/observe/capture | 同一 SDK 父子链、标准 input/output、根实际配置/代码与已观测文件索引指纹、真实 Observation ID、运行身份、根 SSE 拼接、状态和原始/最终哈希；SDK 失败显式记录、遥测 fail-open | 本地 fallback ID 不作为 Cloud ID；运行中 export=NOT_CONFIRMED，须单独回读 |
| trace_schema.py | BASIC/EVAL + debug_details，精确 dataset/qid/source manifest；正文授权信封及后台 mask；统一内容身份和 visibility | 实际业务 capture 默认 LOCAL_ONLY，整份混合消息未经完整来源授权不直接上传；无公开原文接口 |
| trace_schema.py/read_e11_snapshot.py | POSIX dir_fd/O_NOFOLLOW、0700/0600、所有权、大小/容量、锁、内容哈希、期限和损坏检查；离线 CLI | Windows 未验证 ACL 时 NOT_CAPTURED；读取期限默认7天、容量50MB/单项1MB；不自动删除历史；磁盘加密未验证，权限不等于加密 |
| retrieval.py | 实际两路/Min-Max/weighted sum、融合排序和 Top-K、去重前后、每项精确ID/同源字符Jaccard删除及代表和阈值 | 不改字符Jaccard >0.85、跨来源规则或任何排序；不存在的路线不补零 |
| rerank.py | 实际批次正文裁剪哈希、响应下标映射、阈值/排序/Top-K、原分数回退来源、父邻块组成员 | 本轮真实网络 Rerank NOT_RUN，定向替身与实际函数验证 |
| retrieval_tool.py/agent_workflow.py | 独立实际工具轮ID、查询/参数、评分原文/有效性/2000字符范围、重试决策；工具返回与原内容替换记录 | 工具返回不冒充最终Provider输入；Agent补救效果没有Gold则UNKNOWN |
| chat.py/grounded_generation.py/llm.py | 实际引用选择与原内容替换/排图、发送前完整消息、可见原始响应、真实 provider.call 跨度、原始与最终 SSE 哈希 | 扩展内容在原逻辑中被替换的事实可见，未修算法；response瞬时事件不冒充请求延迟 |
| evaluation.py/metric_catalog.py | V1指标名、固定枚举、完整同粒度Gold门、覆盖和相邻转移、排名/缺失语义、独立质量和分母 | 无完整qrel的Recall/MRR及无graded qrel的nDCG不填0；混合评判者不伪装全人工 |
| publish_p0_scores.py/import_e1_reviews.py | 新导入rubric、SSE/结果/事件不可变关联、稳定Score ID、dry-run、实际Cloud根归属检查、分页值/类型/来源回读 | 旧p0语义和ID保留；不伪造历史rubric/Observation ID或回填旧Trace |

密钥、Authorization、连接凭据与显式推理内容拒绝采集（包括 `<thinking>` 及嵌套消息字符串中的换行 Thinking 标记）；SDK mask 未全局关闭。未授权哨兵在非空Cloud响应中不存在，仅限该受控测试，不宣称任意输入永远零泄漏。BASIC不外发任意正文，LOCAL_ONLY 必须有实际保存且能回读的引用，否则 NOT_CAPTURED。

## 真实 Cloud 与本地回读

r7b [Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/25d63f7688bf7dcd76229f6a825a0c79)：run `synthetic-ffaf2c10795b44b8818bcd2e57441f31`，root `5c9e13222a46b9ff`，qid synthetic-1。实际 FastAPI/SSE→ChatService→全文检索→grounded generation 链路，Provider为替身；HTTP200/SSE done。Cloud CLI退出0/HTTP200读回26个Observation，唯一根、父ID无缺失，25个正常节点运行身份一致，另1个为刻意不获授权的独立采集范围。

Cloud另已实际核对根 effective_config（模型、Prompt哈希、temperature=0.7、max_tokens=4096、fulltext/top5、未执行模型参数NOT_APPLICABLE）、code_hash、capture_mode、已观测文件索引范围，候选标准输入输出、retrieval.search返回ID与来源not_applicable原因；内容描述符的子ID与父ID分别匹配真实SDK节点。根问题、检索实际Query、Provider最终消息、原始与最终答案均为 LOCAL_ONLY，Cloud标准 input/output 有角色、时间、哈希及引用。ECS CLI逐一读取6个不同引用，6/6退出0且内容hash一致，包含问题、合成源文、实际模型消息和答案。不能只凭目录/URL作可读证明。

- Cloud：var/e11-20261029/readback-r7b-final/summary.json；私有原始API数据 readback-r7b-final/raw-page-001.json。
- 本地：r7b-private/cloud-sse-r7b/events.jsonl、sse.txt、snapshots/；snapshot-cli-readback-r7b.json。
- `provider.call` SDK Span 有实际起止时间，为替身实际计时（详细数值见回读摘要），只是替身调用，不用于模型性能基线。无真实usage/可信费用，相关聚合NOT_AVAILABLE，不接受平台把缺值求和所得0当免费账单。
- 源码归档与运行清单可独立核查，Cloud回读摘要不单独宣称验证了归档字节。

## 历史 E1 与 Score

30题原始回答全部保留：用户23/24、受托AI4/6，混合27/30。只描述该全文检索便利样本，不是Hybrid总体质量。统一文档 var/e1-20261029/E1-统一待裁决问题.md 当前无新增需要负责人裁决的问题；配套 .json 为30题多行缩进证据，包含字段、段落、引用和来源，技术UNKNOWN不让用户凭猜测投票。

选定正确题 office50:001 和错题 row-0958 导入，逐项核对原SSE/结果/事件及答案hash，并在Cloud验证原根ObservationID。新rubric e11-historical-import-v1 明确是新导入版本，历史rubric NOT_CAPTURED；关联依据 immutable_local_case_and_cloud_root_identity，Cloud没有旧答案正文hash，不能声称Cloud证明了旧答案内容。6条AI复核独立导入保留AI身份，未伪装用户判断。

2题12个Score只发布一次。即时回读曾缺12条，保留失败报告；延迟后只读回验12/12，ID/名称/值/类型/Trace/来源/元数据均一致，无重发。证据 scores-dryrun-v2.jsonl、score-publish-readback.json（失败）、score-publish-delayed-readback.json（退出0）。未知质量用类别UNKNOWN，不发布伪数值0；没有再运行Judge或历史UNKNOWN请求。

[反射面错题复盘](e1-1-error-replay.md)连接原题、原PDF第9页、20个历史候选、实际Prompt/SSE与判定；目标页9未进入去重前20候选，发送资料无mirror。无法追溯的历史Query、过滤原因、同粒度完整qrel和SDK字段保留NOT_CAPTURED，不从新代码推断旧行为，也不编造Recall。

## 定向验证与实际命令

- 最终本地受影响套件：`E:/RAG quention/.venv/Scripts/python.exe -m pytest backend/tests/test_observability.py backend/tests/test_e11_root_capture.py backend/tests/test_e11_capture_policy.py backend/tests/test_e11_candidate_transitions.py backend/tests/test_agent_tool_observation.py backend/tests/test_e11_context_delivery.py backend/tests/test_e11_cloud_acceptance_harness.py backend/tests/test_evaluation_baseline.py backend/tests/test_score_publication.py backend/tests/test_e11_metrics.py backend/tests/test_provider_unknown.py -q`，PYTHONPATH为backend、本项目venv/site-packages和var/p1-metrics-python：退出0，91 passed、5 POSIX skipped、6既有Pydantic warning，日志 final-contract-tests-r2.log；补齐 span 内容父ID后再跑 test_e11_root_capture.py，退出0、16 passed/1 POSIX skipped，日志 final-capture-binding-tests.log。
- r7b独立POSIX测试：ECS `python -m unittest discover -s r7-test -p test_e11_capture_policy.py -v`，PYTHONPATH 指向 code-r7b/backend：退出0，7/7通过，无跳过，日志 capture-policy-posix-r7b.log。包含目录替换、并发容量、权限、损坏、路径、期限、凭据及推理内容拒绝。
- r7b `scripts/run_e11_cloud_acceptance.py --preflight`：退出0、0网络/模型；`--execute --output-dir .../local-sse-r7b`退出0；同一源码 `--execute --cloud --output-dir .../cloud-sse-r7b`退出0。Cloud仅Langfuse出站，实际模型0；使用内存SQLite和新合成资料，不复制正式数据或Chroma。
- `python scripts/build_e11_review_document.py`：退出0，30个不同qid/SSE hash匹配、0空回答、0缺Gold、0越界编号、0新增待裁决项。
- `py_compile` 对23个本轮Python文件：退出0，syntax-check-final.json；`git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check`退出2，现有/改动行尾空格，日志 static-diff-check.log。此为非阻断样式技术债，不冒充静态检查全通过。
- 原始失败保留：依赖路径导致早期导入失败、r2无SDK实际ID、r3 EVAL上下文丢失、初版根空/取消语义测试失败，修正后有定向复验；不覆盖旧运行。r7b使用正确SDK依赖路径，未升级全局SDK。

同源去重20→19（精确副本1）→7（相似副本12）的实际函数替身测试，记录代表ID和字符Jaccard1.0/阈值0.85；Hybrid/Rerank替身验证融合Top-K、重排、阈值和最终Top-K，开关观测业务结果一致。扩展测试证明邻块Gold虽在扩展结果，却因原逻辑使用original_content没有进入最终消息。Agent替身两轮有独立实际查询/ID/评分和重试，不计有效救援。细节见 docs/e1-1-trace-examples.md，所有替身与Cloud事实分开。

## 监控停用和原生分析

三个确定专属于本项目的隔离systemd单元已保存配置/状态/Dashboard快照，执行 stop + disable退出0，现 inactive/dead/disabled。原数据目录保留；正式API /health前后HTTP200，rag-eval、PostgreSQL、Docker及原有容器正常运行，Langfuse US health HTTP200。仅健康检查，不冒充生产QA。内存used1868→1798MiB、available1759→1828MiB，两个时点不作因果CPU收益。证据 monitoring-audit-a4e8651d3b0849539bea4b733fe52e46/。可选metrics.py保留、默认关闭，没有新增监控栈。

Langfuse原生Scores和Metrics API实际可读到2题的PASS/FAIL/UNKNOWN及采样Trace计数/耗时。自定义Widget对scores-categorical的traceId过滤返回HTTP400/CLI退出5；SDK/CLI公开schema未给出已验证的精确样本替代过滤，不扩大分母凑图。未创建成功任何Dashboard/Widget，UI渲染NOT_RUN。按补充指令允许的兼容方案，保留原生Scores/Trace与经过验证的trace限定Metrics查询和本地关联报告；未知检索/Token/费用不填0。能力/查询证据见 langfuse-native-capability-readonly.json、langfuse-widget-filter-capability-final.json、dashboard-write-ledger.jsonl。此为非阻断平台限制，不新建可视化系统。

## 验收状态和后续

| 项目 | 状态 |
|---|---|
| 正确根/实际SDK身份/子链/最终消息与答案可回读 | PASS：r7b真实API + Cloud + 私有CLI |
| 候选去重/融合/重排与实际送达差异 | PASS：实际函数替身测试；未称真实模型质量收益 |
| 历史错题复盘与已审核Score正确关联 | PASS：旧字段缺失明确保留，2题12Score真实回读 |
| 隐私、遥测故障fail-open、空/截断/取消/未知 | PASS：受控测试和r7b未授权哨兵；不作任意输入零风险保证 |
| 隔离监控停用、历史保留、其他服务健康 | PASS：实际systemd与前后检查 |
| 原生Scores/Metrics查询 | PASS；自定义Widget精确样本筛选BLOCKED，UI渲染NOT_RUN |
| 原E1同粒度完整qrel缺失项/上下文精确率 | UNKNOWN/NOT_AVAILABLE；不从来源页命中推算字段命中 |
| 非空真实Hybrid有效补救、可靠Judge自动判分 | 独立BLOCKED，本轮未扩展 |
| 新真实外部Chat/Embedding/Rerank验证 | NOT_RUN（0调用），本轮无需重放旧题 |
| 静态语法检查/样式检查 | PASS / FAIL（仅行尾空白技术债） |

下一步只建议一个机制实验：在固定题集、模型、Prompt、索引和阈值下，比较现有选中证据与单一图片证据候选扩展方案；先零生成比较目标字段级证据覆盖，再对少量明确案例比较最终核心/忠实/引用。反射面与人脸网络两题涉及目标证据缺失，电影产业题则是已给证据的解释/完整性问题，不能混为同一收益。此为建议，未实施算法更改。


## 最后一次契约复核

r6 保留为历史运行，不能证明本轮收尾修复；离线用新验收器核查 r6 时按预期 FAIL，缺根配置、子内容身份和标准候选 IO 的原始证据保留在 readback-r7-tool-offline-r6/。r7 仅运行预检/本地替身，在 Cloud 前发现 span 内容缺父ID，保留该包并以不可变 r7b 复验；r7 没有 Cloud 请求。r7b 是本报告最终源码验收版本。

首次收尾套件 1 failed/90 passed/5 skipped：原因是测试仍使用旧的简化证据结构；补齐真实身份字段的期望后通过，日志 final-contract-tests.log 与 final-contract-tests-r2.log 均保留。实际SDK新增的 span父ID检查单独通过，不以测试更新替代实际 Cloud 回读。

本地原始 Cloud 响应目录之前继承了较宽ACL，本轮已保存 acl-before-hardening.xml 并收紧 var/e11-20261029 为当前用户及SYSTEM；抽查旧r6文件仅这两类访问者，icacls退出0。没有删除旧响应。Windows业务快照仍因未实现可验证ACL而fail-closed，这与人工受限审计目录分开。

用户审核入口仅一份 var/e1-20261029/E1-统一待裁决问题.md，配套多行缩进JSON收纳30题及83条实际已引片段。已有用户口径继续有效；真实证据缺失保留UNKNOWN，目前新增待裁决数0，不用用户猜测技术事实。
