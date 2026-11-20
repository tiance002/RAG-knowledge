# 服务端会话历史：本地审查与 ECS 隔离验收

日期：2026-11-14。目标项目：`D:\RAG knowledge`，ECS `/opt/amrag`。

## 结论与适用范围

**冻结受测版本的服务端历史工程验收 PASS；整套 RAG 无条件替换正式版本的结论不成立。正式服务没有切换。** 收尾时另有三个本地文件发生后续变动，见下述版本漂移，不能把本报告套用到这些未测变动。

- 最终源码本地后端 **60 passed，0 failed / errors / skipped**；前端状态/SSE 检查 12 项、真实 Chrome 合成流程 11 项均通过，TypeScript/Vite 构建通过。
- ECS 最终隔离运行 `live-run3`：普通模式和 Agent 各 3 轮，**6/6 COMPLETED、0 空答案、0 截断、0 UNKNOWN**。5 次实际知识检索都有 Vector 与 BM25 候选；另 1 轮是基于前文的算术追问，查询理解选择了 follow_up/闲聊路径，没有伪称检索。
- 重启隔离服务后，答案与引用回读、幂等重放、新浏览器读取、引用展开 **13/13 检查通过**，重放没有增加 Chat HTTP 请求。
- 人工式逐题复核：核心事实 **6/6 正确**；全文及补充内容完整有据 **3/6**，另 **3/6 PARTIAL**。这只是同一公开报告的两个三轮开发会话，不能外推为整体正确率或性能提升。
- 既有 Agent 在 `top_k=10` 下的工具消息超 32,000 字符预算仍然存在，未通过放宽预算或删证据掩盖。最终通过的小样本明确使用 `top_k=3`，不等于默认 top_k=10 已修复。
- 查询理解仍有英文误改成中文：最终 6 次中 5 次。这次混合检索仍找到正确资料，但不满足原语言要求。本轮没有扩张到改写提示词优化。

负责人可以据此判断是否替换；建议将“历史持久化接线通过”与“默认 Agent 大上下文可用、模型补充内容可靠”分开决策。

## 版本与隔离身份

- 本地实际分支：`codex/bm25-stats-write-optimization-20261112`。
- HEAD：`7c7415f85a063e3f7645f676bd22f7c8f766a732`，已有大量 dirty 文件；未 reset、clean、提交或推送。HEAD 不能单独代表本次运行源码。
- 实际源文件身份：`var/server-chat-history-acceptance-20261114/final-source-manifest.json`，**104 个源码/测试/构建资产文件逐一核对，ECS 0 个哈希差异**。该清单 SHA256：`e2cf5212946e0356a6ed35b53974b9bf9e51efe133523d440128fb7d30b8bbf0`。
- 隔离目录：`/opt/amrag/experiments/server-chat-history-20261114-a1`，只监听 `127.0.0.1:18321`，浏览器经 SSH 隧道访问。
- 原数据库 `/opt/amrag/backend/local.db` 使用 SQLite URI `mode=ro`；会话与检索日志写入隔离目录的 `data/conversations.sqlite`。
- 复用现有解释器 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python`，没有新建虚拟环境或安装包。正式 Chroma HTTP 服务只读查询，没有复制数据库、语料或索引。
- 隔离 app 只启动 Provider/Chroma/词法执行器生命周期，不执行正式 app 的建表迁移、任务恢复或解析 Worker 启动。
- 正式 API `amrag-file-api.service` 全程仍为 PID **680975**，active；未修改正式代码、反向代理、服务配置或共享资源限额。
- 只读逻辑计数前后一致：17 个知识库、23,787 个文件、117,507 个块、12,017 个索引构建记录、1 条系统设置。这些计数不替代全库字节一致性证明；本轮未对生产库进行写入测试。

### 收尾版本漂移，未覆盖他人修改

完成实测后再次核对本地清单，发现 `backend/app/core/config.py`、`backend/app/main.py`、`backend/app/services/bm25.py` 已被其他工作现场继续修改：删除 BM25_BIGRAM_INDEX_PATH 的环境配置入口，启动与分词改为强制依赖 jieba，并移除自动 bigram 回退。HEAD 未变，dirty 内容已变。

这些后续变动没有被本任务覆盖、回滚或同步到 ECS。**其兼容性 NOT_RUN**，不能声称最新本地整个工作区仍是60项/6轮通过的那一份。版本证据为 `post-snapshot-drift.json`；受测三份原源码及与当前源码的 diff 保存于 `tested-source/`，从 ECS 受测目录只读取回，未写回原工作区。

本报告的 PASS 只绑定 `final-source-manifest.json` 对应的冻结源码。后续若要替换正式服务，应先决定采用该受测快照，还是把新增 BM25 变动合入后重新做相关验证；不能混用二者的验收结论。

## 本轮确认并修复的缺陷

| 文件 | 修复与理由 | 验证 |
|---|---|---|
| `frontend-weknora/src/sseParser.ts`、`pages/Chat.tsx` | SSE 支持 CRLF/UTF-8 任意传输边界、多行 data、尾帧，不因分包丢事件 | 真实源码状态检查 + Chrome |
| `frontend-weknora/src/chatHistory.tsx`、`pages/Chat.tsx` | 旧 GET 不能覆盖新的流式正文或回退 revision；完成刷新不复用过时请求；旧分页保留实时尾部 | 12 项前端检查中的并发用例 |
| `frontend-weknora/src/chatHistory.tsx` | 损坏浏览器归档行/消息显示聚合警告，只读显示安全字段；导出原字节、导入原数组，不静默改原备份 | 12 项检查 + 11 项本地 Chrome |
| `backend/app/services/conversation_context.py` | 旧文件 `sha256=NULL` 时，使用**已核验 ACTIVE manifest** 的 source_sha256 作为来源身份；两者都有而冲突时拒绝复用。修复 ECS 中真实来源被误拒的问题 | 47 项 Store 检查及 ECS 后续轮次 |
| 同上 | 自称 VERIFIED 及自报正文哈希不能授权任意历史正文；只有与当前有效块精确一致的正文可复用，其余快照仍可展示 | 导入伪造/正文变化/版本变化反例 |
| `backend/app/services/conversation_store.py`、`conversation_runner.py`、`conversation_context.py` | 可选 source_engine 支持本轮只读源库和独立会话侧库；正常产品默认仍使用同一 engine | 60 项后端检查 + ECS |
| `backend/app/services/llm.py` | 查询理解调用前记录应用层请求身份，便于把历史与该阶段对应 | 请求捕获及真实逐轮记录 |

原实现文件、四张会话表、API 和前端接线详见 `docs/server-chat-history-implementation-20261114.md`。本轮没有重构检索、重排、父子扩展、默认 Prompt 或模型规则，也没有增加严格答案校验。

新增验收文件：

- `backend/tests/test_server_conversation_store.py`、`test_server_conversation_api.py`。
- `scripts/conversation_acceptance_app.py`：真实产品链，只替换 Provider HTTP 响应。
- `scripts/conversation_browser_host.py`、`check_conversation_browser.py`：实际构建页面验收。
- `scripts/conversation_ecs_isolation.py`：只读源库/独立日志及会话库；隔离诊断与无正文 HTTP 计数。
- `scripts/run_conversation_live_acceptance.py`、`check_conversation_readback.py`：真实会话及重启/幂等/浏览器回读。

## 本地实测命令与退出码

在 `D:\RAG knowledge` 执行：

```powershell
$env:PYTHONPATH='backend;.'
$env:DATABASE_URL='sqlite:///:memory:'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_server_conversation_store.py backend/tests/test_server_conversation_api.py -q --tb=short --disable-warnings --junitxml=var/server-chat-history-acceptance-20261114/local-final.xml
```

退出码 **0**，**60 passed in 22.76s**。47 项 Store + 13 项 API，不再把修复前 35/48 项累加。875 条现有弃用警告，没有跳过。测试使用真实 SQLite、路由、runner、store、ChatService、BM25、SDK 和 Agent 工具；模型网络响应为合成替身，不能当真实模型质量评测。

| 命令/运行 | 退出码 | 实测 |
|---|---:|---|
| `node var/server-chat-history-acceptance-20261114/frontend-repair-check.cjs` | 0 | 12 passed，模拟 API；执行实际 TypeScript 逻辑 |
| `npm run build --prefix frontend-weknora` | 0 | tsc + Vite，3210 modules，构建通过 |
| `E:\RAG quention\.venv\Scripts\python.exe scripts/check_conversation_browser.py --base-url http://127.0.0.1:18322 --out var/server-chat-history-acceptance-20261114/browser-run4` | 0 | 11/11，真实 Chrome 与本地合成 Provider |
| `E:\RAG quention\.venv\Scripts\python.exe scripts/check_conversation_readback.py --base-url http://127.0.0.1:18323 --artifacts var/server-chat-history-acceptance-20261114/live-run3 --out var/server-chat-history-acceptance-20261114/ecs-browser-readback-final` | 0 | 13/13，SSH 隧道访问真实 ECS 已保存会话 |

E 盘虚拟环境只提供现成 Playwright 驱动，受测源码仍是 D 盘项目，没有调用 E 盘另一个 RAG 项目的业务代码。未安装浏览器/依赖。

本地最初的浏览器失败尝试保留在 `browser-run1/2/3`：分别包含等待/终端编码及定位器问题；最终 run4 通过。没有为这些辅助脚本问题改变产品答案或评测 Gold。一次 `git diff --check` 因当前 chat.py 的行尾空白返回 1，属于格式警告，不当作业务验收失败，也未因此重写整个已有 dirty 文件。

ECS 复用解释器没有 pytest，因此远端 **60 项 pytest NOT_RUN**；没有为补数量安装环境。远端做的是下列真实 HTTP/SSE/持久化与浏览器检查。

## ECS 三次运行，失败不覆盖

共同条件：明确 KB `37f912bb-ea0d-4f66-82eb-fd01426f5992`（caption50 指定父子切块版本）；模型绑定 `a8753099-fe56-403b-a7db-ff457a6403f5`，实际 `Qwen/Qwen3.5-4B`；Hybrid 0.7/0.3、context_window=1、rerank=false、use_rewrite=true。隔离进程路由 OFF 以固定模型，不修改正式路由配置；系统设置 revision=2，历史上限=20。

| 运行 | top_k | 计划/实际/完成 | 结果与退出码 |
|---|---:|---|---|
| `live-run1` | 10 | 6 / 4 / 3 | 退出 2。隔离启动器漏启动受管执行器，普通模式降级 BM25；Agent 超字符预算，后续 2 轮不运行。不能当 Hybrid 通过 |
| `live-run2` | 10 | 6 / 4 / 3 | 退出 2。生命周期修好，真实双路检索可用；Agent 首轮仍超字符预算，后续 2 轮不运行 |
| `live-run3` | 3 | 6 / 6 / 6 | 退出 0。修复旧文件哈希核验后，普通/Agent 连续追问、保存和回读通过 |

合计 14 次用户轮次尝试，不是 14 道独立题；前两次每次各保留 1 个 FAILED 和 2 个依赖 NOT_RUN，无 UNKNOWN 自动重发。top_k 的变化显式单独记录，因此不把第 3 次当作前两次的同配置性能 A/B。

最终运行命令（ECS 隔离目录、现有解释器）：

```bash
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -u scripts/run_conversation_live_acceptance.py --top-k 3 --out live-run3
```

使用临时 systemd unit 独立执行，运行器状态 `ExecMainStatus=0`。隔离 API MemoryMax=768 MiB、CPUQuota=100%、TasksMax=128，限定最大生存时间；这些是本轮隔离上限，不是正式资源配置。

## 最终六轮逐题复核

依据为实际已保存答案及其引用正文，不将 COMPLETED 当正确。源表在 PDF **物理第 45 页 / 印刷页码 43**，两种页码不应误判冲突。原文明确 Trump voters 533、5.1 percentage points；Clinton voters 587、4.9；差值 54。Gold 没有作为额外提示送给产品。

| 模式/轮次 | 问题要点 | 核心 | 全文/引用说明 | 秒 |
|---|---|---|---|---:|
| chat-1 | Trump 样本量和误差 | PASS | PARTIAL：533、±5.1 正确，[1]支持；额外“supported or leaned toward Trump”分组解释不由当前引用支持 | 9.293 |
| chat-2 | 同报告 Clinton 对应值 | PASS | PASS：587、±4.9；[1]印刷页 43 与物理页 45 一致 | 8.019 |
| chat-3 | 比较前两轮样本量 | PASS | PASS：Clinton 多54；[2]为对应表格 | 18.373 |
| agent-1 | Trump 样本量和误差 | PASS | PARTIAL：核心正确；把 half form 解释成嵌套支持者子群体无引用依据，额外列出的第40页并不支持该答案 | 25.126 |
| agent-2 | 同报告 Clinton 对应值 | PASS | PARTIAL：核心正确；275(min)、±7.2 确实在表中，但附加 half form 解释无当前引用依据 | 8.022 |
| agent-3 | 比较前两轮样本量 | PASS | PASS：依据服务端前文做587−533，没有新引用，不冒充重新检索 | 8.436 |

普通模式平均 11.895 秒，Agent 平均 13.861 秒；每组仅 3 轮且包含冷启动，**不能宣称系统 P95 或相对旧版性能收益**。完整复核依据在 `answer-review.json`。补充内容问题是当前引用支持不足，不等于已经核查整份报告都不存在该事实；没有因此在产品里加硬拒答规则。

## 历史与实际送达证据

- 普通 chat-2 的 history_manifest 保存 chat-1 的 turn ID，历史候选 2 条；最终引用中第40页来源标为 `HISTORY_REUSE/VERIFIED`，其正文哈希进入 `grounded_answer` 请求记录。
- chat-3 保存前两轮 turn ID，历史候选 6 条；Agent 的后续轮次分别有 2、5 条历史候选。候选存在不等于全部送入模型。
- 普通最终生成请求记录的来源数为 2、4、3；Agent 最终资料回答为 2、3，最后一轮 agent_chat 为 0（使用对话背景做算术）。
- `PROVIDER_REQUEST_PREPARED` 是应用层 SDK 调用前的正文身份核对，不伪称供应商接收证明或完整线协议。真实 HTTP 另有 16 条 SEND 与 16 条 HTTP 200 头回执；6 个最终模型回答完整结束。SDK 隐式内部格式转换不在该正文投影的证明范围内。
- 幂等重放后 SEND 仍为 16，不产生新轮次。重启后六轮的 ID、答案、引用与原回执逐一相同。
- 终态 usage 保留在逐题 JSON；查询理解 usage 不完整，Embedding 未纳入 Chat HTTP 计数，故完整 Token 总量和账单金额记 **UNKNOWN**，不做虚假费用合计。前两次未启用 HTTP 计数器，不能用 16 表示三次运行总调用量。

## 未关闭项

| 项目 | 状态 | 证据/影响 |
|---|---|---|
| 默认 top_k=10 Agent 大上下文 | FAIL，既有 | 首轮没有任何历史候选，仍在 `agent_workflow.py:enforce_request_budget` 前失败。已有 ToolMessage 同时存 context 和 citations 正文，不能归咎本轮历史重复；需单独评估投影/预算机制，不直接增大上限 |
| 英文误改中文 | FAIL，仍存在 | 最终 6 次理解有 5 次改成中文；Hybrid 仍召回，但不符合语言要求 |
| 补充内容无当前引用支持 | PARTIAL | 上表 3 轮；不通过严格句式规则掩盖模型质量问题 |
| Agent 最后一轮无新行内引用 | NOT_APPLICABLE 于新增检索引用 | 本轮选择 follow_up/agent_chat，计算依据是前两轮；不计入新来源引用分母 |
| Langfuse Cloud 回读 | NOT_RUN | 本轮隔离遥测关闭，使用本地/侧库请求身份与 HTTP 回执；没有假称产生 Cloud Trace |
| 真正用户浏览器全量历史迁移 | NOT_RUN | 只在新浏览器上下文验证合成旧记录和真实新会话，不触碰用户日常浏览器 profile |
| 压测、全题集质量、整体 P95 | NOT_RUN | 本轮不是性能实验或 QA500 全测 |

## 收尾与资源

- 隔离 API 与 SSH 隧道已停止；正式 API PID/状态保持不变。未切正式前端，未启解析 Worker。
- 最终运行所在隔离 API 生命周期实测 MemoryPeak=246,104,064 字节（约234.7 MiB），CPUUsageNSec=3,850,412,000。只代表这次小流量进程，不含共享 Chroma/Redis 的总资源开销。
- ECS 隔离目录约4.5 MB，侧库376,832字节；没有语料/索引副本。保留源码、侧库、三次回执及失败日志用于负责人判断与恢复。
- 本轮浏览器合成 SQLite 清理1,245,184字节，Store历史夹具清理记录另存 `store-cleanup.json`；日志、截图、逐题证据不删除。没有新建容器或虚拟环境。
- ECS 收尾磁盘可用11,755,409,408字节（约10.95 GiB），高于8 GiB保护线；该数值是收尾观测，不能把与启动时粗略11G的差当本轮精确增量。
- 回滚本轮隔离验证只需停止隔离 unit；正式服务尚未迁移，没有生产 schema/索引回滚动作。后续替换时应另备份正式数据库并批准四张会话表初始化，不把隔离侧库自动并入正式数据库。

## 证据入口

本地统一目录：`D:\RAG knowledge\var\server-chat-history-acceptance-20261114`。

- `acceptance-summary.json`：分母、状态、运行统计。
- `local-final.log/xml`：60项最终源码结果；`frontend-repair-check.log`、`frontend-repair-build.log`。
- `browser-run4/browser-results.json`：本地11项；`ecs-browser-readback-final/readback-results.json`：ECS13项及截图。
- `live-run1/2/3`：原始用户请求、完整SSE、答案、引用、配置和每轮回执，旧失败不覆盖。
- `answer-review.json`：逐题原文与引用复核。
- `provider-attempts.jsonl`、`retrieval-metrics.json`、`isolation-api.log`：真实发送、双路候选、失败栈位置。
- `final-verification-v2.json`、`final-source-manifest.json`、`ecs-preflight.json`：源码及源库身份；`final-verification.json`中source_bytes误取目录stat大小，已在v2更正，旧件保留不作为空间指标。
- `post-snapshot-drift.json`、`tested-source/*.diff`：本地后续三文件变动，NOT_RUN；未改写原工作区。

目前没有提交、推送或正式上线。下一步由负责人决定替换范围；若希望默认 Agent top_k=10 也可靠，优先单独处理并验收其工具正文重复与请求预算问题。
