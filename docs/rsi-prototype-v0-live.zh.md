# DEV_DEBUG 真实开发轮与角色路由

## 角色路由

DEV_DEBUG 固定使用 `M1_SPECIALIZED_V1`。三个独立补丁槽位 `P1_DEDUP`、`P2_BUDGET`、`P3_DEPENDENCY` 由 `gpt-6-luna` 以 `max` 推理生成可执行 Patch。三个槽位从同一基线和诊断出发。补丁应用、AST 与签名校验、逐题配对回放由确定性代码完成。Diagnosis、Curator、Meta 沿用本次费用批准中的 Luna 路由。

RSI 原有使用 Sol 的分析和编码失败升级路径改为 `gpt-6.2-sol`、`reasoning.effort=medium`。直接 Responses 脚本中的 `implementation` 明确接收方案并输出源码替换，因此用 Luna max。旧 Sol 请求仍可读取和计费。正式流程中的模型或价格版本变更需要按现有规则重新冻结。[[Sol API 模型与价格](https://developers.openai.com/api/docs/models/gpt-6.2-sol)] [[Luna API 模型与价格](https://developers.openai.com/api/docs/models/gpt-6-luna)]

## 本轮条件

复用原 `OPENAI_API_KEY`、已有本机 loopback 代理和累计 USD 5 账本。申请范围为一次独立 DEV_DEBUG 轮次，最多六次 Luna 请求、无重试、增量上界 USD 0.02。Preflight 核对两款模型的官方元数据响应，均为 HTTP 200 且 ID 精确匹配。读取前账本共 264 次请求，费用上界为 USD 1.370204365，5 次 UNKNOWN 用量已有保守上界。六个保护文件的 SHA256 与冻结值一致。元数据检查消耗 0 次生成请求。

配对输入沿用 [DEV14 冻结清单](evidence/devloop-cases.json)，由 8 道历史失败类和 6 道历史正确类构成。模型可见输入只含允许的聚合及父代选择器。事实和回答结果沿用已审核分母规则。

请求固定在 Standard 层，每个请求正文最多 16,384 字节。三个补丁槽位分别最多 4,096 个输出 Token，包含推理 Token。其余三个角色分别最多 1,024 个输出 Token。按官方 Luna Standard 价格和保守缓存写入费率，六次合计上界 USD 0.019968。 [Responses 服务层](https://developers.openai.com/api/reference/resources/responses/methods/create)

## 实际结果

一次完整的 DEV_DEBUG 角色调用，CLI 退出 0，耗时 138.818 秒。六次请求均为 HTTP 200、Luna、Standard 层，各发一次。基线复跑 14/14 一致，两臂各选中 195,469 字符。

| 角色 | 实际状态 | 输出 Token | 账本费用上界 USD |
| --- | --- | ---: | ---: |
| Diagnosis | 主动弃权，指出当前只有基线聚合，没有结果比较可支持归因 | 113 | 0.000271925 |
| P1_DEDUP | 输出上限截断，4,096 个 Token 均用于推理 | 4,096 | 0.002266300 |
| P2_BUDGET | 输出上限截断，4,096 个 Token 均用于推理 | 4,096 | 0.002266550 |
| P3_DEPENDENCY | 输出上限截断，4,096 个 Token 均用于推理 | 4,096 | 0.002267675 |
| Curator | 主动弃权，缺少完成回放的候选 | 55 | 0.000207175 |
| Meta | 契约通过，完成只读审阅 | 332 | 0.000305300 |

三个槽位没有返回最终消息，因此新 Patch 为 0，候选配对为 0。各槽位的 AST 核验、事实修复、回答回归和字符改善没有候选分母。逐题基线、用量、回执哈希和失败原因见[真实轮次证据](evidence/devloop-live-results.json)。之前两份归档候选的逐题结果见[归档结果](evidence/devloop-results.json)。

原账本追加 6 条开始和 6 条完成事件，累计上界由 USD 1.370204365 变为 USD 1.377789290，本轮增量 USD 0.007584925。没有重试、挂起或新增 UNKNOWN，旧的 5 条有上界 UNKNOWN 继续保留。六个受保护文件复核一致。

实际瓶颈是 Luna max 在 4,096 个输出 Token 内无法完成结构化补丁。下一次如要保留 max，针对输出额度做单变量试验，使用更高的输出上限前重新计算六角色总费用和取得新的调用授权。当前一次授权已经用完。

## 复现入口

```powershell
$env:RSI_OPENAI_HTTPS_PROXY = $env:HTTPS_PROXY
& 'D:/RAG knowledge/.venv/Scripts/python.exe' -B scripts/run_rsi_devloop.py --live `
  --cases var/devloop/cases.native-dev14.json --archive var/devloop/archive.json `
  --key-file 'D:/RAG knowledge/config/runtime-secrets.env' `
  --approval-id rsi-dev-v0-20261118-luna-01 --out var/devloop/live-approved-20261118
```

上面是已执行命令。调用 ID 在原账本中唯一，复用同一个批准 ID 会得到 `DEV_DEBUG_APPROVAL_ALREADY_USED`。原始回执和日志留在隔离工作树的 `var/devloop/live-approved-20261118`。
