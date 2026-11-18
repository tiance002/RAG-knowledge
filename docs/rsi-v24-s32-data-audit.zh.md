# RSI v2.4 S3.2 数据审计与补充题草案

2026-11-18 结论：**BLOCKED_DATA；真实基线尚未取得资格。** 本次只读复用 DEV 专用审计、编译回执、结构证据和失败日志，核对允许读取的来源字节；没有读取 FINAL20 题目/答案/证据正文，没有打开混合 Gold 全表，没有 ECS、模型调用或真实回放。新增草案见 [补充题草案](rsi-v24-s32-question-drafts.zh.md)，不能替换冻结 DEV20 或记为合格覆盖。

## 两套 DEV20 必须分别计算

| 数据群 | 已有材料 | 本次结论 |
| --- | --- | --- |
| 旧 v2.3 DEV20 | 20 来源/20 题；MIRACL 中文13、LongBench 中文4、SciFact 3；26 事实原子；历史20 prepare、40 deliver、26 原子正负对执行控制 | 历史收集执行通过，但独立来源家族准入0；不能与后来的用户选择题合并分母 |
| 用户从冻结 QA300 选择的 DEV20 | 20题、31份原始资料；17检索题、3历史/直接生成业务控制；72 canonical claims；四套 fixed-history fixture | 实际 prepare 已失败；并非仅停留在“尚未标注”阶段。20题保持原分母，3业务控制不能伪造检索成员 |
| 其他本地 QA 候选 | QA500 development runtime、reused base、enterprise/mtrag 审查和 source inventory 等文件存在 | 本次仅目录和大小清点；多数表可能混入 FINAL 身份，未打开其题目/Gold，不能称为已安全抽取的未暴露补充池 |

本次逐文件读取 DEV 专用 audit 中的31个 `local_binding` 并计算 SHA256：**31/31 匹配、0缺失、0不匹配，共6,163,727 bytes**（9 md、21 txt、1 pdf）。这证明现存原始资料与既有 DEV 绑定一致；没有证明新鲜性、独立家族、语义正确或当前索引可检索。没有复制原文、索引或数据库。

## 后来的 Gold 编译和真实失败不能被早期概述覆盖

复核原 `gold-native-literal-r5/build-receipt.restricted.json`：SHA256 `6e0f684d22f6c0c89e2b0d2294dca284e4cbe67f383b3e44f1c6d8a9a6f8080b`；status=`COMPILED_NOT_EXECUTED`，accepted claims=72，evidence refs=68，matched refs=66，`production_qualified=false`。这与最早接收阶段的136次原始 evidence 引用不是同一分母。最早136次中125次显式 Gold 来源哈希匹配、11次只有快照绑定的区别继续保留，不倒算成完全 Gold 绑定。

历史原始日志 SHA256 `add8eaedf9ff13e62168d118f4dbfc2a09c96e7c745a3a0e4dc35a60bb367dcd` 确认：

```text
ValueError: COVERAGE_EVERY_ATOM_CONTROL_REQUIRED
```

历史 S7 报告记录该命令 exit 1、发生于 capsule/模型调用之前；本次没有重新执行。未发现独立 exit 小回执，因此 exit 1 的来源是历史 S7 记录，而异常本身已与原始日志核对。

| 套件 | 缺失的 atom 负控制数 |
| --- | ---: |
| main | 25 |
| question-paraphrase | 25 |
| forward-seed | 27 |
| backward-seed | 15 |

结构证据 SHA256 `0315ebc5bd1c616348146d91fafc3ea77de8e4d99d93eeca0ebc23bda8514005` 指向4题/9 atoms的单 chunk 下限：`forward-session14-v7-s1-t10`、`terra-ecs-b8-006`、`terra-ecs-b6-019`、`terra-ecs-b11-005`。唯一成员为 seed 时不能删除；复制/拆 chunk 或删除 seed 均不能制造合法 nonseed 控制。即使修正 backward-seed 的其余6个几何缺口，仍有9个结构缺口。

已有 S7 记录还保留31条 claim 的部分 component/category 无法字面化，以及 qst_0131 的2条跨 chunk 完整引文未匹配；不能把72条可执行 claim 说成全部 canonical 语义已经完整形式化。本次没有重阅混合 Gold 或重新评分。

## 六类准入情况

| 类别 | 可以复用的事实 | 仍未满足的要求 |
| --- | --- | --- |
| NONSEED_NECESSITY | 历史非seed删除设计及明确失败 | 每个必要 atom 的合法控制；4题/9 atoms结构阻塞 |
| CROSS_MEMBER_RELATION | 部分 literal 组件与类别映射 | 不把单块内的多事实当跨成员；仍需真实成员/种子配置和配对核查 |
| NEGATION_CONDITION | 原始DEV中已有可定位条件材料 | 未字面化条件保留 UNKNOWN；标签不是控制通过 |
| UNIT_SCOPE_OWNERSHIP | 已有数值/归属标注和原文 | 数值、单位、对象、范围需逐项绑定，不补造抽象单位 |
| MULTI_SOURCE_SUPPORT | 原始两条多来源 atom 都落在 forward-session14 | 其中映射仅1或2文件且均单chunk；无法构成两个独立家族正例与负控制 |
| CITATION_SUPPORT | literal refs 66/68匹配及字节支持 | 完整引用与窄切片分开报告；引用可定位不等于覆盖或正确回答 |

六类没有本次新增已准入独立家族或实跑正负对，统一保持 BLOCKED_DATA。旧DEV原子标签数量、CSV类别数量、literal类别数量、独立家族数、真实控制数必须分别报告。

## 来源家族与暴露

旧来源账本报告101身份、18已消耗核查身份及统一UNKNOWN依赖分量；它们不是101个独立家族。后来选定DEV的31文件也不能凭不同哈希、UUID、标题或格式认定独立。既有接收报告指出4道DEV捕获额外召回6份FINAL来源、共13引用，且与DEV必要Gold支持精确交集为0；本次只复用该元数据结论，未读取这些FINAL正文。旧会话前缀6题含7个FINAL题号的问题同样保留，历史正文不能重新作为开发输入。

新草案只从 DEV-only audit 明确列出的 DSM 规范文本和九寨沟文本派生，共12题、2个已暴露文档组，**已准入独立家族0**。源组沿用旧身份；不称新来源、新Gold或新鲜终测。没有创建或追加正式 ExposureLedger，草案须在任何实际使用前进入独立开发暴露登记。九寨沟来源在已有native资料中属于单chunk，不能因草案数量增加而补足nonseed控制。

## 后续数据工作与真实基线

安全增补入口应为显式DEV-only提取件，或有可核对开发用途记录的独立文档。全表含封存题时应由封闭流程提供仅DEV投影；不能先读完整Gold再过滤。新增题必须逐项绑定源版本/家族、精确证据、必要atom、真实native成员与seed、正负控制，并保持冻结旧20题和补充草案的不同分母。文档题可以先起草，不能由此直接签发Gold、BASELINE_QUALIFIED、ADOPT或VERIFIED。

实际 A/A 基线、当前合格父代、六类覆盖实跑、冻结经验与正式分析计划均未在本次产生；真实基线状态 NOT_RUN，现有准备失败状态保留。不得用旧正常退出、合成测试或模型连通性代替真实基线资格。

## 验证与资源

本次只读审计命令 exit 0；未运行新的pytest/模型性能测试。原始失败日志已核对，输出截断的辅助浏览不作为完整数据证明；关键计数以小型JSON逐字段读取和本次31个源文件哈希核对为依据。复用现有Python和本地来源/回执；新增本文、12题草案及[脱敏审计回执](evidence/rsi-v24-s32-data-audit.json)；新容器/数据库/索引副本0、删除0。保留源数据、Gold、失败、UNKNOWN及日志供恢复审查。

磁盘清点前：D used 187,502,997,504，E used 432,382,382,080 bytes；清点后数值见JSON。期间其他代理并发工作，盘符变化不能全部归因本文。没有清理用途不明或其他任务资源。
