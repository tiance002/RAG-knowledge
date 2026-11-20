# RSI v2.2 实施与验收记录 — 2026-11-15

本次交付的是受控 RSI 实验机制及真实试运行证据。**没有发现可采纳的字符压缩收益，没有 VERIFIED 候选，没有生产发布。** 五轮上限已生效，不会为获得成功结果而继续重试同一血统。

## 约束与实际修改

用户已取消逐阶段人工等待，授权自行测试推进；OpenAI、成本优先、禁用 Astra、复用原有 key。费用规则采用最后指示：本任务累计 USD5，无金额预留，调用数不限，最后一次可超额；出现无法确定的用量先停止。

产品工作区 `D:/RAG knowledge` 保留既有改动。实际 RSI 控制仓库为 `E:/codex_workspace/2026-11-03/task/rsi-work`，新增 `rsik/` 和 `scripts/build_rsi_v22_foundation.py`。冻结业务根是 `D:/RAG-R0` 的 `90d86ce87e8c76c8c45f5f296160ddf53de71d54`，不是 `E:/RAG quention`。没有将不同项目或 HEAD 混用。

六个入场保护指纹仍一致：`rsi_candidates.py`、`run_rsi_offline.py`、`rsi_feedback.py`、`rsi_delivery_worker.py`、`replay_source_preparation.py`、`rsi_decision.py`。这些文件在开始前可能已有相对 Git HEAD 的修改，因此“指纹一致”不等于 `git diff` 为空。本任务未改写其内容、FIXED 或 EVALUATOR。

新增模块与接口说明：`E:/codex_workspace/2026-11-03/task/rsi-work/rsik/README.md`。只对本任务新增代码生成了 `var/rsi-v22-20261115/implementation.diff`，36 个 Python 文件，275963 字节；未替其他已有修改提交 Git。

## 阶段结果

|阶段|已验证内容|实际边界|
|---|---|---|
|P-A|路径、源码、原 API、保护边界、环境和费用规则审计|见 `rsi-v22-pa-audit-20261115.md`，不把历史部署信息当当前事实|
|P-1|真实训练20题/20来源簇、隔离 sealed10题/10簇、新 R0、原接口继承和两次真实20题基线/等价回放|来源自检 + 既有 AI 语义审阅，非 HUMAN Gold；布局与 seed 为新构造|
|P0|纯确定性裁决、来源硬门、固定检验家族、逐层投影、锁、原子提交、恢复、事件与费用账本|含真实竞争进程/终止、UNKNOWN 不计免费、空/部分/伪造结果负控制|
|P1/P2|单 Proposer 与三个独立 Proposer 真实调用；一份正式补丁原 AST/编译/探针后完成20题真实配对|只有1个正式提案，NO_GAIN，无候选采纳|
|P3|不可变手册、有限条件冲突、DRAFT/ACTIVE 资格、计数重建和摘要注入；Curator 真实调用|Curator 无足够候选证据时 ABSTAIN，没有编造 ACTIVE 经验|
|P4|五轮限额、可信无收益计数、一次核查、失败冻结、Verified/Exploration 父代过滤与选择|当前血统5轮已用完；不存在真实 VERIFIED 父代样本|
|P5|Meta 真实只读报告，覆盖/时效/false-pass 缺失证据标 UNKNOWN|报告不改变裁决或题集，没有从 UNKNOWN 猜具体原因|
|P6|外部核查适配器复用原 checked_case/execution_view/assess/compare_pair；双比较 Holm；隔离签名/CAS/回滚/中断核对|适配器以记录捕获测试验证；真实 sealed 未执行，生产发布默认拒绝；批准为模拟身份|

P7 接缝扩展未开放。原 evaluator 保序规则仍生效，P3 不能为了试验成功而绕过。

## 真实试运行与费用

|轮次|模型 API 次数|费用估算 USD|正式提案/合格/真实回放|结果|
|---|---:|---:|---|---|
|single-r1|1|0.000132400|0/0/0|Diagnosis ABSTAIN|
|single-r2|3|0.000945525|0/0/0|P1 ABSTAIN；含一次已知用量格式修复|
|parallel-r1|4|0.000888575|0/0/0|三个独立 Proposer 均 ABSTAIN|
|parallel-r2|4|0.000926325|0/0/0|三个独立 Proposer 均 ABSTAIN|
|parallel-r3|4|0.009623025|1/1/1|Sol 去重提案真实回放 NO_GAIN|
|Curator + Meta|2|0.000295900|不生成代码候选|ABSTAIN / 只读报告|
|合计|18|**0.012811750**|**1/1/1**|0采纳、0 VERIFIED|

17 次使用 `gpt-6-luna`，1 次去重编码回退使用 `gpt-6.1-sol`，没有 Astra。默认均为 Luna；Sol 仅在低价模型连续无法提出补丁后使用。公开价格依据为 [Luna 官方模型说明](https://developers.openai.com/api/docs/models/gpt-6-luna) 与 [Sol 官方模型说明](https://developers.openai.com/api/docs/models/gpt-6.1-sol)。金额根据每次响应的 input/output/cache usage 和冻结单价计算；服务商实际账单字段仍为 UNKNOWN，不把估算写成已对账实扣。账本无 pending/unknown usage，预留为0。

唯一正式补丁去掉了 `exact_duplicate` 选择配置对既有精确重复规则的限定；未新增近似重复判定或外部调用。通过原 apply_proposal/validate_seam、编译和20个固定不变式探针，再经原 derive/materialize/replay 接口入库与执行。20/20 完整配对，所有必要来源事实 PRESENT，总字符差0，确定性 `REJECTED/NO_GAIN`。训练集为 OTHER/Markdown 来源，本次未观察到可被该规则移除的重复，是结果局限而不是推断收益。

已完成轮次再次通过 `--resume-completed` 核对，返回相同结果，模型调用数保持18，未重复注册、物化或执行 worker。每个静态合格实例分别回放、相同补丁不得去重跳过，由独立并发 FakeRoles 负控制覆盖；本次真实模型没有产生三个相同的合格补丁，不能声称真实验证过那种组合。

累计五轮的绝对限额已触发；可信 NO_GAIN 回放只有一轮。此前将三轮 ABSTAIN 误称为三轮无收益停止，已纠正实现与说明。不可重置血统或任务费用库来绕过限制。

K=1 和 K=3 使用了自适应提示与一次不同模型，未形成对等随机比较；不能得出多 Agent 优于单 Agent，也不能把没有合格提案的轮次算作回放胜负。

## 测试与原始证据

最终命令使用复用的 `D:/RAG knowledge/.venv/Scripts/python.exe`：

```text
python -B -m pytest rsik/tests backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_business_source.py -vv --tb=short
```

`final-tests-r2.log`：退出码0，collected159，逐项159 PASSED，0 FAILED/ERROR/SKIPPED/XFAIL/XPASS；原始169行日志无截断，分母核对一致。159包括模拟单元/负控制和原接口回归，不等于159次真实模型实验。早期失败日志未覆盖、未当通过计算。

实际隔离验证：最终正常探针20项通过；Python3.12.3、UID65534与7个依赖版本已记录。合成超时 worker 启动后代进程后被1.5s截止限制终止，实际耗时约1.649s，容器与后代清除；不是 Future.cancel 代替终止。真实候选61个阶段（1个单元阶段+60个配对阶段）运行环境一致，全部确认容器移除，job目录清理成功。远端最终任务标签容器0、任务目录0；未停止正式服务。

控制仓库证据根 `var/rsi-v22-20261115/`：

- `live-parallel-r3/round.json`、`P1_DEDUP/run/results.json`、原始逐题 capture/feedback 和来源核验。
- `cost-and-boundary-final.json`、`round-comparison-final.json`、`task-model-budget.sqlite`。
- `completed-recovery-r1.log`、`capsule-final-controls/verification.json`、`remote-final-inventory.json`。
- `final-tests-r2.log`、`final-test-log-audit-r2.json`、`implementation.diff`。
- `resources-final.json`、`pytest-archive-inventory.json`、`pytest-artifacts-final.zip`。

## 资源复用、收尾与保留

复用本地 venv、冻结业务 checkout、既有 QA/来源、远端镜像及只读系统 Python/venv；没有复制生产数据库/Chroma、拉新镜像或重建依赖。首次精选30个原始来源共183694字节，后续版本大量硬链接复用，封存题替换一个预执行标题冲突来源仅新增1173字节。旧冻结版本、失败尝试和逐题结果作为审计/恢复依据保留。

本任务证据目录逻辑大小约21.06 MB，硬链接在逻辑计数中重复；新模块及测试约246 KB（随后文档单列）。远端临时容器与任务job目录已清理，未删除镜像、依赖环境和其他任务资源。

10个本地 pytest 临时目录共2289个文件、8121114字节，已归档为2335826字节 ZIP，CRC与逐文件 SHA 均核对通过。批量删除与固定单目录删除均被自动审批拒绝，返回 `blocked by policy`。未绕过限制，**这10个原目录仍保留**，本地删除项为0。

清理尝试前：D已用162026168320/可用132946644992字节；E已用447012802560/可用41080446976字节。尝试后：D已用162026168320/可用132946644992；E已用447012552704/可用41080696832。由于未实际删除目录，E的249856字节盘级变化不归功于本任务。磁盘存在其他并发活动，不能将整盘变化当本任务空间收益。

## 未建立的结论与后续准入

没有训练采纳候选，因此 sealed10题仍未执行，真实独立泛化与真实生产晋级均未验证。本次只允许称为“已实现并试运行受控多 Agent RSI 实验机制”，不能称为实现持续可靠自我改进、回答质量提升或线上收益。

当前训练题来源标签是既有 AI 审阅加自检；20个来源簇与源字节可核对，不代表所有必要语义事实都完备。封存来源虽完成 hash/标题/8gram 分离筛查，历史语义独立性仍有边界。Windows 不宣称目录 fsync 或突然掉电的完整持久化保证。真实生产身份、部署与在线监控接口没有接入，当前以拒绝生产调用和隔离演练收束。

后续如要开新血统或扩展选择规则，需要依据当前无收益/ABSTAIN证据调整观察信息与目标，保留原冻结数据与本次费用账本；不能继续同一五轮血统或放宽事实、AST与统计门来制造通过。
