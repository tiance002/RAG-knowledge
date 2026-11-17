# RSI v2.4 M0 验收与交付

M0交付RSI自身分析代码、测试和历史数据结果。基础PR1为87fd8314b55e24b6c78ee73572e78f77cd90f033，分支rsi/evolution-v24-20261117。PR2/GLM40业务实现未合并或复制，M1仅为设计。

## 已交付

- 数据源审计：rsi-v24-data-source-audit.zh.md（另有m0命名导航别名）。
- 统一31项指标规范：rsi-v24-metrics-spec.zh.md；代码rsik/analytics/schemas.py是唯一指标定义。
- Portfolio：rsi-v24-proposal-portfolio.zh.md。
- 自身能力与M1设计：rsi-v24-capability-report.zh.md。
- 只读reader/projections/metrics/portfolio/report与三份合成测试模块；不建立第二套事件事实库。
- 历史汇总：evidence/rsi-v24-m0-history-summary.json；输入版本：evidence/rsi-v24-m0-history-inputs.json；审计清单单列29个来源。

## 真实执行命令

在本地既有历史来源仍存在时，以新目录输出：

```powershell
& 'D:/RAG knowledge/.venv/Scripts/python.exe' -m rsik.analytics.report `
  --source-manifest docs/evidence/rsi-v24-m0-history-inputs.json `
  --artifact-manifest docs/evidence/rsi-v24-m0-history-inputs.json `
  --output var/m0-20261117/new-readonly-report
```

也支持重复--event-log、--budget-ledger，以及--registry（只取该目录直接存在的已知事件库，不递归扫描Gold/holdout）。筛选支持experiment-id、epoch、role-profile、agent-role、evidence-mode、parent-id、candidate-id、compatibility-fingerprint。所有源先完整校验链，再筛选；输出已存在则拒绝覆盖。

## 验证范围

专项最终61 passed，0 failed/0 skipped，exit0；原始日志special-tests-final.log逐项保留。完整回归collected579/passed579/failed0/skipped0、exit0（542.83秒），逐项日志与源码版本绑定在本次交付回执；远端保留原Ubuntu/Windows×Python3.11/3.13矩阵，所有新测试由原rsik/tests选择自动收集，不修改统计门。CI逐Job日志、collected/passed/failed/skipped及pytest step状态保存var/m0-20261117/ci/及ci-audit.json，并在Draft PR列出对应Run链接。CI最终状态以该提交的PR Checks和交付回执为准。

相同版本对8个原事件库及1份非封闭round的两次真实只读分析，report.json、report.zh.md、metrics-schema.json逐字节相同，9个输入文件哈希均不变。旧query engineering归档缺少round_id/execution_state，不冒充可关联的完整round。原账本SHA256基线b1cbb1b5d178d6c716a508ed42bdb575370e9790f45c5b4bdfcd728b4ad3be52。六个保护文件核验由rsik.foundation.verify_boundary完成。

## 负控制映射

| 要求 | 对应合成测试/检查 |
|---|---|
| 重复事件不重复计数 | duplicate_event、duplicate_input、跨来源同key控制 |
| 损坏事件停止 | read_only_missing_hash_and_chain |
| 外部hash不匹配停止 | manifest_hash_mismatch_before_output |
| Synthetic与Real隔离 | modes_and_versions_partition |
| Engineering不计真实收益 | simulation_mode_is_engineering 与分区检查 |
| 未执行Holdout为N/A | holdout_absent_na_and_adoption_not_verification |
| 零独立验证收益C4为N/A | unknown_cost_never_zero_or_invoice；缺improvement另为UNKNOWN |
| 基础设施失败不计拒绝 | complete_three_slot_family_and_infrastructure_separation |
| 同候选不重复计独立改进 | mirrored_holdout_counts_candidate_once |
| 家族完整三槽 | missing-round/duplicate-slot/disabled/aborted family controls |
| UNKNOWN费用非零实际费用 | bound_keeps_unknown_usage_and_failed_cost |
| 不完整Round不进完成回放 | incomplete_round_never_completed_replay |
| 回滚后无旧当前晋级 | rollback_removes_current_promotion / independent histories |
| 原SQLite内容不改变 | cli_repeat_is_byte_identical_and_read_only / 真实输入前后SHA |
| 相同输入报告可复现 | deterministic_json_and_no_source_text / history-r2/r3字节比对 |

上述控制不使用真实Gold。另有封闭路径别名在读取正文前拒绝、活动WAL/journal拒绝、无源不建Schema、禁止写构造器、未知资格不当空分母、profile完整指纹分区等控制。

## 保留的失败与限制

默认Anaconda没有pytest的首次命令失败，后复用已有venv。专项初次组合测试中封闭缺文件的异常类别不一致，修正词法路径和解析后真实目标的双重检查；保留初版日志。全量R1因显式basetemp父目录不存在出现223 passed / 356 setup errors；创建本任务临时父目录后用绝对路径重跑，不改变测试或准入。

清点中一次排除目录规则不足，解析过目标根下合成pytest夹具事件以核验链，未查看/输出其题目、答案和证据字段，未打开闭合QA300/QA500目录；最终清单只采用29个明确非测试来源。审计文曾误落旧工作树的三个新增M0文件，已移至本分支；旧文件未被覆盖。

缺失信息保留UNKNOWN/N/A。最终匹配Memory不冒充原始检索；父代准入不冒充选择；传输收到响应不冒充契约成功；训练采纳不冒充独立核查；模拟operator不冒充真人发布批准。无独立锚点不能证明日志尾部完整。已知usage小计与完整总usage分开；供应商账单仍UNKNOWN。

## 资源与执行边界

复用现有venv、SQLite和公开归档，只建立独立代码worktree、小型报告/测试产物；没有复制语料、数据库或索引，没有创建容器。未调用模型API、未运行真实搜索、未操作ECS/生产、未修改原EventLog/Budget/Evaluator/Gold/保护文件。原用户业务/前端和PR2未提交内容保留。临时合成测试资源集中var/m0-20261117/for-user-delete供用户删除，不清理旧证据。磁盘前后字节及资源大小见本地final-receipt.json。

M0完成后停止等待人工审查，不进入M1，不合并、不部署、不执行Promotion，也不开放任意源码自改。

工作区核对限制：D产品的chat.py和grounded_generation.py在本任务期间出现与初始hash不同的内容，本任务没有写入这两个业务源文件，也未回退/覆盖其当前内容。该变化来源未独立判定，逐项hash保存在external-workspace-drift.json；不声称D产品整体字节不变。M0输入和计算只依赖新E工作树、明确历史事件/归档和已有解释器，不依赖这些业务源变化。原RSI/GLM工作区已记录脏文件、六个保护文件、原账本和9个分析输入均保持对应基线。
