# 设置、主链接线、旧实现与迁移冲突审查

日期：2026-11-05。对象：D:\RAG knowledge 当前工作树，HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e` 加已有未提交修改。三个子代理分别复核持久化、前端接线、旧实现与降级；主代理整合源码证据。只读审查生产代码，只新增本报告；不改数据库、环境或服务，不调用模型。没有运行行为测试，所有“确认”指源码调用关系，不代表已复现线上事故。不是全仓每一个历史脚本的穷尽审计。

## 一、未接入和互相覆盖

| ID | 判断、影响与触发条件 | 源码位置 | 最小处理建议 |
|---|---|---|---|
| S01 | 三个 DEFAULT_*_MODEL_ID 可保存、显示和启动加载，但未找到后端实际服务消费；前端 Chat 也不读取系统设置。保存默认模型不保证问答/建库使用它。 | api/settings.py:27-34、38-45；services/chat.py:180-194、556-568；services/llm.py:300 起；services/embedding.py:20-23；services/vlm.py:18-22 | 明确按请求选择、知识库绑定、系统默认分别解析模型；正常界面未显式选择时应用默认值。不能把 ID 当供应商 model_name。 |
| S02 | 聊天页在没有选中或选中项失效时自动选第一个模型，并发送 model_id；这是自动默认行为，不是用户明确覆盖全局设置。 | frontend/src/pages/Chat/index.tsx:171-181、224-227、346 | 读取服务器默认，再考虑现有有效会话选择；失效时提示，不静默随机换供应商。 |
| S03 | 建库表单隐藏字段默认 sys_embedding/default，不取设置页默认模型；后端直接保存请求值。 | frontend/src/pages/KnowledgeBase/List/index.tsx:306-318；backend/app/api/knowledge_base.py:create_knowledge_base | 初始化新建表单时使用有效系统默认；保留已有知识库的模型绑定，不自动改现有索引。 |
| S04 | 解析弹窗和多文件预览硬编码 500/50，作为显式请求传给后端；FileParser.parse 也把缺失大小回退为 500。全局切块设置可能没有机会生效。 | frontend/src/pages/KnowledgeBase/Detail/ParseModal.tsx:95-96；MultiFilePreview/index.tsx:50-51；api/files.py:229-256；services/parser.py:parse，约 825-868 | 从系统默认初始化界面；区分真正手动覆盖与默认展示值。任务接收时冻结实际配置。 |
| S05 | TextSplitter 的 overlap 使用 `chunk_overlap or settings.CHUNK_OVERLAP`，显式 0 被非零默认替换；非 custom 模式还强制改成块长 10%。 | services/parser.py:97-98、FileParser.parse 约 845-862；tasks/parse_tasks.py:275 | 用 is None 判断缺失。非 custom 的 10% 属现有策略，需显示并单独决定，不能迁移时静默更改。 |
| S06 | max_concurrency 保存到 MAX_PARSING_WORKERS，但当前解析 API 用 BackgroundTasks；该参数用于 Celery worker_concurrency，未找到约束实际 BackgroundTasks 并发的接线。 | api/files.py:247-258、308、356；tasks/celery_app.py:29 | 设置页必须说明真实生效范围；若要控制当前解析并发，再做最小接线，不以迁移存储代替并发控制。 |
| S07 | 手动改写按钮发送所选 model_id，后端 RewriteRequest 没有这个字段，接口创建默认 LLMService；按钮也未提交对话历史。 | frontend/src/pages/Chat/index.tsx:274；schemas/schemas.py:197-200；api/chat.py:26-34 | 同一接口明确接受并解析模型与真实历史；与自动改写共享语言和实体保留规则。 |
| S08 | ChatRequest 接受 use_rewrite，但 Agent 分支不转交该参数；普通分支会转交。Agent 自行生成工具 query，不代表该开关控制了 Agent 改写。 | services/chat.py:chat_stream 约 897-933；services/agent_workflow.py:agent_node、execute_tools | 对外说明作用范围；若要统一开关，先定义 Agent 行为，避免再增加一遍重复改写。 |
| S09 | 模型删除不检查系统默认和 KB 绑定。KB 指向非空但不存在的 CustomModel 时，检索回退系统 Embedding，且此处未标记 degraded；同维异模型可能产生看似合理的错误相似度。 | api/settings.py:196-212；models/models.py:43；services/retrieval.py:236-258 | 优先级高：保护正在使用的绑定；显式失效引用应报可识别错误，不自动换 Embedding 查询已有索引。未实测删除生产模型。 |
| S10 | 未找到当前入库主链调用 VLMService.describe_image；PDF/Word/独立图片生成图片标记与路径，随后入库。KB vlm_model 没有在该描述链路被消费。 | services/vlm.py:15-24；services/parser.py:1031-1036、1244-1250、1506-1540；tasks/parse_tasks.py:397-407 | 不能声称配置 VLM 后已经自动生成图片描述。独立核对需求再补接线。增强 PDF OCR、Doubao 多模态向量是其他能力，不因此判为无效。 |
| S11 | 最大历史轮数仅 Agent 输入和改写裁剪；普通直接聊天/普通 RAG 仍传入全部消息。 | services/chat.py:206-217、455-464、543-547；services/grounded_generation.py:177；services/llm.py:346-353 | 统一需要限制的真实模型调用边界，保留当前问题及正确历史语义；不能仅显示设置已保存。 |
| S12 | 已有向量的 KB 绑定 sys_embedding 时，解析弹窗排除 sys_embedding、要求选择自定义模型并自动作为 vector_model；后端已有向量保护会拒绝不同 ID。 | frontend/Detail/ParseModal.tsx:39-42、55、68、252-268；List/index.tsx:306-318；tasks/parse_tasks.py:46-56 | 两条机制意图各自合理但组合可能让正常追加解析失败；应让界面复用 KB 绑定，不能放宽后端索引身份保护。这里只确认条件冲突，没有浏览器实测。 |
| S13 | 多文件预览重新打开 ParseModal 时没传 models，默认空数组，而弹窗模型项必填；默认配置还使用旧键 separators/chunking_mode。 | frontend/MultiFilePreview/index.tsx:49-54、633-639；Detail/ParseModal.tsx:17-25、34、262-267 | 补齐当前契约与模型来源，复用单文件入口；页面实际阻断待浏览器核验。 |

## 二、保留的旧实现、兼容接口和受开关控制能力

| ID | 当前状态 | 处理边界 |
|---|---|---|
| L01 | grounded_validation.validate_grounded_answer 没有生产调用，台账明确离线用途，普通及 Agent 回答标 validation_status=NOT_RUN。 | 这是已批准停用严格校验后的状态，不能为了“接入率”重新启用。表格上下文准备和来源辅助仍被生产使用，不能整模块删除。见 docs/engineering-consistency-ledger.md 的 N3/N8、services/chat.py:393、429、476-484、agent_workflow.py:377-394。 |
| L02 | Celery 包装任务、重试和时间限制仍在，但当前 API 调用 process_* 函数的 BackgroundTasks 路径，未调用 delay/apply_async。 | 旧入口可能供外部 Worker/历史脚本使用；仅凭主 API 不用不能直接删。当前 API 任务不能宣传具有 Celery 的重试/重启恢复保障；已确认任务丢失需运行验证。 |
| L03 | VLMService 仍从 services/__init__.py 导出，却未找到主入库调用。 | 标为未接入旧服务候选；不能依据它的失败回退字符串来解释当前正式入库行为。 |
| L04 | OllamaEmbeddings 已被 EmbeddingServiceAdapter 替代，但仍放在同一个可选 import 块。它导入失败会把 HAS_LANGCHAIN 设为 False，连带禁用另两项真正使用的切分器。 | 可独立清理的依赖耦合。仅发现旧 import 不代表模型运行时调用了 Ollama；运行是否触发取决于环境依赖。见 parser.py:21-27、105、335、352-359。 |
| L05 | RetrievalService.search 接受 use_cache=True，但源码明确为了索引身份安全始终绕过缓存。 | 有意兼容参数，不是当前缓存命中。每次可能重新 Embedding，成本需据真实调用统计。见 retrieval.py:271、287、307-308。 |
| L06 | enhanced PDF 路线受 PDF_PARSER_MODE 控制，代码默认 legacy；扫描表格另有开关。 | 有实现不代表部署启用。上轮部署核验为 legacy，此轮未访问 ECS 重新核验；不擅自切换正式解析器。见 config.py:77-81；parser.py:_parse_pdf。 |
| L07 | 动态路由默认 OFF，并有角色授权/绑定门；QUALITY_CHECK 默认 NONE，升级不是语义答案判分。截断/故障/无依据升级旗标被设为 true 时，当前运行端会拒绝未实现策略。 | 属受控且部分受限能力，不应声称所有路由策略都已生效，也不应为清理而放开。见 grounded_generation.py:_route_generation、236-255；model_router.py:decide_escalation。 |

## 三、现有持久化与提议迁移的覆盖风险

1. **当前不会在无记录时自动写入默认值**：load_persisted_settings 直接返回；有记录时只覆盖其中非 null 字段。应修正“现在已经自动初始化完整配置”的误解。
2. **损坏配置会阻止启动**：json.loads 或 model_validate 异常由 main.lifespan 直接传播；没有设置加载专用降级。类型错误、未来范围变化均可能触发；此轮未证明现库有坏数据。settings.py:38-45、main.py:24-31。
3. **多进程内存不一致**：get_settings 是进程内 lru_cache；PUT 仅更改处理请求的进程。Celery 只在导入时读取并发，不走 FastAPI 启动加载。单进程当前情形不等于已复现多进程事故。
4. **并发覆盖**：同一 JSON 行执行读→合并→写，无 revision/CAS；多进程并发保存不同字段可能后写覆盖前写。首次同时插入也有主键竞争。settings.py:95-105。
5. **null 清除未实现**：exclude_none=True 会忽略 null；模型字段当前 str 默认 None，与“null 表示明确未选择”的提议不一致。缺失、清除、恢复默认应定义为不同操作。
6. **字段组合和引用缺校验**：分别限制范围未保证 overlap<size；默认模型 ID 接受字符串但未检查存在、类型和激活状态，删除可使绑定失效。
7. **JSON 初始化覆盖风险属于新方案风险，尚未发生**：不能 `defaults.update(user)` 以外的错误合并、不能每次启动重新用 env 覆盖 DB；也不能直接把空的 DEFAULT_MODEL_ID 当作当前实际模型配置。保留先前有效运行值，并记录一次性迁移来源。

## 四、收敛后的迁移方案

- 数据库只成为这七项**系统默认值**的持久化权威。请求明确指定和知识库已有绑定仍按其作用范围生效。Redis 用户表达偏好保持独立；不迁入 system_settings，不从全局恢复默认操作清除个人偏好。
- 首次迁移在事务内读取现有记录；已有有效用户值优先保留。只对缺字段做补齐；模型 null 为明确未选择；数值 0 按契约保留；无效 JSON/值保留原证据并报告，不静默覆盖。迁移保存 schema_version 和 revision，可重复执行。
- 旧 env 的这七项只作为一次性迁移输入；迁移后不得继续作为这些项的运行覆盖源。部署地址、密钥、模型连接资料不随之删除。系统模型 sentinel（如 sys_embedding）的身份和原连接也必须在迁移前定清，不能粗暴删除 provider 配置。
- 先消除 Chat、建库、解析弹窗自动硬编码默认，再收敛服务端模型解析；显式用户选择与自动初始化分别记录。历史 DB 中从未接入的模型选择不能突然变成生产换模，迁移需列变更前后实际模型对照。
- 保存成功以 DB commit 为准，加载内存快照只在 commit 后。用 revision 拒绝过期更新，界面刷新确认，避免整表单覆盖另一页较新修改。任务/请求开始时固定一份配置，记录 revision；处理中不半途切换。
- 生效边界：历史轮数/默认回答模型对新请求生效；切块参数对新解析任务生效；已有索引不自动重建；并发数需接入当前执行器或标为重启生效。多进程先采用明确重载/重启约定，不必建设配置中心。

## 五、降级策略与最小外部验收方案

| 故障 | 现状与建议 |
|---|---|
| 启动读取设置失败 | 现状会启动失败。建议无可信快照时明确配置不可用；不能宣称数据库完全不可用时 RAG 仍必然能工作。已运行进程可对无需重新读 DB 的参数继续用最后成功加载快照，但显示版本/降级状态。 |
| 保存失败/锁竞争 | 不改当前内存、不报成功、不自动重放结果不明的写入；提示重新读取当前版本。 |
| 模型失效 | LLM/VLM 显式选择失效应提示重新选择；Embedding 绑定失效禁止静默跨模型降级。 |
| Redis 偏好不可读 | 当前 memory.get_preferences 已返回默认表达偏好并标 redis_fallback；写入/回读失败抛 MemoryUnavailable，不能显示保存成功。它与全局设置数据库是两套职责。 |
| Hybrid 某路故障 | 保留现有明确的降级状态，不把配置修复扩展成算法调整。Rerank 已区分 UNKNOWN 并阻止此类隐式 chat 重发；不要误判所有 fallback 均为重复调用。 |

交给测试者的最小方案：①设置页改默认模型，普通/Agent/新建库实际请求身份一致，用户明确请求另选模型仍有效；②改全局块长，两个解析界面初始值同步，显式 overlap=0 实际为零，非 custom 策略明确；③保存、重启、缺字段补齐、null 清除、JSON损坏均检查旧值不丢；④两个客户端 revision 冲突不覆盖；⑤失效的 KB Embedding 绑定不发起另一个模型调用；⑥BackgroundTasks 实际并发与页面声明相符；⑦手动改写的模型、历史与自动路径对齐；⑧配置变更不修改既有索引、Gold、Redis个人偏好。

上述均为待执行方案，NOT RUN。本次仅源码只读审查，检索命令 exit 0 的结果不代表产品行为通过；一次针对不存在前端路径的 rg 返回 2，已改用实际目录复核。没有重新执行历史模型请求。没有提交、推送、部署、清理资源或更改既有工作区内容。结论可用于优先级排序，修复仍需针对性复现与 Owner 确认后实施。
