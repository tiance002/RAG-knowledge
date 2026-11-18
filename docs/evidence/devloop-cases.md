# DEV14 原生输入冻结说明

本批输入用于离线 selector 诊断：8 个历史完整回答未达 CORRECT 的样本（2 INCORRECT、6 PARTIAL），以及 6 个历史 CORRECT 样本。历史标签只定义诊断分组，不能作为本轮 selector 输出正确性的判据。

私有完整输入位于 `var/devloop/cases.native-dev14.json`，由 `rsik.dev_debug_cases.load_cases(path)` 读取。受版本控制的 `devloop-cases.json` 只记录来源绑定、题号、历史标签、数量、哈希、资源和限制，不包含题目、Gold、原文内容或历史回答。

数据来自已存在且显式标为 DEV 的 `gold-native-literal-r5/main/cases.json` 和 `DEV_20_question_ids.csv`。选择规则在新候选执行前冻结：按照 DEV CSV 原顺序，在 17 个有真实 seed 的检索样本中选前 8 个非 CORRECT 与前 6 个 CORRECT；3 个无检索业务控制不进入池。保持原生 chunk、file 和 seed ID 不变；`members` 为原生 fixture chunk 池的直接投影，仅从同一 fixture 文件表补充 `kb_id`。每个 seed 必须存在于 member 池，每段原文必须匹配其归档 `content_sha256`。

这批输入是 **归档原生 fixture chunk 池**，没有冒充历史 SSE 已扩展池，也没有重现历史请求的完整检索、扩展、生成和引用过程。直接投影的成员全集适合观察同一真实原文输入上的 selector 行为及字符变化；不能证明历史端到端回答修复或正式服务收益。

没有找到可直接作为本批输入权威的已准入 atom 保留谓词。原生 fixture 的 source relation 有 AI_REVIEWED 标记，但其中接受的 Gold claim 仍带 UNREVIEWED_NOT_ADMITTED，不能提升成 AUDITED。因此冻结输入 `atoms=[]`，保留率及语义结论必须为 UNKNOWN。不能将“没有检测到丢失”解释为“Gold 原子全部保留”。

也检查了已有 DEV200 prepared payload 的结构：真实 delivered citation 保存 seed_child_ids/seed_evidence_ids，但 parent 成员池常常没有原 child seed 记录。这不是本批 selector 原生池，未进行 seed 到 parent 的重映射，也未把它作为可执行的历史池。没有打开 FINAL20 内容，没有读取混合 Gold 全表，没有调用模型、provider、线上服务、数据库或索引。

冻结命令退出码为 0，输出为 `FROZEN_NATIVE_DIAGNOSTIC_INPUTS`，14/14 个样本通过原生 seed/member 绑定及内容哈希校验。原始冻结审查记录在 `var/devloop/cases.freeze-audit.json`。本次复用已有 DEV fixture、DEV CSV 和 Python；仅新建私有冻结 JSON、适配器和审计元数据，没有新建或复制环境、语料库、数据库、索引和容器，没有删除资源。磁盘变化见 `devloop-cases.json` 的 `resources`；保留私有 payload 用于复现和审查。
