# RSI v0 DEV_DEBUG 原型验收

复用了同一历史轮的六个 OpenAI 回执，按原始请求、响应、传输与费用记录校验。历史角色版本由归档槽位识别为 `legacy/v1`。Diagnostician 输出通过现有契约。P1、P2 各生成一个真实 Patch，P3 主动弃权。Curator 弃权，Meta 返回历史评议。两项评议的适用范围均为原轮次。

## 实际候选与对照

| 槽位 | 真实 Patch | AST／函数签名 | 实际配对 | 选中成员正文字符 |
| --- | --- | --- | --- | --- |
| P1_DEDUP | 在已选非 seed PDF 成员中按完整原生位置与正文键去重 | 通过 | 14/14 | 195,469 → 195,469 |
| P2_BUDGET | 将非 seed 的双倍得分与最高 seed 分的比较从 `>=` 改为 `>` | 通过 | 14/14 | 195,469 → 194,650 |
| P3_ORDER | 归档角色因无法在其当时的职责下满足约束而弃权 | 无 Patch | 无候选 | N/A |

P1 在冻结输入上的成员选择全部相同。P2 在历史 `PARTIAL` 题 `legacy-session2-v5-s1-t02` 移除一段 516 字符的非 seed 成员，在 `qst_0744` 移除一段 303 字符的非 seed 成员。两者合计减少 819 字符（0.42%）。两候选共 28 个已完成配对，六道历史正确题在两个候选中都保持相同的成员选择。首跑两臂和两个候选都在 `qst_0744` 触发原型指令上限。将离线沙箱的上限从 20 万调到 200 万后全部跑完，首跑日志保留。这个改动属于执行器工程修复。

历史分组是 2 道 INCORRECT、6 道 PARTIAL、6 道 CORRECT。本轮在真实原文归档的 Native Fixture 成员池上执行六参数 selector，包括 query、原生 seed、Chunk、文件与成员。历史线上 SSE 扩展池、问答生成与引用评议不在此次执行范围内。两款候选各 14 道必要事实评价为 UNKNOWN，已审失败修复分母为 0，已审回归分母为 0。因而事实修复数与回答回归数均为 UNKNOWN。字符口径只计选中成员正文。详见[冻结清单](evidence/devloop-cases.json)、[输入说明](evidence/devloop-cases.md)和[逐题、Patch、AST 与退出记录](evidence/devloop-results.json)。

## 下一次调试

优先看 P2 删除的两段是否仍提供必要的条件、数值单位或跨成员关系。P2 的主要可见问题是用得分边界决定删除，反馈里没有逐段说明能否保留必要证据。旧 P3 的弃权只说明当时的排序职责产生了约束冲突。首次获批的新生成轮使用固定的 `M1_SPECIALIZED_V1`。若其结果复现同类风险，下一轮只改 P2 任务说明，要求对拟删非 seed 成员提供可核对的替代证据，无法指出替代证据时保留。以相同 14 题和执行器比较，保留所有结果。已归档的重跑没有改变 Agent 策略。

## 运行与费用

从项目根目录运行。已有的 DEV-only 文件和历史回执使用原路径与原字节。输出目录必须是 `var/devloop` 下的新目录。

```powershell
$devPython = 'D:/RAG knowledge/.venv/Scripts/python.exe'
$devArchive = 'E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-v22-20261115'
$devFixture = 'E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-v23-qa300-selected-20261116/gold-native-literal-r5/main/cases.json'
$devSelection = 'D:/RAG knowledge/var/qa500-full300-20261115/selection-20261116/DEV_20_question_ids.csv'
& $devPython -B -m rsik.dev_debug_cases --fixture $devFixture --dev-selection $devSelection --output var/devloop/cases.native-dev14.json --metadata-output var/devloop/cases.reproduced.json
& $devPython -B -c "import sys; from rsik.dev_debug_receipts import freeze_archive; freeze_archive(sys.argv[1], sys.argv[2])" $devArchive var/devloop/archive.json
& $devPython -B scripts/run_rsi_devloop.py --cases var/devloop/cases.native-dev14.json --archive var/devloop/archive.json --ledger "$devArchive/task-model-budget.sqlite" --out var/devloop/reproduction
```

已有冻结文件可直接从第三条命令开始。原始 `baseline.json`、各候选 `patch.json`、`worker.py`、`paired.json`、度量日志和原始 provider 回执保留在本地。仓库中的逐题证据仅含 DEV 题号、历史分组、成员 ID、字符数、状态与哈希。最终重放耗时 9.851 秒。历史模型费用上界 USD 0.00206205 已记在原账本，历史模型可验证耗时为 UNKNOWN。本次新请求数为 0、增量费用为 USD 0。原账本 668 事件，264 次请求中 5 次用量 UNKNOWN 且已有保守上界，总上界 USD 1.370204365，按原 USD 5 上限计算余量 USD 3.629795635。六个保护文件与原账本 SHA256 核对通过。

M1_SPECIALIZED_V1 的真实生成停在付费授权边界。原型已输出六个角色各一次、无重试的成本计划，按库内费率估计最高 USD 0.019456。实际付费前需重新确认价格、输入体积和账本，单轮授权以 USD 0.02 为上限。当前 CLI 可直接执行归档轮次，新发请求适配仍待授权后接通。回放状态限于 DEV_DEBUG。
