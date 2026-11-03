# 用户长期偏好与 RSI 文件接口本地验收

工作树为 `D:\RAG knowledge`，分支为 `codex/pdf-parser-migration`，开始与交付时的 HEAD 均为 `57f9972b8477b0c4aaf7fac329b911420fafc95e`。验收针对该工作树中的实际源码，包含负责人先前保留的未提交修改。目标目录有 `AGENTS.md`，没有 `progress.md` 或 `docs/adr/`。该项目已有 Redis 客户端、FastAPI 聊天入口、LangGraph、Langfuse 和设置页。本轮复用这些入口。

## 交付与表现

开工时应用配置指向本机回环地址，未单设 `MEMORY_REDIS_URL`，本机 6379 和 6381 均未见监听。仓库没有已有的 Redis 持久化配置，Docker 守护进程也未启动。因此无法把旧环境的 AOF、RDB 或既有备份报告为已启用。以下实测使用新的本地配置，未读取 ECS 的配置。

| 项目 | 本次结果 | 证据与适用范围 |
|---|---|---|
| 显式长期记忆 | PASS | 验证附件的五种明确表达、三种撤销操作、普通陈述、引用、否定、一次性要求和历史消息重放。单纯记忆命令无需模型，混合指令会在确认偏好后继续回答剩余问题 |
| 默认值与身份 | PASS | 未设置不会创建 Key；显式选默认值和显式空数组能保留来源。认证替身分别模拟两个用户，经 HTTP 测试读、删和伪造身份参数；一方无法改变另一方的偏好。当前实际发布接口仍采用个人全局身份 |
| 普通聊天 | PASS（模型替身） | 通过 `ChatService.chat_stream → _chat_stream_normal`，无库时捕获 `generate_stream` 消息，有资料时捕获 `generate_grounded_candidate` 传给客户端的请求。偏好在选择模型后加入，资料与引用仍在原区块。已验证本轮详细要求覆盖长期简洁、单次表格要求不写 Redis |
| Agent | PASS（实际图、模型和检索工具替身） | `_chat_stream_langgraph` 将同一轮解析的策略交给图。闲聊走 `generate_chat`，有知识库走 `agent_node`。真实运行两轮检索的图，对比默认组和显式偏好组的工具、评分、意图输入和调用数。强制检索和已安排的重试不附加风格指令，有证据且可回答时才附加。判断直接使用评分节点的实际调度结果，覆盖第三次已调度重试边界 |
| Redis 故障 | PASS | 真实 redis-py 连接本机不监听端口、超时替身、两种问答路径均有覆盖。读失败记录 warning，沿用默认表达。纯记忆写入无法确认时 SSE 以 `error / MEMORY_UNAVAILABLE` 结束；混合问题继续问答。已写入但回读失败有独立提示 |
| 真实存储与恢复 | PASS | Redis 7.0.15，真实 SSE 保存后由管理 GET 读回。两个并发输出偏好均保留。Hash 无过期时间，AOF 开启并每秒落盘，RDB 条件为 `900 1 300 10`，淘汰策略为 `noeviction`。先备份再改解释深度为 expert，原目录重启后读到 expert；独立 RDB 恢复读到备份时的 intermediate/default，重新开启 AOF 并再重启后相同 |
| 设置界面 | PASS（本机 Chrome） | 构建出的设置页通过真实 HTTP、FastAPI 和临时 Redis，完成三字段操作、多选、取消选择、刷新回读、单项和全部恢复，核对页面来源及实际 Hash。页面上的其他模型设置接口提供固定空数据 |
| Langfuse | PASS（本地 SDK 导出）；Cloud 回读 NOT RUN | 真正使用 Langfuse 5.0.4 与内存 OTel exporter，从导出数据核对 `memory.preferences` 的读取状态、三个有效字段及来源、Redis 降级标记，校验无任意测试私文。新字段仅允许固定值。没有访问云端项目 |
| RSI 文件接口 | PASS（公开合成夹具） | 校验 UTF-8 JSONL、唯一修订身份、前驱摘要、证据、独立 PASS 回执形状、不可静默改写、坏行和截断末行。独立可信的文件摘要能检测末条改写或尾部删除。验证者是否真正独立、引用事实是否正确仍需审阅 |
| 真实模型回答效果、ECS 持久化及备份调度 | NOT RUN | 本轮业务模型和提案模型外部调用均为 0。ECS 生产进程、Redis 设置、数据库和索引均未接触 |

默认风格为 normal、深度为 intermediate，默认格式为空。无显式要求时无需追加默认 Prompt。输入来自设置页或明确的聊天命令，只允许四种输出偏好标签。常用中文句式及部分英文有规则覆盖，不能识别的自然语言不会持久化。更改只影响回答表达。Agent 的工具选择和最终回答沿用原有共享模型节点，测试证明了程序送达边界及替身工具输入一致；真实模型是否每次都遵从风格属于独立的效果验证。

## 修改文件

| 文件（仓库相对路径） | 职责 |
|---|---|
| `backend/app/services/memory.py` | `MemoryService`、三字段校验与默认值、服务端身份、稀疏 Redis Hash、原子增减、UTC 修改时间和失败语义 |
| `backend/app/api/memory.py`、`backend/app/main.py` | 精确挂载 `/api/memory/preferences` 的四个管理操作 |
| `backend/app/core/config.py`、`backend/.env.example`、`config/redis-preferences.conf` | 可选独立 Redis URL 及持久化示例 |
| `backend/app/services/memory_commands.py` | 显式修改、撤销、本轮覆盖、表达策略及旧命令历史的处理 |
| `backend/app/api/chat.py`、`backend/app/services/chat.py` | 受信身份入参、每轮解析、SSE 确认／失败与普通聊天接线 |
| `backend/app/services/grounded_generation.py`、`backend/app/services/agent_workflow.py` | 在现有模型请求中加入独立表达指令，复用相同的偏好快照 |
| `backend/app/services/observability.py` | 允许导出的偏好字段、来源枚举 |
| `frontend/src/services/memory.ts`、`frontend/src/pages/Settings/PreferenceCard.tsx`、`frontend/src/pages/Settings/index.tsx` | 请求契约、独立的设置卡片、设置页入口 |
| `backend/tests/test_memory_service_api.py`、`backend/tests/test_memory_chat.py`、`backend/tests/test_memory_live.py` | 存取与身份、普通／Agent 请求与 Trace、真实 Redis 恢复和浏览器 |
| `scripts/rsi_playbook_verify.py`、`backend/tests/test_rsi_playbook_contract.py` | 只读验证接口、CLI 和文件契约回归 |
| `docs/rsi-file-memory-v1.md`、`docs/examples/rsi-playbook-v1.jsonl`、`docs/examples/rsi-synthetic-evidence-v1.md` | RSI 文件布局、ACE 字段和公开的合成经验／证据对 |
| `README.md`、`docs/user-preference-memory.md`、`docs/user-preference-acceptance.md` | 使用入口、备份恢复与本次验收 |

个人记忆的 Redis Key、字段和多用户迁移边界见[运行说明](user-preference-memory.md)。RSI 采用独立的文件接口，Playbook 经验要先有实验观察，再作为 candidate 入档，有独立验证才能升级；目录、状态转换和示例见[文件规范](rsi-file-memory-v1.md)。没有创建第二个登记系统，也没有让 Agent 自动写经验或改生产代码。

## 命令、计数与证据

以下命令从 `D:\RAG knowledge` 执行，Python 3.13.0、FastAPI 0.115.5、redis-py 8.1.0、Langfuse 5.0.4、OTel SDK 1.45.2、pytest 8.3.5。仓库原有的根 requirements 固定 redis-py 7.1.0，此次实测版本为本地已有的 8.1.0。未改依赖或安装项目包。

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:DATABASE_URL='sqlite://'
$env:MEMORY_REDIS_URL='redis://127.0.0.1:1/0'
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:LANGFUSE_ENABLED='false'
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/final-offline.xml backend/tests/test_memory_service_api.py backend/tests/test_memory_chat.py backend/tests/test_rsi_playbook_contract.py backend/tests/test_normal_evidence_guard.py backend/tests/test_agent_request_parameters.py backend/tests/test_agent_quality_outcomes.py backend/tests/test_dynamic_routing_observability.py backend/tests/test_langfuse_sdk_export.py
```

退出码 **0**，114 项通过，0 跳过，11 条依赖弃用警告。逐项 JUnit 中有存储和 API 9、聊天和 Agent 46、RSI 格式 7、既有相关回归 52。普通与 Agent 的真实模型均用替身。原始输出在 `var/preferences-acceptance/final-offline.log` 和同名 XML。

本机保留一份经 Ubuntu 包清单 SHA-256 核验的独立 Redis 测试二进制。下面的路径指针只在本机，复用它即可。其他环境有 `redis-server` 时可直接通过 PATH 使用；已在 WSL 安装它的机器也可把最后一个命令参数设为绝对的 Linux 可执行文件路径。

```powershell
$bin = (Get-Content -Raw -LiteralPath 'var/preferences-acceptance/redis-test-runtime.txt').Trim()
$env:RAG_TEST_REDIS_CMD = ConvertTo-Json -Compress -InputObject @('wsl.exe','-d','Ubuntu','--exec',$bin)
$env:RAG_TEST_BROWSER = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
& .\.venv\Scripts\python -c "import sys; sys.path.append(r'E:\RAG quention\.venv\Lib\site-packages'); import pytest; sys.exit(pytest.main(sys.argv[1:]))" -p no:cacheprovider -q -s --tb=short backend/tests/test_memory_live.py
```

退出码 **0**，2 项通过，0 跳过，9 条依赖弃用警告；输出见 `var/preferences-acceptance/final-live.log`。测试自行分配回环端口、凭据和临时数据目录，不采用应用的 Redis URL。首次启动曾遇 WSL 临时程序目录随实例退出消失、Windows 文件夹占用和浏览器过早断言；将运行程序放在可复用的目录，显式指定测试数据路径，并等待服务端确认后，完整重跑成功。验证的是进程重启及独立备份恢复，未模拟主机断电。

使用同一测试配置执行 `CONFIG GET appendonly appendfsync save maxmemory maxmemory-policy bind`，回读值分别为 `yes`、`everysec`、`900 1 300 10`、`0`、`noeviction`、`127.0.0.1`。验证退出码 0，原始结果见 `var/preferences-acceptance/redis-verified-config.json`。

| 其他检查 | 退出码 | 结果 |
|---|---:|---|
| `node frontend/node_modules/typescript/bin/tsc -p var/preferences-acceptance/ui-tsconfig.json --pretty false` | 0 | 对新增设置组件和服务的严格类型检查通过 |
| `npm run build`（frontend 下） | 1 | 改动前后各 45 条 TypeScript 诊断。文件、错误码及信息完全相同。独立 Vite 打包与实际浏览器流程均通过。原始输出及对照保存在 `var/preferences-acceptance/frontend-full.log` 和 `ui-compare.json` |
| `python scripts/rsi_playbook_verify.py docs/examples/rsi-playbook-v1.jsonl` | 0 | 合成经验格式可读取；故意给错误的外部摘要退出 1，符合预期 |

## 版本、资源与提交建议

保留原分支和所有原有工作区改动，无提交、推送或服务切换。工作目录没有持续运行的本任务 Redis、Vite 或测试 API。使用原有 Python、Node、Chrome 和 WSL。约 7.3 MiB 的 Redis 测试运行程序专用于下次复跑；已清理下载包和测试数据库。小型日志、Git 补丁、运行程序路径仅留在被忽略的 `var/preferences-acceptance/`。测试插件临时写出的旧式报告已与本轮证据分开，原有受版本管理报告恢复为开始状态。

开始时 D 盘可用 133.596 GiB，收尾抽样为 133.527 GiB。这是整盘读数，不能据此计算本功能的独占开销。本轮下载的 4 个包共 1,481,976 字节，已清理。保留的 Redis 程序位于 WSL 的独立目录，路径记录在本地验证目录，便于下次直接用于验收。两个首次失败遗留的临时数据目录也已清理。

本地补丁 `var/preferences-acceptance/memory-only.patch` 只含上表 26 个文件中的本轮变更，可用 `git apply --stat var/preferences-acceptance/memory-only.patch` 审阅。已在临时目录逐文件验证反向、正向应用后的内容，并在实际工作树只读执行 `git -c core.whitespace=cr-at-eol apply --reverse --check var/preferences-acceptance/memory-only.patch`。由于已有未提交的聊天、模型和配置修改，建议按补丁中的差异单独暂存。可分为 `feat(memory): add explicit Redis answer preferences` 与 `docs(rsi): specify and verify versioned playbook records` 两个提交。暂未实施自动提取、语义搜索、真实用户认证、知识库级偏好覆盖及 Playbook 自动写回。
