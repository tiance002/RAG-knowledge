# R1 本地候选继承验收

**R0 最终版本 PASS；R1 继承及反馈 PASS；E1c 正式候选已固定，正式数据 BLOCKED，正式效果 NOT_RUN。** 本轮只有合成运行、工程故障注入及对已有材料的只读审计。仍使用 `scripts/run_rsi_offline.py` 执行配对。

## 三份版本

|用途|身份与恢复依据|
|---|---|
|业务基线|工作树 `D:\RAG-R0` 起始及收尾 HEAD 为 `90d86ce87e8c76c8c45f5f296160ddf53de71d54`。业务代码 `backend/app` 相对基线无变化。|
|上轮 R0 回执|旧 `var/r0-public-01/versions.json` 中 worker 原始文件 SHA-256 为 `5f7b165c3bf00f8aecd50588d4553fcd099bac11eba64b6fa80e1672e71f1b2b`，可从同目录 implementation 恢复；此前选择器收尾只做过离线等价核查。|
|本轮重跑及冻结的 R0|`var/r1/r0-final/versions.json` 中 worker SHA-256 为 `a5f793b9f8f6ad7908c78580a670c0cc5c909898dc8e4c36d05fdbc2a330d3da`。包含 `select_member_indices`、普通交付和受限网络环境。R0 runner 原始文件 SHA-256 为 `c61b24a9238dadff3cb7ae4b88ad537bf8882480f4cf9883953f0fe40493905c`。|

开工时工作树有已修改的历史 E2.0 回放脚本和未跟踪的 R0 入口、worker、来源准备、测试、报告及清单。本轮在 R1 改动前把上述 **13 个公开文件**与基线合成一个本地 Git 提交 `297e66ed049d58060fffe2c15d36efaec182ecc8`，由 `codex/rsi-r0-frozen` 引用。逐文件用 `git show SHA:path` 校验其字节与实测源码一致；提交树没有 `var/`。恢复精确复验时，使用以业务 SHA 为 HEAD 的独立工作树，将该提交相对父提交的 13 个 blob 用 `git cat-file blob <提交>:<路径>` 按字节写入同名路径。这也保留 R0 旧 CLI 对业务 HEAD 的检查。R1 工作树继续保留本地改动，无远端提交或生产切换。

## 本次 R0 复验

```powershell
Set-Location 'D:\RAG-R0'
$env:RAG_TEST_EXTRA_SITE_PACKAGES='E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_offline.py --manifest experiments/r0/synthetic.json --out var/r1/r0-final
```

上面是实际 R0 命令，退出 **0**，输出目录已封存。逐个比较新旧 result 和 capture，并从实际系统消息依照每个成员的偏移截回正文，复算内容哈希。

|题|实际送达 A → B|独立种子；必要断言（每臂）|对照|
|---|---|---|---|
|delivery，report.pdf|header、row、copy → header、row；106→72 字符，166→118 UTF-8 字节|1 个；数值、表头、单位共 3 项均 PRESENT|两臂的来源/顺序/消息分别与旧版相同|
|collision，colors.pdf|color → color；29→29 字符，49→49 字节|1 个；颜色字段 1 项 PRESENT|均未将另一库的资料交给客户端|
|missing-owner|缺少文件 missing 的可信归属，准备阶段 BLOCKED|不适用|没有 A/B 目录，未进入配对分母|

**总题数 3，配对 2，预期阻断 1，非预期失败 0，本地末端捕获 4，外部模型请求 0。** 两题不是各有四项断言。R0 汇总复算和恢复字节检查退出 0。

## 共用流程和可复制命令

```powershell
& 'D:\RAG-R0\scripts\demo_rsi_inheritance.ps1'
```

该命令读取本机已封存的 `var/r1/r0-final` 及反馈，采用新运行目录，并在同一 registry 内按内容寻址复用父代。工程演示显式以同一份小夹具给 A/B，不将夹具成绩与 R0 三题作数值比较。实际验收调用为 `& .\scripts\demo_rsi_inheritance.ps1 -Output var/r1/inheritance-proof`，退出 **0**。

1. `scripts/rsi_candidates.py register-r0` 核对输入、评测文件、已完成运行和代码、配置身份，将原 candidate_id 和可恢复 artifact 联系起来。旧设计中没有记载的原始假设保留 UNKNOWN。
2. `derive` 从父候选、已有 run 的真实 observation ID、唯一改动、假设、预期及反证条件创建子代。核对归档、成员回执、run_id、qid 及父臂身份。工程示例用 `worker-patch.json` 表达一行可审查的差异；管理器保存实际源码字节以及配置。可替换为受审查的小型选择代码、配置或选择工作流规则。第一版的代码改动局限于 `select_member_indices`，可用操作受限。
3. `materialize` 给新目录写出两臂配置、独立 worker 源码、冻结输入与评测身份、父子记录与证据绑定。普通继承沿用冻结 cases、evaluation、fixed、budget、evaluator 和停止条件。演示的 fixture override 需要显式声明；开发/工程观察不能悄悄转成另一数据集的质量结论。
4. 原 CLI 通过 `python scripts/run_rsi_offline.py --manifest <物化目录>/manifest.json --out <全新归档>` 运行。先按可信 SQL 文件归属准备来源，然后限定候选可见的成员、文件及结构字段；候选不接收 qid、Gold 和必要性标注。A/B 各自从校验过的源码文件启动 worker，共用上游身份和评分版本。评估依据是本地客户端收到的实际 Prompt。归档包含本地 run_id、源文件 SHA、候选记录、反馈出处、逐题结果、两个捕获路径和原始文件 SHA。
5. `scripts/rsi_feedback.py` 从汇总、逐题记录和实际回执生成事实，缺失种子、已知事实缺失/相对 A 丢失、来源冲突、执行失败及不可配对分别记录。输入阻断的质量评价为 NOT_RUN。用 `python scripts/rsi_feedback.py --out var/r1/r0-final --run-id r0-final --length-changes` 查仅测得长度变化、已核验事实仍保留的题，本次输出 delivery（−34 字符/−48 字节）。有损失的负例或内容未知的题不能从该查询中得出“质量保持”。开发者假设单列在候选记录，绝不据此自动晋级。这里的 observation 是本地派生事实，历史无供应商 receipt_id 时以回执路径和哈希定位。

## 父子链和故障反例

- 来源 `r0-final / delivery / obs-67df0642fa587e9fbad4eff5`。父代 `verified-exact-duplicate-v1` 的登记 ID 为 `3a82adba110ba6bccc55687fdb2781e89fc83038cb3ebdf2b168502105a3567f`。它的实际 B 从精确重复处省去了 copy，保留表头和独立命中的数值行。
- **等价工程子代** `43309ef3fcea87e5930647c7232d24831b9618816e9aefe67599c0cda505749e` 仅把返回索引列表改为返回其副本。如果同一输入的送达成员或证据字节改变，就推翻等价假设。运行 `ff073a2b76c545d5a263121ab5526b62` 中两臂均送 header、row，1 个工程配对，退出 0。可由 `var/r1/inheritance-proof/run/implementation/B_candidate.json` 追到假设与父代，再经 `lineage` 和 `case-001/result.json` 读到相应原始回执。
- **故障注入子代** `0f178f4d77c2…` 仅返回选中列表的最后一个成员。在同一夹具中，A 仍送 header、row，B 只送 row。虽然 B 缩短 40 字符、72 字节，评分器从实际请求确认丢失表头及单位。运行 `f7a6792cd94b4dc79f868c73b44a82d0` **退出 1，符合故障注入预期**。保留失败归档 `var/r1/engineering-failure/run`；反馈中有 `required_fact_loss_vs_parent`，原父代及成功运行未覆盖。两次工程运行均零外部调用，均未计入 R0 三题分母。
- 两份物化目录均从 registry 在临时目录重新构造，并按文件逐字节比对。两份反馈亦可由现存回执重建。多代继承、选择器篡改、输入污染、失败现场保留、错误配对和原始资料隔离由工程测试覆盖。这些记录证明工程能力，不是业务候选晋级或回答质量评估。

## E1c 就绪情况

|交付件|状态|核对结果|
|---|---|---|
|正式 cases|BLOCKED|6 道历史开发题有 107 个种子、72 个唯一块、10 个文件。已有候选引文中的 file/KB 组合都源于检索输出；已查的获准材料没有独立、能绑定到该版索引的逐文件归属。需要从获准的历史库或入库清单导出实际 file_id、KB、源哈希和版本。|
|独立 evaluation|BLOCKED|281 次出现的成员文档身份、正文哈希及索引位置均能对齐。真正的 Office 原文位置只有 7 次出现获历史核对，17 次 PARTIAL、257 次 UNKNOWN；不能把占位页码当表格单元格。必要性仍有不确定，尚缺独立冻结的逐题必要事实、表头、单位及原文定位评分材料。|
|正式 B 源码与配置|已固定|沿用上述 `verified-exact-duplicate-v1`、`exact-duplicate.json` 和 R0 冻结提交。当前实现要求可验证的 PDF 页/区域及结构；六题来自 DOCX/XLSX，可证明满足此规则的成员为 0。不能将近似或跨位置去重放在原候选 ID 下。|
|独立留出与正式效果|NOT_RUN|六题已反复用于开发。即使补齐归属和评分，也只可能成为 READY_DEV_ONLY。独立留出另备。未运行历史六题或真实模型、未评成本收益或晋级。|

就绪审计报告在 `docs/rsi-r1-e1c-readiness.md`，机器记录在 `experiments/r1/e1c-readiness.json`；原 `experiments/r0/e1c-planned.json` 保持 NOT_RUN。审计命令退出 0 表示材料核对完成，不表示 E1c 已就绪。公开结果仅列汇总和身份；逐成员盘点、运行回执和 registry 只在本地 `var/r1/`。当前没有供正式 E1c 执行的 READY 清单。

## 验证与工作区

```powershell
Set-Location 'D:\RAG-R0'
$env:PYTHONPATH='D:\RAG-R0\backend;E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_offline_runner.py backend/tests/test_replay_source_preparation.py -p no:cacheprovider --disable-warnings --tb=short
```

定向测试 **51 passed，退出 0，32 条依赖弃用警告**。本轮新增/调整包括 `scripts/rsi_candidates.py`、`rsi_feedback.py`、`run_rsi_offline.py`、`prepare_e1c_dev.py`、`demo_rsi_inheritance.ps1`、`experiments/r1/`、对应的候选/反馈/运行器测试及两份 R1 报告。R0 worker、业务 Prompt、Memory、Skill 没有改动。`git diff --check` 退出 0。

复用本地 Python 3.13、现有补充依赖及历史只读输入；未建新容器或复制 Chroma。新归档和只读盘点证据约 1.5 MiB，临时恢复比对目录结束后已清理。D 盘整盘开工/收尾观测约 133.2 GiB/133.2 GiB，不能用整盘变化估算任务资源。原 `D:\RAG knowledge` 的工作区保留原状，Git 状态项仍为 52。只有 R0 使用本地冻结提交，R1 源码和报告在 `D:\RAG-R0` 本地工作树，未推送。
