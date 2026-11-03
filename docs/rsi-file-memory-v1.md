# RSI 文件记忆和 ACE Playbook 接口提案 v1

本规范为本地离线文件预留，非已部署引擎或已批准运行流程。个人长期偏好只在 Redis Hash 中管理，与 RSI 实验及经验文件隔离；不能将聊天内容自动写入此处。现有 [RSI Core 接口提案](rsi-core-interface-proposal.md) 的候选、运行与证据身份仍为独立提案；`RSI-kit/03-共用机制与队列.md` 已描述共享 manifest、registry 与 run_id。本规范不创建第二个 registry，也不迁移既有资料。

建议布局（目录及文件由未来获授权的流程建立，本轮不创建运行数据）：

```text
RSI/
  registry.jsonl                 # 唯一实验登记，追加事件；现有登记不得重置
  experiments/EXP-001/
    experiment.yaml             # 实验 ID、假设、数据/配置版本、授权引用
    hypothesis.md
    runs/RUN-001/               # 独立运行身份
      config.json               # 数据、模型、配置身份；不放密钥
      metrics.json              # 指标、分母、未知状态
      cases.jsonl               # 每题一行及证据引用
      report.md
      manifest.json             # 制品及 hash
    findings.md
  candidates/CAND-001/          # metadata.json: ID、父代、状态、证据及版本
    metadata.json
    patch.diff                  # 如适用，父代相对补丁
    evaluation.json             # 逐项结果引用和分母
  playbooks/                    # index.md 与 retrieval/reranking/parsing/citation/evaluation.md 为索引或说明
    entries.jsonl               # v1 经验追加修订；全文件 ID 命名空间
  evolution/                    # 未来演化路径、父代关系和检查点
  archive/                      # 原始实验与历史归档，不覆盖
```

以上实验文件字段为最小建议，并非本轮已实现的 schema；真实证据应引用可回读的已授权文件。原始实验先归档，再提炼经验候选；失败经验保留失败条件。`entries.jsonl` UTF-8，每行一个完整 JSON 对象并以换行结束。公开的[合成候选示例](examples/rsi-playbook-v1.jsonl)引用实际存在的[合成证据](examples/rsi-synthetic-evidence-v1.md)，不代表存在真实实验或已验证结论。

每条修订必须含 `schema_version=ace-playbook/v1`、唯一经验 `id`、从 1 连续递增的 `revision`、`previous_sha256`、`problem`、`conditions`、`observation`、`strategy`、非空 `evidence` 引用数组、`counterexamples`、`confidence`（low/medium/high）、`status`、非负整数 `usage_count` 和 `success_count`。条件与反例是字符串数组；适用范围和失败条件应具体。首次修订的 predecessor 为 null；其后为该 ID 上一条完整 JSON 对象的 SHA-256，计算使用 UTF-8、键排序、非 ASCII 原文、无多余空格的 JSON。ID 不可分配给不同经验，同一 ID 仅以连续修订追加。使用后追加新修订记录累计使用和成功次数，成功数不得大于使用数，计数不得减少；未知结果不计成功。不要把推测写成验证结论。

状态从 `candidate` 开始，可保持 candidate、转为 validated 或 deprecated；validated 可保持或转为 deprecated；deprecated 只能保持 deprecated。validated 必须有 `independent_validation` 对象，含非空 `reviewer`、独立的 `evidence_ref` 和 `result=PASS`；验证资料应与提炼来源独立，由负责人或独立审阅者核实实际数据、方法及结果。不同引用字符串本身不能证明独立性；失败或证据不足不能晋级。新证据或状态、计数变化都追加版本，禁止无痕替换。

只读接口 `scripts/rsi_playbook_verify.py` 提供 `read_jsonl(path) -> ([(line, object)], diagnostics)` 和 `verify(path, trusted_sha256=None) -> diagnostics`。读取逐行保留完整有效 JSON 对象，损坏行和末尾未完成行报告行号，后续完整行仍可读取；不改写、不截断原文件。无效修订不作为后续修订的可信前驱。调用方必须在任何诊断存在时将结果视为不完整，保留原文件并从可信备份人工恢复，不能将读取到的部分当作完整档案。校验仅检查格式、修订链、引用非空及回执形状，不打开引用、验证事实、独立性或证明引用权限。删除整个末尾修订或重写整条链无法由链本身发现；可在独立可信位置保存定期的文件 SHA-256 与备份，然后调用 `python scripts/rsi_playbook_verify.py docs/examples/rsi-playbook-v1.jsonl --trusted-sha256 <可信摘要>` 进行核对。摘要必须由可信渠道独立提供；对文件正常追加后需更新检查点。未提供摘要时只检查文件内部一致性。并发写入、锁、原子持久化和自动恢复属于未来设计，本轮没有写入器。

借鉴边界：EvoAgentX 只借鉴记忆模块解耦；HyperAgents 借鉴 JSONL archive 和候选元数据；DGM 借鉴 patch、父代与恢复；GEPA 借鉴逐题反馈、状态恢复与 Pareto 候选管理；ACE 是 Playbook 核心参考；AFlow 借鉴树状演化和经验回填。均不引入完整框架或实现演化算法。模型调用、生产集成、自动晋级及自动修改均未实现。
