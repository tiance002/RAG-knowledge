# RSI v2.4 S3.3 原始 Worker 基线资格准备度

**BLOCKED_DATA。真实 A/A 为 NOT_RUN，当前没有可签发的 BASELINE_QUALIFIED。** 已核对历史候选记录、源码、配置、四个 DEV manifest、Evaluator 源码与 Gold 编译回执的字节身份。原件未写入。完整 SHA256 见[身份回执](evidence/s3-v24-s33-baseline-readiness.json)。

| 对象 | 已核验内容 | 资格边界 |
| --- | --- | --- |
| 受保护原始 Worker | `scripts/rsi_delivery_worker.py` SHA256 `a5f793b9f8f6ad7908c78580a670c0cc5c909898dc8e4c36d05fdbc2a330d3da` | 原始生产无关选择函数是旧接缝。fixed-history 资格必须为实际绑定的六参数工件签发 |
| 历史 query 根 | `a8191e0…` 的 Registry 记录、Worker 与 Config 内容寻址匹配 | 导入/存在只证明血统和字节 |
| 历史 fixed-history wrapper | `e0339eb…` 基于同一根，Worker 和 Config 均匹配。两者配置 `selection=all`，配置哈希相同 | wrapper 不是本次新候选。当年覆盖准备在 `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED` 退出，未完成实际资格 |
| Evaluator 与协议 | 记录 fixed-history profile、coverage、adjudicate、launch、资格入口和受保护 decision 的源码哈希 | 当前代码可读取、旧保护值一致，尚无新评测绑定 |
| 4 套历史夹具 | 4/4 manifest 明示 DEV、同一 profile。每套 20，含 17 检索题与 3 业务控制 | manifest 的 `AI_REVIEWED` 不构成人工 Gold 签发。四套变体复用原题，分母仍是每套 20、原题 20 |
| DEV Gold / 来源快照 | DEV-only 编译回执保留 72 accepted claims、68 引用、66 直接匹配。来源审计与逐 atom 矩阵见同行交付件 | 缺 component、必需原生成员、负控制与独立来源家族的项目保留 UNKNOWN 或阻塞 |

后续实际资格依赖以下证据全部到齐：

1. 确定唯一 owner 审核的根工件和来源血统。冻结 Registry 身份、实际 Worker、Config、隔离环境及回执策略。旧 VERIFIED、合成 PASS 或历史 wrapper 标签不能代替资格。
2. 先完成可审核的 DEV-only Gold 和数据集登记，按六类覆盖门冻结完整来源家族、曝光、四套 20 槽夹具与原生非 seed 控制。当前阻塞使正式 launch 计划无从签发。
3. 数据和授权齐备后，才按现有 `qualify_baseline` 入口运行隔离 A/A。先通过原不变量探针，四套各按同一候选、同一配置、同一上游做两臂。G1–G4、逐题事实、字节哈希与长度一致性均须通过。真实回执须保留失败阶段、命令退出码、环境与输入绑定。
4. 用 `verify_baseline_qualification` 从冻结文件、真实执行回执及各套评测结果重算，核对候选和计划是否一致。仅按现有协议签发根资格，仍需后续独立验证判断改进。

原累计 USD5 只读账本 SHA256 仍为 `b1cbb1b5d178d6c716a508ed42bdb575370e9790f45c5b4bdfcd728b4ad3be52`。SQLite `mode=ro&immutable=1` 下有 668 条现存事件。原件读取前后字节相同，当前无新费用、无新增候选或资格。启动前应再核对费用上界和 UNKNOWN。
