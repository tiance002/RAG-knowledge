# RSI v2.4 M1-A.1 指标规范

新观测沿用 EventLog v2.2 和观测载荷 v1，另带 `metric_contract_version=rsi-metrics/m1a1`。报告按指标口径、证据模式、实验/epoch、角色 Profile 及指纹、兼容指纹和 Evaluator 版本分区。仅含旧输入的三个 M0 输出沿用原格式和原计算。混合输入按口径分别展示。

| 指标 | 旧口径的限制 | M1-A.1 分子与分母 |
| --- | --- | --- |
| S3 | 按已选事件计数，遗漏空操作 | 不同实际选出父代数 / 已完成且有效的选择操作数 |
| S6 | 只在选出父代的记录上计算 HHI | 各父代被选次数平方和 / 已完成且有效的选择操作数平方 |
| M1 | 多条返回结果可能被计为多次检索 | 唯一 retrieval_id 数，含空检索和已开始后中断 |
| M2 | 按 round 和 entry 归并，可能错配检索与注入 | 已检索且按原回执注入的 (retrieval_id, entry_id) 数 / 已检索的该配对数 |
| M3 | 归档匹配可能进入注入统计 | 已注入且被 Proposal 明确引用的 (round_id, entry_id) 数 / 已注入的该配对数 |
| P3/P4 | 缺少完整 Worker 时无法确定 AST | P3 为静态通过实例中的不同完整 AST 数 / 静态通过实例数。P4 用同一分母，分子为实例数减不同 AST 数 |

选择开始先记录唯一 selection_id 和 attempt。经 START、实际 SELECTED（返回一项时）及 COMPLETED 组成的有效选择才入完成分母。空返回计一次完成，父代次数为零。中断或冲突留 UNKNOWN。重复同义事件只计一次。若完成一次空选择和一次实际选择，则 S3=1/2，S6=1/4。

检索回执随接口返回，四个 section 的结果仍按原流程排序、去重并截取 top 3。注入事件在回执下记录实际入选的 entry，模型上下文继续使用原经验结构。单轮两次检索分别返回 E1/E2 与 E1/E3，只注入第一份并引用 E1 时，M1=2，M2=2/4，M3=1/2。完成的空检索 M1=1，M2=0/0（N/A）。缺结果的中断检索或无回执的注入使 M2 保留 UNKNOWN。M4 需要独立核查证据。

Worker 字节 SHA256 与 `ast.dump(ast.parse(source), include_attributes=False)` 的 SHA256 分开记录。无完整源或解析失败时 AST 为 UNKNOWN。Patch、hypothesis、expected impact 各自保留哈希。AST 相同表示所用 Python 解析器下结构相同，自然语言哈希只表示文本身份。跨运行比较需固定解释器版本。

账本继续提供请求与用量记录。观测库使用引用键关联账本，每次写入失败归 STORAGE_ERROR。缺终态的调用保持证据不完整。缓存恢复依赖已持久化的原请求和响应回执。
