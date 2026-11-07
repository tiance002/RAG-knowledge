# Tavily 联网接入与五角色 RSI 循环验收（2026-11-05）

本报告以本地源码、原始回执和命令退出码为准。不会把模型请求成功等同于业务质量改善。未提交、推送、部署或切换正式服务。

## 1. 工作树与统一配置

- 产品工作树：`D:\RAG knowledge`，分支 `codex/pdf-parser-migration`，起始 HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`。
- 现有 RSI 工作树：`E:\codex_workspace\2026-11-03\task\rsi-work`，分支 `codex/rsi-receipt-decisions`，HEAD `aeb2c89bc66d724472621c581107093d7dd2ad32` 及既有未提交成果。
- 两处原有改动保留；复用既有 `.venv`、registry、冻结工程输入与回执。没有复制整库或索引，没有创建容器。
- 用户自行填写 `D:\RAG knowledge\config\runtime-secrets.env`。该文件已被 Git 忽略且未跟踪；公开模板是 `config/runtime-secrets.env.example`。本次没有把桌面凭据迁入仓库、打印密钥或把密钥写入证据。
- 文件中使用 `TAVILY_API_KEY`、`OPENAI_API_KEY`；OpenAI 当前固定官方 Responses 地址。`OPENAI_BASE_URL` 保留官方值，当前 RSI 适配器不会据此切换代理端点。
- ECS 可用 `RAG_RUNTIME_SECRETS_FILE=/受控位置/runtime-secrets.env` 或服务的 env-file 注入。同一机制可迁移，但本轮没有部署 ECS，工程输入的 Windows 路径也不代表已完成 ECS 迁移。
- 后端配置变更须重启开发进程才能加载。产品读取优先级为进程环境 > 统一文件 > 原 backend/.env。RSI 优先使用进程 `OPENAI_API_KEY`，其次显式选定的统一文件。

## 2. 联网功能与真实验收

聊天页增加默认关闭的联网开关。开启后走共享的 `WEB_SEARCH_GROUNDED` 分支：当前问题 → Tavily → 网页证据 → 既有模型生成 → SSE 与网页引用。

普通模式和 Agent 模式都能使用该分支，但它是明确标识的网页问答流程，**不是 Agent 自主搜索工具循环，也不混合私有知识库**。不会把 KB 全文、历史消息送给 Tavily。网页内容按不可信资料处理，引用保留真实 URL，不伪造文件/Chunk ID。

Tavily 单次请求：`basic`、最多 5 条、`include_answer=false`、不取 raw content/图片、`include_usage=true`、有总超时、不自动重试。失败、UNKNOWN、未配置均通过 SSE error 结束；无结果说明证据不足；截断或空生成不冒充完整成功。

依据的官方资料：

- [用户指定的 Tavily setup Skill](https://tavily.com/agent-setup/SKILL.md)，采用应用接入路径 B/E；未安装无关 CLI。
- [Tavily 官方 SDK/REST Skill](https://github.com/tavily-ai/skills/blob/main/skills/developers/tavily-sdk/SKILL.md)。旧 setup 中的 best-practices 路径返回 404 后核对了当前官方目录，没有按不存在的文件实施。

### 实际结果

|检查|结果|证据|
|---|---|---|
|后端定向及受影响回归|PASS，110 passed，退出 0|`var/tavily-rsi-20261105/backend-offline-tests.log`|
|前端引用/开关/SSE 定向测试|PASS，13 passed，退出 0|`var/tavily-rsi-20261105/frontend-tests.log`|
|Vite 打包|PASS，退出 0；既有大包警告|`var/tavily-rsi-20261105/frontend-build/`|
|TypeScript 全项目检查|FAIL，45 条；改前/后同集合，新增 0|`var/tavily-rsi-20261105/frontend-typescript-comparison.json`|
|真实 Tavily + 产品 ASGI HTTP/SSE|PASS，退出 0；1 次搜索、5 条来源、回执 1 credit|`var/tavily-rsi-20261105/live-smoke-backend-20261105/manifest.json`|
|真实云端模型联网回答质量|NOT_RUN|本次生成网络调用使用本地替身|
|浏览器交互/正式部署启动|NOT_RUN|未声称真实浏览器或生产验证完成|

真实搜索公开问题：`Python official documentation len() built-in function`。供应商 request_id：`1ecc443a-9ea1-495c-91ec-60bb18c25c7b`。原始搜索结果与 SSE 的 5 条标题、URL、摘要逐项一致；全部来源 URL/正文进入实际构造的模型请求。

原始证据目录包括 `tavily-request.json`（无鉴权头）、`tavily-response.json`、`model-request-SYNTHETIC.json`、`response.sse`、`sse-events.json` 和 `manifest.json`。只替换模型网络那一跳；为避免操作真实存储，未运行应用 lifespan，冒烟子类省略未用的 Retrieval/Chroma 初始化，真实偏好服务使用默认回退。这些隔离限制在 manifest 中逐项记录。

```powershell
Set-Location 'D:\RAG knowledge'
& '.\.venv\Scripts\python.exe' scripts/tavily_smoke.py --live --output ("var/tavily-rsi-20261105/web-" + [guid]::NewGuid().ToString('N'))
```

不带 `--live` 只说明计划，不发请求。带该参数会新增一次 Tavily 查询；该命令不会调用真实业务模型。

## 3. RSI 角色、权限与控制器

|角色|模型|推理档位|作用|
|---|---|---|---|
|诊断|gpt-6-luna|max|读取父代选择器与实际失败回执|
|分析优化|gpt-6.1-sol|high|提出最小修改计划与反证条件|
|实施|gpt-6.1-sol|medium|返回单一选择器补丁|
|评估|gpt-6-luna|max|阅读程序复判，提出建议，不覆盖冻结分数|
|经验更新|gpt-6.1-terra|xhigh|追加绑定运行及父子身份的未验证经验候选|

继续复用 `run_rsi_loop.py` → `derive` → `materialize` → `run_rsi_offline.run` → `review_run`。没有第二套候选执行器。候选只可修改 `select_member_indices`，不能修改评分器、Gold、预算或父代。模型没有 shell/文件工具。

程序依据真实请求捕获决定停止、保留父代或继续。经验账本追加保存，不把模型自评晋级为 validated；后续迭代实际读取有限条、绑定仍有效的候选经验。经验生成 UNKNOWN 不会抹掉已产生的确定性回执。

离线初始验证 40 passed、退出 0，覆盖多轮继续、回归后保留父代、经验消费、UNKNOWN 不重发和建议不能覆盖判定。后续接口修复仅运行受影响测试，数量不与这 40 项相加。

### 真实运行结果

真实调试回执全部保留，没有重置或覆盖。此前三次启动均退出 1，不能写为通过：

|目录|实际请求|结果和处置|
|---|---:|---|
|E `var/rsi-roles-20261105/live-01`|1|诊断请求 HTTP 400。JSON 模式要求 input 包含 JSON 指令，已修复请求格式；不声称取得模型输出或已知费用。|
|E `var/rsi-roles-20261105/live-02`|1|诊断 HTTP 200/completed；内容正确，但 evidence 使用字符串列表，被过严的本地字符串契约拒绝。旧 UNKNOWN 回执不改写；新复用记录通过原始响应澄清它是本地解析问题，不是网络结果未知。|
|E `var/rsi-roles-20261105/live-03`|2，另复用诊断 1 次|分析、实施都完整返回；候选已入档，配对前因 E 工作树业务代码不同于冻结 SHA 被拦截。没有发生 A/B 业务回放，没有评价/经验调用。|

兼容性修复：说明字段接受非空字符串或字符串列表，代码补丁仍保持必要的可执行契约；已 completed 的解析失败记 INVALID_OUTPUT。增加只读的 `--reuse-completed-roles`，要求请求内容及输入哈希一致、HTTP 200、原始响应 completed、返回模型一致，再归一化读取。复用单独记 REUSED 及原用量，不计入新请求/新 Token，也不重发原请求。

恢复后的实际角色循环及配对结果见下方最终记录。工程恢复和冻结评分分别报告，不因为接口命令退出 0 就把冻结 FAIL 改成 PASS。

### 最终实际记录：live-04

控制器命令退出 **0**，run_id `81518100f4984dffbd1070988bd6704b`，自动停止原因 `STOP_RECOVERED`。本次启动复用 3 个已完成角色响应，仅新调用 evaluation 和 experience；并非另外重做五次生成。

|事项|A：直接父代|B：真实 Agent 提案子代|
|---|---|---|
|候选 ID|`0f178f4d77c2532e322f991ef5bd733ee8a371bb61dbd9d55de439d26358117e`|`0dc4cde2543c6e7c6e58b460645f5e04fe787ae35cb152902a43b08494fff8d8`|
|实际成员|row|header → row|
|数值行 `春季 / 120`|PRESENT|PRESENT|
|表头 `季度 / 收入`|ABSENT|PRESENT|
|单位 `万元`|ABSENT|PRESENT|
|上下文字符 / UTF-8 字节|32 / 46|72 / 118|
|客户端请求捕获次数|1|1|
|健康参照|缺表头与单位|MATCHED_HEALTHY_CAPTURE，完整捕获哈希一致|

总题数 1，有效配对 1，阻断 0，本地请求捕获 2，RAG 外部调用 0。已读实际 capture，重算 SHA-256、字符及 UTF-8 字节，并确认 context 出现在客户端收到的 messages 中。不是仅依据内部选中成员判定送达。

唯一补丁由 `gpt-6.1-sol / medium` 实际返回：把 `return selected[-1:]` 改成 `return selected`。模型未修改评分器、业务 Prompt 或 Gold。程序完成入档、独立物化、A/B 回放、复判及停止；后续开发父代更新为子代，正式晋级仍 NOT_RUN。

冻结结果继续为 **FAIL**：除父代已有缺失外，旧口径还要求长度相同；恢复结构使字符增加 40、字节增加 72。`review_run` 保留原评分及 `runner_exit_by_contract=1`，另给出 `RECOVERED_WAIT_FOR_NEW_OBSERVATION`。本轮由控制器直接调用 `run()`，没有另行启动离线 CLI，因此这里的 1 是原 CLI 契约退出码；真正执行的控制器进程退出码为 0。

实际命令（历史已执行目录，不要覆盖重跑）：

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
$env:RAG_RUNTIME_SECRETS_FILE = 'D:\RAG knowledge\config\runtime-secrets.env'
$env:RAG_TEST_EXTRA_SITE_PACKAGES = 'E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_loop.py --config experiments/loop/header-roles.json --out var/rsi-roles-20261105/live-04 --execute --approved-proposals 1 --approved-model-calls 4 --reuse-completed-roles var/rsi-roles-20261105/live-03/iteration-01
```

代码接线修复发生在 live-01～03 之间，由开发代理完成。live-04 启动以后，没有人手写候选补丁、粘贴报告或指定下一步动作。不能把整个调试过程描述为从第一次启动起就无需开发介入。

### 请求、用量与模型建议的局限

|角色|实际模型|输入 Token|输出 Token（含供应商 reasoning）|该次调用秒数|
|---|---|---:|---:|---:|
|诊断|gpt-6-luna|659|629|8.941|
|分析|gpt-6.1-sol|795|586|16.333|
|实施|gpt-6.1-sol|1,085|291|8.376|
|评估|gpt-6-luna|2,124|4,083|54.694|
|经验|gpt-6.1-terra|2,687|1,274|19.402|

共 **6 次 Responses HTTP 请求：1 次 400，5 次 completed**。上述 5 次可确认用量合计输入 **7,350**、输出 **6,863** Token；复用不重复累计。400 没有 usage，费用总额 UNKNOWN，不能把它记成已知免费。另有一次只读 models 预检，不计为生成。当前没有未解决的传输 UNKNOWN；旧 live-02 的误分类保留原记录，并通过 completed 原文及输入身份澄清。

**程序循环工程验收 PASS；模型建议质量 PARTIAL。** evaluation 和 experience 都把“已执行补丁回放”误写成“提案尚未运行”，虽然它们正确识别了恢复的表头/单位和冻结 FAIL。原始输出不修改，不把它们升级为可信经验。此类建议仍显式为 `UNVALIDATED_CANDIDATE`，可能供后续角色作为待验证建议阅读，不能作为判定依据。

已对后续输入补充程序生成的 `executed_change`：run/child 身份、提案及归档 worker 哈希、匹配状态、实际配对数和额外建议用例 NOT_RUN。相关 26 项离线测试退出 0；这项输入改善尚未再次调用真实模型验证。没有加入新的答案格式/数值裁判，也没有为消除这条建议误判继续花费模型调用。

### 归档与可复制入口

E 工作树 `var/rsi-roles-20261105/` 中：

- `live-01`～`live-04`：原始输入、响应、派发记录、角色回执、候选及逐题记录。
- `live-04/loop-result.json`、`live-04/iteration-01/decision.json`、`run/results.json`、`run/case-001/{A,B}/capture.json`：本次闭环的直接证据。
- `cross-review.json`：根代理对实际 capture、长度、哈希、分母和唯一新请求数的复核。
- `experience-candidates.jsonl`：追加的经验候选，尚未独立验证。
- `offline-tests.log`、`reuse-fix-tests.log`、`business-source-tests.log`、`executed-context-tests.log`：不同阶段定向测试，**包含重叠用例，不能相加当总数**。

先做零调用预检：

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
$env:RAG_RUNTIME_SECRETS_FILE = 'D:\RAG knowledge\config\runtime-secrets.env'
$env:RAG_TEST_EXTRA_SITE_PACKAGES = 'E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_loop.py --config experiments/loop/header-roles.json --out var/rsi-roles-20261105/preflight --preflight
```

同一入口启动新的工程循环的调用方式如下，会产生新的模型请求。已恢复案例没有必要重复跑；实际下一轮应绑定新的真实失败观察和对应冻结材料。

```powershell
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_loop.py --config experiments/loop/header-roles.json --out ("var/rsi-roles-20261105/run-" + [guid]::NewGuid().ToString('N')) --execute --approved-proposals 1 --approved-model-calls 5
```

此轮没有自动发现新的自然业务故障、扩大候选修改范围或运行正式 E1c；具备的是受控工程循环与可审计的恢复能力。

## 4. 改动范围和未完成事项

产品侧：`backend/app/services/web_search.py`、`chat.py`、`backend/app/api/chat.py`、`backend/app/schemas/schemas.py`、`backend/app/core/config.py`、`backend/.env.example`、两个 web_search 测试；`frontend/src/pages/Chat/index.tsx`、`WebCitation.tsx`、`frontend/src/types/chat.ts`、`frontend/tests/web-search.test.mjs`；`scripts/tavily_smoke.py`、统一配置模板、Git 忽略规则及本报告。

RSI 侧：既有 `scripts/run_rsi_loop.py`、`scripts/run_rsi_offline.py`，新增 `scripts/rsi_roles.py`、`scripts/rsi_roles_loop.py`、`experiments/loop/header-roles.json`、`backend/tests/test_rsi_roles.py`、`backend/tests/test_rsi_business_source.py`、`docs/rsi-roles-loop.md`。业务基线改为显式只读复用已有 `D:\RAG-R0`，不是放松来源或版本校验；prepare/deliver 都由归档 worker 加载该基线。没有改写 D:\RAG-R0。

本次保存的源码版本在 D 的 `var/tavily-rsi-20261105/source-versions.json`。实际复用依赖：Python 环境中 FastAPI 0.115.5、Pydantic 2.14.1、pydantic-settings 2.12.0、httpx 0.28.1、OpenAI 3.40.0、langchain-openai 1.7.3。未安装或升级依赖；RSI 角色传输直接使用标准库 HTTP。

E1c 正式数据仍 BLOCKED，正式效果/晋级 NOT_RUN。本轮工程表头丢失案例是已知故障夹具，不能证明系统已自动改善真实 RAG 问题。没有新的独立留出集，也没有重跑 250 题。未进行新的 Langfuse Cloud 回读。

## 5. 定向命令、复核与资源

产品目录执行的后端命令（退出 0，110 passed）：

```powershell
Set-Location 'D:\RAG knowledge'
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_web_search.py backend/tests/test_web_search_chat.py backend/tests/test_empty_kb_preflight.py backend/tests/test_memory_chat.py backend/tests/test_normal_grounded_answer.py -q
```

前端目录执行（依次退出 0、0、1）：

```powershell
Set-Location 'D:\RAG knowledge\frontend'
node --test tests/web-search.test.mjs tests/chat-stream.test.mjs
.\node_modules\.bin\vite.cmd build --outDir ../var/tavily-rsi-20261105/frontend-build
.\node_modules\.bin\tsc.cmd --noEmit
```

RSI 初始受影响集退出 0，40 passed：

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_roles.py backend/tests/test_rsi_loop.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_candidates.py --disable-warnings --tb=short
```

业务源隔离改动后，`test_rsi_business_source.py`、`test_rsi_roles.py`、`test_rsi_loop.py` 共 39 passed、退出 0；其中 4 项验证业务源选择及归档 worker 接线。最后一次 executed_change 输入调整，只重跑下列受影响文件，26 passed、退出 0：

```powershell
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_roles.py --disable-warnings --tb=short
```

最终零调用预检退出 0，`final-preflight.log` 显示实际可读取 1 条 UNVALIDATED 经验候选；首请求 4,081 UTF-8 字节，模型调用 0。读取候选不等于验证其内容正确。

复核已覆盖：Tavily 原始响应到 SSE/模型请求的匹配；角色真实模型身份与 usage；所有启动的退出码；冻结分母；A/B 客户端请求的正文、来源、顺序、哈希及长度；候选补丁与健康参照；模型评估建议与程序回执间的矛盾。没有用格式测试全绿代替这些检查。

本次证据与版本备份检查时合计约 **3.25 MiB**（含约 2.38 MiB 前端构建产物）。没有新增持久化数据库、Chroma 索引副本、容器或依赖环境，也没有删除历史数据。保留产物用于审查和恢复；全盘前后剩余空间没有连续测量，不能声称已知磁盘净变化。最终扫描 210 个本轮产物文件，没有发现已配置密钥泄漏；配置文件本身不在扫描输出或版本归档中。检查记录：D `var/tavily-rsi-20261105/final-resource-secret-check.json`。

下一轮应优先接入一条仍在当前版本复现的自然失败及冻结输入。评估/经验 Agent 的执行事实理解需要再次验证，但不应让其覆盖确定性判定；本轮不自动继续付费调用，不重复本工程案例，不自动晋级或上线。
