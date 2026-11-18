# RSI v2.4 S3.4 控制与六类覆盖核查

**当前结论：BLOCKED_DATA。DEV20-R2 未冻结、未准入；合格正负控制及真实基线均为 NOT_RUN。** 本包只读核对明确为 DEV 的历史元数据，不生成 Gold、不指定 Seed，不把编译规格或解析成功算成控制通过。

历史选定 DEV20 为 20 题（17 检索、3 业务控制），更早 v2.3 DEV20 单独保留；S33 原 12 草案仍为已评分 0、已准入 0；R2 的 20 槽位中已准入 0。

## 已有规格与缺口

| DEV 套件 | literal atom 规格 | 已编译负控制规格 | 缺少负控制 | 实际执行 |
| --- | ---: | ---: | ---: | --- |
| main | 72 | 47 | 25 | NOT_EXECUTED |
| question-paraphrase | 72 | 47 | 25 | NOT_EXECUTED |
| forward-seed | 72 | 45 | 27 | NOT_EXECUTED |
| backward-seed | 72 | 57 | 15 | NOT_EXECUTED |
| 合计（套件 × atom） | 288 | 196 | 92 | NOT_EXECUTED |

196 是已有控制**规格**数量，无法据此报告正例 PRESENT 或负例 ABSENT。92 条缺口去重后为 32 个 atom：9 个结构性 atom 对应 36 条套件缺口，其余 23 个对应 56 条。92 条的已登记必要支持非 seed 数均为 0；原生池中存在其他非 seed 成员并不能证明这些成员提供必要事实。保留历史失败 `COVERAGE_EVERY_ATOM_CONTROL_REQUIRED`。

## 六类显式计数

| 类别 | 历史类别标签次数 | 已编译负控规格 | 缺控次数 / 去重 atom | 已准入正例 | 已准入独立家族 | 合格正控实际执行 | 匹配负控实际执行 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NONSEED_NECESSITY | 0 | 0 | 0 / 0 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |
| CROSS_MEMBER_RELATION | 8 | 3 | 5 / 2 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |
| NEGATION_CONDITION | 76 | 48 | 28 / 12 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |
| UNIT_SCOPE_OWNERSHIP | 100 | 82 | 18 / 8 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |
| MULTI_SOURCE_SUPPORT | 4 | 0 | 4 / 1 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |
| CITATION_SUPPORT | 288 | 196 | 92 / 32 | 0 | 0 | 0（NOT_RUN） | 0（NOT_RUN） |

类别可以重叠，不能横向相加当成题数。标签次数按四套件计数；缺口 atom 按 qid + atom_id 去重。标签与编译规格均未获得人工语义或来源独立性准入。现行每类至少 2 个正例、2 个独立正例家族，且逐例、逐家族均须匹配负控制；`MULTI_SOURCE_SUPPORT` 的每个合格支持设计本身须包含至少两个已证明独立的支持家族，多个文件 ID 不足以成立。

## 本轮可核对与仍不能执行的部分

可确定性核对 DEV 分区、文件哈希、72 × 4 原子规格与 196 + 92 的分母闭合，以及六类标签/缺控分布。实际合格控制必须先有来源准入、完整且经批准的必要 atom、真实原生成员及真实冻结 Seed，再用实际交付字节证明正例 PRESENT、合法移除必要非 seed 后负例 ABSENT。必须排除所有仍能成立的替代支持组合并保留全部必要 Seed。不得通过拆块、复制、人工补造 Seed、删必留 Seed 或放松门限补数。

S34 新增解析仅能提供原生位置和成员清单。没有真实检索 Seed 角色、已批准 Gold 和必要性证据时，leave-one-member 也只能是技术盘点，不能升级为合格匹配负控。本包未执行此类控制，没有观察到的正/负控制判定。DEV20-R2 仍缺完整的 20 个合格输入，本轮没有真实基线启动依据。

## 证据与资源

S34 来源核查另确认 31 个允许 DEV 文件的哈希全部匹配，但来源准入仍为 eligible 0 / blocked 0 / unknown 31，已准入独立家族 0；12 草案的人工签署或准入为 0。详见 [现有来源核查](rsi-v24-s34-existing-provenance.zh.md)。这项核对未解除控制执行门。

机器可读计数、逐套件输入哈希及门条件见 [s34-coverage.json](evidence/s34-coverage.json)。复算只读 DEV manifest、relations、control-spec 和 S33 缺口 CSV；未读取混合 Gold、FINAL 内容或 histories/source 正文。最初探测中发生一次 list/dict 形状假设错误、一次 gbk 解码错误；已显式使用 UTF-8 并以完整 JSON/CSV 重算，最终复算命令退出码 0，288 = 196 + 92，各套件亦闭合。早期被截断的控制台材料没有作为计数依据。此项验证只证明元数据计数，不证明控制实际通过。

复用历史 DEV 元数据与现有 Python；新建本说明和 JSON 两个小工件，语料/数据库/索引副本 0、容器 0、删除 0。保留失败、UNKNOWN 和未执行状态供复核。E 盘前后用量记录在 JSON，存在并发任务，磁盘差额不全归因于本包。

S3.4 [原生解析回执](evidence/s34-native-map.json)已核验 5 源/221 成员，各源两次解析一致。明确处于隔离字符切分；没有执行原生检索或分配 Seed。因此定位成功仍只计词面定位，合格控制实跑为 0。
