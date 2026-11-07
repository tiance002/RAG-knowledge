# 父子分块、父块扩展和上传去重实施验证报告

日期：2026-11-07。代码已实现并通过定向验证；没有向现有知识库执行数据迁移、重建或上线切换。工作区原有大量未提交修改均保留，没有 reset/revert/提交。

## 数据和实施边界

先读审计记录，再添加兼容字段与新路径。根目录 local.db 仍为 1 KB / 1 文件 / 46 普通块 / 46 BM25 行；源 XLSX 可读，Chroma 0 collection / 0 embeddings。backend/local.db 的知识数据表为空。最终 audit-final 的三个根库逻辑指纹与 audit-after 相同；初始 audit-before 未采集全文指纹，因此不声称完整初始正文哈希证明。详见 parent-child-migration-20261107.md。

首次旧回归 fixture 意外初始化了 backend/local.db 的 system_settings 默认记录，现有知识文件/切片未被该操作写入。该配置行保留并披露；后续测试统一 DATABASE_URL=sqlite:// 与独立 fixture 数据库。没有清理其他任务资源。

## 实现与契约

- FileDocument 增加 original_name、display_name、sha256、supersedes_file_id；DocumentChunk 增加 nullable start_offset/end_offset。SQLite 启动追加迁移幂等，仅加列和普通索引；本次未对历史库执行。历史父临时 ID 不自动推断为 UUID，不添加会误伤脏历史的父外键。
- parent_child 显式开启，旧模式仍可用。先生成父块 UUID，再生成 child UUID 和真实 parent_id；确认入库时整组重新分配 staging UUID 并精确映射父子关系，避免覆盖活跃向量。应用层验证父类型、同文件、父存在、子正文包含于父正文。
- 父子大小/overlap 单位为字符，参考默认 4096/0、384/48。参数为 parent_chunk_size、parent_chunk_overlap、child_chunk_size、child_chunk_overlap。边界优先利用解析器已有标题/页/格式片段和段落/句末，缺少可靠结构则回退字符窗口。overlap 不跨解析器不同片段；上游旧 splitter 本身可能使用 token，此处最终窗口仍按字符严格控制。未证明适合当前语料。
- 父块 SQL 持久化且不进常规向量/BM25 候选，子块和原有独立表格/图片块正常索引。无法可靠映射源位置时 offset 留 NULL；父内局部切分不是原文件全局位置，不冒充精确定位。
- 新父子块命中后，即使 context_window=0 也回捞父块；该参数仅控制邻接。seed、文件和唯一父块使用批量 SQL 读取。同父命中合并正文一次，保存全部 seed_child_ids/matched_child_ids；按最佳 seed rerank（否则检索）选择组代表，并标记 score_origin。父引用/邻接引用清除独立相关性分数，不伪称父块被 rerank。
- 无父普通块正常返回；历史 dangling / wrong-type / cross-file 父链只返回可验证 seed 并标记 UNKNOWN。SQL 真实文件归属校验知识库范围。父读取异常沿用各入口 seed-only 降级契约。
- MAX_PARENT_LENGTH 默认 4000 字符，CONTEXT_MAX_CHARS 默认 24000 字符。父超限时退回完整命中子块，优先预留其他真实 seed 的预算；放不下的 seed 显式标记 SEED_OMITTED_OVER_LIMIT。同一成员 ID 只交付一次。
- parent 已提供结构上下文时不额外追加跨父邻居；普通块可按原窗口补邻居，识别到标题时不能越界。未知边界标 UNKNOWN。没有默认增加邻居相关性模型或二次 rerank；不同父块 overlap 的部分文本暂不做可能改变引用字节的模糊删除。
- 最终普通回答和 Agent 主生成调用前，计入消息、引用、提示词/历史及工具 schema 的应用层字符预算 GROUNDED_REQUEST_MAX_CHARS（32000）；超限明确拒绝发起模型请求，进入现有错误/兜底流程。它不是模型 token 上限保证，也不预留未知 provider framing/output tokens。
- 扩展日志只有 ID、长度、状态和有限降级枚举；沿用原正文 snapshot 配置。核对现有 masking/sanitize，新增受控枚举而未放开任意正文。未验证远程 Langfuse 导出。

## 上传和版本

原字节按 1 MiB 分段保存并计算 SHA256；UUID_source.ext 存储 key 不包含用户路径。原始名不改，显示名独立分配 (1)/(2)。同 KB 的默认 auto 同名同哈希返回既有 ID/当前状态，不解析、不建重复索引；不同名同哈希独立。UI 用 check，任何同名（包括同哈希）先提示复用/新建/替换。显式 new 允许用户保留独立身份；空哈希不当成相等，可读历史源可只读计算用于比较。

SQLite BEGIN IMMEDIATE 将检查/命名/插入置于同一写事务，多进程适用；非 SQLite 锁定 KB 行，但旧 schema 的非 SQLite 迁移须专门实施，本次未验证该方言。主动 replace 建立新 file_id 和 supersedes_file_id；成功入库前旧版本继续检索，成功后 SQL 回查过滤旧版本，旧源/块/索引不删除。同目标多个成功版本选择最新创建的一个；失败的候选不取代成功版本。删除新版会使仍保留的旧版重新成为候选；独立同名条目不受此关系影响。

旧版本向量暂保留在同一 collection，会占候选额度和磁盘；真正从索引移除旧版本应另行评估、明确执行。此次没有自动删除旧向量。

## 验证结果

| 验证 | 退出码 | 分母与结果 | 证据 |
|---|---:|---|---|
| 最终后端综合定向回归 | 0 | 139 tests / 139 passed / 0 failed / 0 errors / 0 skipped | final-backend.log、final-backend.xml |
| 补充解析失败保留旧状态 | 0 | 1 / 1 passed（与上述139项不同） | parse-failure.log |
| 主前端 TypeScript + Vite | 0 | 构建通过，存在 bundle >500kB 提示 | frontend-build-fixed.log |
| frontend-weknora TypeScript + Vite | 0 | 构建通过 | weknora-build-final.log |
| 前端 Node 回归 | 0 | 13 / 13 passed，0 skipped/cancelled | frontend-node-regressions.log |
| 真实字节离线结构检查 | 0 | README 四个词项，6/6 seed 原文保留；46/46 XLSX 表格原文保留 | offline-comparison.json/log |
| 最终只读审计 | 0 | 根/后端知识 ID、块类型/数量与最初相符 | audit-final.json/log |
| diff whitespace（允许 Windows CRLF） | 0 | 指定改动文件无额外空白错误 | diff-check.log |

综合回归使用现有 .venv；DATABASE_URL=sqlite://，RAG_OBSERVABILITY_ENABLED=false，pytest -q --tb=short -rA，JUnit保存逐项分母。模型、embedding、向量故障等使用明确替身；不是在线模型效果实验。741个告警主要是现有 Pydantic/datetime.utcnow/LangChain 弃用项，日志中没有隐藏失败或 skip。补充测试原始 traceback 是预期注入的 synthetic parser failure，断言随后验证旧状态、旧块、旧向量均在。

覆盖 UUID 父子、同父/不同父、跨文件/缺失父、普通块、超预算、零邻接窗口仍回捞父块、最高分 seed 身份、邻居不能消耗后续 seed 预算、来源 metadata、同名不同/相同哈希、不同名同哈希、并发重复、HTTP选择、删除隔离、版本成功激活/失败保留、解析/embedding/SQL/向量失败恢复以及原检索融合/UNKNOWN处理。

初轮失败均保留原始日志：regression-initial.log、broadened-regression-initial.log、version-fixture-initial.log。原因包括旧 fixture 使用 dangling siblings、ChatService.__new__ 缺运行设置替身、既有模型绑定/任务契约变更和直接删含子记录文件的测试 fixture。已修正测试数据/隔离与预期契约；没有通过删除业务断言或跳过测试来取得通过。主前端先前 TypeScript 阻塞由兼容 props/types 与真正未使用声明的最小修复解决。

## 实际文档检查的限制

README 当前 SHA bfa1214ea3c91b95b070fe3388cbd680232e7d883f305983036ab48a94b2ac11，5939字符：旧普通 splitter 得到8块，参考新配置得2父/21子。4个词项 probe 最终各返回1个父引用，正文3993字符，6个 seed 全保留。引用沿用 evidence_text().strip()；Celery 的1个 seed 边界空白发生精确字节差异，同时报告 raw exact 和 strip-normalized 口径。首次扩展包含import启动开销约826ms，其余约0.8–1.34ms，不是线上延迟基准。

没有运行真实向量/BM25召回、模型回答或 Gold/qrel 评分，真实召回率、答案质量、引用正确性和父块语义噪声均尚未评测。扩展增加的字符不等于相关内容增益。4096/384与字符预算均为可配置参考值，不能据单测通过声称质量提升。

## 迁移与使用

解析请求增加 parent_child=true 和四个字符窗口参数；结构化模式可同时选择 chunk_mode=structure。UI解析弹窗有显式开关。应用初始化执行 additive schema 后，新上传即可记录哈希；已有源哈希回填用 scripts/migrate_parent_child.py 默认预览，--apply 需 journal、批次及维护路径。

历史库尚未迁移；旧关系无法证明时保留原索引，用新的独立文件版本重新解析/校验。scripts/migrate_parent_child.py 生成重建预览清单，脚本不自动执行破坏性重建。数据库迁移、远程服务验证和生产流量切换均不在本次实际操作中。

## 资源收尾

复用 .venv、两套前端 node_modules、源 XLSX、README、现有SQL/Chroma和冻结产物目录；未复制整份语料/数据库/索引，未下载依赖，未创建或停止容器。新建代码、测试、迁移工具、报告及原始证据；两套 dist 由构建再生成。离线脚本 TemporaryDirectory 自行清理，上传拒绝/失败的 staging 文件由finally清理并被测试验证。

本任务 test-temp-final 是139项最终测试的独立合成源/数据库目录，大小3007676字节。清理前已核实绝对路径处于任务目录，测试进程已结束；递归删除被自动审批审查以 blocked by policy 拒绝，目录保留，未尝试绕过。test-temp-parse为补充用例的独立临时目录。原始日志、JUnit、审计、UNKNOWN/失败记录保留供复查。Docker daemon不可访问，未确认的容器不做清理。

初始审计D盘 used=152983760896 / free=141989052416；清理尝试前（未成功删除）used=152988983296 / free=141983830016；最终只读审计used=152988999680 / free=141983813632字节。变化包含并行构建及系统活动，不能全部归因于代码文件。cleanup.json保存临时目录与拒绝原因。受策略限制的临时目录清理仍待外部执行。
