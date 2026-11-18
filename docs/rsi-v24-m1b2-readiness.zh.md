# RSI v2.4 M1-B2 准备度审计

2026-11-18 结论：**本地配对工程控制可供审查；真实搜索 NOT_READY，S3 仍为 BLOCKED_DATA。** 本文不批准模型调用、真实回放、恢复、终测或发布。脱敏只读元数据回执见 [readiness-audit](evidence/rsi-v24-m1b2-readiness-audit.json)。合成入口、观察完整度与冻结测试只能证明工程行为，不能证明实际模型、来源、父代或独立验证资格。

## 数据与起始父代

现有 [S3 继续执行回执](rsi-v23-stage-evidence/s3-continuation.json)记录 20 个冻结 DEV 来源/20题，来源家族及暴露历史仍不完整；该回执的六类 `NONSEED_NECESSITY`、`MULTI_SOURCE_SUPPORT`、`CROSS_MEMBER_RELATION`、`NEGATION_CONDITION`、`UNIT_SCOPE_OWNERSHIP`、`CITATION_SUPPORT` 均为 BLOCKED_DATA。每类已准入正例、配对负控制为 0；旧事实区间及成员区间通过核对不等于新增六类覆盖通过。其 26 个实际回放控制及40次送达是历史工程证据，回执自身仍写明 `baseline_qualified=false`、`verified=false`。本次未执行新的来源审定或真实覆盖评测。

历史元数据包含101个来源身份、18个已消耗终测身份，来源身份数、manifest出现次数、题目数均不能代替独立家族分母。不可读取的历史记录及未决来源关系继续记 UNKNOWN，不重新包装为新来源或新终测。

本次只读核查原 `var/rsi-v22-20261115/registry/policy/parents.sqlite`：4条旧 `PARENT_ADMITTED`，其中3条 EXPLORATION、1条 VERIFIED，另有2条隔离事件。旧准入均未声明当前要求的 `evidence_mode` 与 `admission_version=rsi-parent-admission/v2.3`。旧 VERIFIED 标签不能建立当前起始父代资格。`query-aware-v1/registry/policy.sqlite` 仅有1条隔离事件。本次没有重新计算真实资格；**M1-B2 实际合格起始父代 UNKNOWN，真实资格验证 NOT_RUN**。不得从当前 tip、旧 label 或合成 parent fixture 推断真实起点。

## 费用账本与预算

本次通过 SQLite URI `mode=ro`、`PRAGMA query_only=ON` 读取原累计账本，未调用会初始化数据库的 `Budget` 构造器，未写入原库。原件路径：

`E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-v22-20261115/task-model-budget.sqlite`

| 项目 | 本次读取结果 |
| --- | --- |
| 原件读取前后 SHA256 | `b1cbb1b5d178d6c716a508ed42bdb575370e9790f45c5b4bdfcd728b4ad3be52`，两次相同 |
| 已存在事件 | 668 |
| 事件前缀尾哈希 | `3d728882322973a22567ece837141396041c69167d209bd68f3f12160b1630ca` |
| started / completed | 264 / 264 |
| 有界 UNKNOWN usage / 无界 / pending | 5 / 0 / 0 |
| 原累计上限 | USD 5 |
| 已记录费用上界 | USD 1.370204365 |
| 按该上界计算的剩余额度 | USD 3.629795635 |
| 预留 / 次数上限 | USD 0 / 无 |
| 供应商实际 invoice | UNKNOWN |

已校验668/668条现存事件的序号、前序哈希、事件哈希、请求哈希和幂等键；没有可信历史尾部锚点时，不能据此证明未发生尾部删除或供应商来源真实性。264个请求对是跨角色任务总账，不能全部归为 RSI 搜索。5个 UNKNOWN usage 已有成本界，不能改成零费用或已知精确 usage。历史 S3 的 USD0.167283495 是较早快照，USD4.832716505 不再作为当前可用余额。预算不重置，不以花完为目标；保持原规则：累计账面费用低于 USD5 时准许请求，最后单次可能跨线，UNKNOWN/pending 仍须失败闭合。

四组各3次、USD0.20/搜索的旧名义计划合计USD2.40，当前余额扣除后名义余量为USD1.229795635；该算式不包含真实请求、异常、QA和最终核查成本界。未冻结请求上限与所有组的可完成预算，故不能据此判定预算准入。

## 冻结、回放、恢复与公平性边界

本地配对 fixture 将两组运行 manifest 与共同输入冻结为同一测试工件，用于核对角色差异、共同 Evaluator、父代、模型映射、观察库和经验输入。其确切路径、SHA及命令结果以最终验收记录为准。**本地测试工件冻结与真实 launch 冻结分别审查；本次真实 launch 未冻结。** 不用手工设置 `ready_for_real_run=true` 代替实际证据复算。

| 检查项 | 当前证据与缺口 |
| --- | --- |
| 实际 S3 覆盖和来源暴露 | BLOCKED_DATA；没有新增真实合格资料 |
| 实际起始父代 | UNKNOWN；真实资格复算 NOT_RUN |
| 模型实际身份/可用性 | 本次未联网核查，UNKNOWN；历史 metadata 成功不证明当前付费请求能力 |
| 真实 Parent Selection、完整回放 | 本次 NOT_RUN；旧回放/合成测试不转换为当前真实证据 |
| 真实请求回执与断点恢复 | 本次 NOT_RUN；合成故障注入只证明相应控制路径 |
| DEV、来源审定、FINAL 隔离 | DEV只用于开发；来源审定是来源证据；FINAL只允许授权的独立封闭核查，本次未读 Gold正文 |
| 真实启动授权 | 未在本审计中取得；当前范围仅本地工程准备 |

两组工程配对及后续四组比较均需固定同一代码、解释器、Evaluator/Fixture/Gold指纹、合格父代、模型与费用口径、分析时点和停止条件；每组使用独立学习区及搜索血统。运行顺序、独立重复次数和预算分配须预先固定，不删除失败组、不合并不兼容分母。完整三槽 Holm 与全部静态合格实例回放仍适用；相同 Patch 不缩小候选家族。Memory 只报告关联；没有真实合格验证经验的 Experience-Guided 组保持阻塞，不能用合成经验补足。

训练 ADOPT、独立 VERIFIED、合成控制 PASS、真实执行完成四者分别记录。基础设施失败、被跳过、证据缺失、费用 UNKNOWN 保留各自分母；命令退出0只证明命令执行，不证明回答或改进成立。未发生终测即不报告终测提升，未发生真实恢复即不报告实际恢复成功率。

## 资源与验证记录

本审计复用现有解释器、历史元数据回执和原累计账本，仅新增本文及小型脱敏JSON；未复制语料、Gold、数据库或索引，未创建容器、虚拟环境，未删除资源，未接触正式服务。保留原始资料、失败和 UNKNOWN 是恢复及审查所需。本审计命令退出码为0；只读账本汇总已核对原始事件、分母及前后文件哈希。没有运行模型测试，故不产生模型性能或费用效果结论。磁盘清点记录在脱敏回执，磁盘变化含其他并发工作，不能全部归因本审计。
