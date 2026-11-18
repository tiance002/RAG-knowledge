# RSI v2.4 S3.3 最终决定

**数据准入仍为 BLOCKED_DATA。DEV20-R2 不冻结，真实基线 NOT_RUN。** 本轮从现有 DEV-only 原件核查题、atom、原生成员与来源，交付可以逐条人工处理的工作包。历史选定 DEV20 仍为 20 题（17 检索、3 业务控制）；更早的 v2.3 DEV20 仍是另一套 20，两个分母不混合。12 道文档草案保持 DRAFT_UNGRADED_NOT_ADMITTED，已评分 0、已准入 0。

## 哪些缺口可处理

| 冻结套件 | 缺口总数 | 结构锁定 | 其余 seed / 支持几何待核对 |
| --- | ---: | ---: | ---: |
| Main | 25 | 9 | 16 |
| Question-paraphrase | 25 | 9 | 16 |
| Forward-seed | 27 | 9 | 18 |
| Backward-seed | 15 | 9 | 6 |
| 合计（套件 × atom） | 92 | 36 | 56 |
| 去重后的 atom | 32 | 9 | 23 |

9 个结构性 atom 涉及四题。三题各只有一个原生成员，另一题有两个成员且两者都是必留 Seed。现有绑定下无可删除的必要非 seed 成员。剩余 56 条的原始池中有非 seed 成员，但已核对的现有支持集中必要谓词仍由 Seed 提供，支持非 seed 计数为 0。23 个对应 atom 可列入人工几何与标注复核，**本轮已证明可直接合法补控的 atom 为 0**。只有在原生真实池里找到完整、必要、可合法删除的非 seed 支持，且能排除 Seed 上的替代成立组合，才能提交匹配负控制。保留原 `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED` 失败和证据边界。

## 草案、独立来源与标注缺口

DSM 8 题不能凭跨章节获取跨成员信用。九寨沟 4 题的现有原生记录虽有重叠，但所需事实集中于同一个有效支持成员；草案没有自己的真实 Seed 分配。逐题目标事实、定位、已知成员与 UNKNOWN 分开写入 CSV 和标注包。待标注的 literal predicate 与引用是审核材料。人工决定和独立复核均未签署。

31 个已绑定原始来源文件的 SHA256 全部匹配，家族独立性已准入 0。从可用元数据核对，必要 DEV 源与封存源的精确哈希交集为 0，家族层交叉仍 UNKNOWN，既有历史曝光不能用文件哈希消除。六类现行门各仍差至少 2 个合格正例家族和匹配负控制，合计至少 12 个家族–类别占位。若同一对家族能分别满足所有类别，逻辑下限为 2 个新获准家族，实际需要新增采集多少来源仍 UNKNOWN。多来源支持必须有同一事实设计下真正独立的支持关系。目前 20 个 R2 槽位的已准入题为 0，缺少完整的 20 个合格输入；不发布题单凑数。

## 基线和验证

4/4 历史 DEV manifest 身份可核对，选定 wrapper 和父代源码/配置的哈希匹配，来源与 Gold 仍待完成实际准入。因此不提出真实执行启动，也未运行基线或生成 `BASELINE_QUALIFIED`。原累计账本 668 条事件，读取前后 SHA256 同为 `b1cbb1b5d178d6c716a508ed42bdb575370e9790f45c5b4bdfcd728b4ad3be52`。

专项数据校验、原始日志和最终四环境 CI 由草稿 PR 回执关联。M0 757 条投影复算、三份旧输出逐字节一致，九份冻结输入和六保护文件 SHA256 通过。未读取 FINAL20 题目、答案和证据正文；未打开混合 Gold 全表。原生 Chunk、Seed、Gold、Evaluator、统计门及正式业务代码维持原样。

- [逐题 CSV](evidence/s3-v24-s33-question-audit.csv)
- [逐 atom 缺口 CSV](evidence/s3-v24-s33-atom-control-gap.csv)
- [来源家族](evidence/s3-v24-s33-source-family-map.json)
- [人工 Gold 工作包](s3-v24-s33-gold-annotation-package.md)
- [DEV20-R2 可行性](evidence/s3-v24-s33-dev20-r2-feasibility.json)
- [基线准备度](s3-v24-s33-baseline-qualification-readiness.md)
- [历史保护哈希](evidence/s3-v24-s33-history-guard.json)

复用原 DEV 证据、Registry 只读字节和既有测试环境。新增审查材料和合成测试产物保存在独立工作树，未复制语料、索引或数据库。原始资料、失败与 UNKNOWN 留存。付费调用 0、真实回放 0、新候选 0；本阶段止于人工审查。
