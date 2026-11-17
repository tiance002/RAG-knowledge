# RSI v2.2 实验系统

本目录实现既定方案的受限 RAG 策略优化：结构化诊断、独立提案、安全补丁、真实配对回放、确定性裁决、经验事件、独立核查和隔离晋级演练。它不会自行修改 RSI 的裁决器、调度器或预算等框架代码。

当前交付与实测边界、逐阶段验收及失败修复见 [审查报告](../docs/rsi-v22-review-20261115.md)。公开仓库不包含私有题目、Gold、模型完整响应、密钥和运行数据库；这些证据在原执行环境保留。公开的收益摘要属于开发验证，不能冒充自主测试收益或生产收益。

## 可复现的本地检查

使用已有 Python 3.11+ 环境和 pytest 8.3.5（本地验证 Python 3.13，真实隔离业务执行 Python 3.12）。从仓库根运行：

```shell
python -B -m pytest rsik/tests tests/test_query_workflow.py backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_business_source.py -vv --tb=short
```

这些检查只需 pytest 和标准库，合成来源与捕获在测试临时目录生成；不需要 `var/`、SSH、Docker、业务数据库、模型密钥或付费请求。它们验证契约与读写/裁决行为，不能代替真实业务或模型验证。具体合成材料说明见 [tests/README.md](tests/README.md)。GitHub Actions 对 Linux/Windows、Python 3.11/3.13 执行同一套检查。

`.gitattributes` 对六个受保护文件禁用换行转换，以保持 `foundation.PROTECTED` 的原始字节哈希。此边界相对任务入场版本冻结；入场前已有的受控业务根适配补丁也会随源码发布，不能把它们误说成本次改写保护文件。

## 入口与运行前提

```shell
python -m rsik --help
python scripts/run_rsi_query_round.py --help
python scripts/run_rsi_autonomous.py --help
```

- `python -m rsik`：原五参数接缝，支持 K1/K3、注册/物化/回放阶段记录与已完成轮次重验。
- `run_rsi_query_round.py`：用户授权的独立六参数 query-aware 接缝，支持 K1/K3、每个合格实例独立回放、Curator/Meta 自动执行和兼容经验检索。`--resume-completed` 只重算已完成证据，不再次调用模型、容器或写入候选。
- `run_rsi_autonomous.py`：先固定系统代码与输入哈希，再让 RSI 自行诊断、提出下一条策略并调度轮次；外层监测不提供优化公式。默认单提案以降低成本，`--profile parallel` 启用三个独立提案角色。每轮主结果归档后执行策展与审计角色。

真实运行必须显式提供合格父代、冻结的真实 Fixture/来源标注、已有密钥文件、隔离执行主机和依赖、共享费用账本。业务源码固定到 `90d86ce87e8c76c8c45f5f296160ddf53de71d54`，不能让控制仓库当前工作区代替已核实的业务根。`capsule.py` 是当前已验证运行环境的适配器，不是任意云环境的一键部署程序；环境不符合时应拒绝执行，而不是自动安装、复制整库或更改正式服务。

现有外部模型通道使用 OpenAI Responses API，默认 Luna，必要的编码回退使用 Sol，禁止 Astra。旧 `scripts/rsi_*` 兼容模块包含历史实验入口，不能把它们误认为本系统默认的外部模型通道。

## 核心机制

| 组件 | 职责 |
| --- | --- |
| `contracts.py`、`redact.py`、`openai_roles.py` | 独立角色、递归输入投影、Schema、本地引用校验、请求与用量留档 |
| `workflow.py`、`query_workflow.py` | 独立 P1/P2/P3、每个静态合格提案回放、重复补丁独立记账、P3 不改变集合/原顺序、固定 Holm 三槽最多一个采纳 |
| `adjudicate.py` | 不含 LLM/I/O 的事实、来源、局部回归、总收益及统计门；模型置信度不决定结果 |
| `query_aware.py`、`query_verification.py` | 独立接缝/注册记录、训练证据复算、按来源防止 holdout 重打包复用 |
| `state.py`、`locking.py`、`events.py` | intent/receipt/checkpoint、真实 OS 锁、原子写入、追加事件与校验 |
| `handbook/`、`postround.py` | 不可变经验条目、状态/计数投影、冲突隔离、只读审计；没有可信证据不激活规则 |
| `autonomous.py`、`autonomy_contract.py`、`test_phase.py` | 开发/正式测试边界、跨轮失败记忆、有界模型投影、具体静态错误反馈、代码变更时终止当前测试 |
| `budgets.py`、`http_worker.py` | 同一个累计 USD5 账本、无金额预留、无轮次/调用次数上限、最后一笔可跨线、未知用量不当免费、无隐式重试 |
| `promotion.py` | 签名绑定、版本 CAS、观测/回滚的隔离演练；真实生产入口默认拒绝 |

## 正确理解结果

`ADOPT_CANDIDATE` 是训练决选，`VERIFIED` 是指定独立核查通过，二者都不代表生产部署。实际生成回答质量、生产延迟和 token 节省没有由字符指标得到证明。

原短尾 VERIFIED 候选在新增同源备用问题中遗漏事实，已被隔离；这说明旧验证结论只能覆盖旧证据。新开发候选重新经过原题、备用问题、双向 seed 压力和独立来源核查。第一批正式自主测试仍发现重复失败补丁与静态反馈不足，已保留失败现场并结束该版本；之后修复系统并重新验收，不能覆盖旧失败。

真实来源与合成控制分开，旧 holdout 不再作为新候选的独立验证。新来源正常核查与双向压力共享来源簇，压力用例不增加统计分母。

## 费用、权限与恢复

沿用任务累计账本，不能换新文件重新获得 USD5。金额来自实际响应 usage 与记录的价格版本；供应商最终账单不可假定已核对。每次格式修复、失败请求及未知请求均留档；未知请求没有确认之前不盲目重发。

系统工程与自主测试分开记录：工程师可以开发系统；进入冻结测试后，优化补丁仅来自 RSI 模型，监测者不代写策略。人工停止与工程失效分别记录，后者不能冒充用户要求停止。部分运行或副作用不确定时保留现场并拒绝盲重启，完成轮次可只读重算。

每个隔离 capsule 自行回收本任务容器/远端目录。原始来源、冻结 Gold、逐题捕获、失败和 UNKNOWN 记录保留；不要清理其他任务资源。当前阶段不执行真实生产切换；下一阶段是否开放 RSI 自身系统代码优化，由审查后的新方案定义。


## v2.3 S0–S2

详见 [阶段报告](../docs/rsi-v23-s0-s2-review.md) 与 [S3/S4 准备](../docs/rsi-v23-evaluation-preparation.md)。默认真实读取强制逐题 prepare/A/B execution receipt；合成控制显式隔离。完整三槽 FamilyDecision 贯穿归档、恢复与核查；新 query-k3-dependency/v1 显式映射 P3_DEPENDENCY，旧 P3_ORDER 不变。核查 CLI 可通过 handbook/session/原账本参数接通可信经验闭环。

旧 v2.2 归档和基线不会自动获得 v2.3 资格，需完整证据迁移。prepare_rsi_v23.py 只生成准备清单，不启动真实实验。当前 S3 覆盖与新资格未就绪，不运行 S4。
