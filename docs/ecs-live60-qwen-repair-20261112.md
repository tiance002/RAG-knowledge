# ECS Live60 Qwen 修复技术报告（2026-11-12）

初始记录：2026-11-12T22:17:31+08:00；最终更新：2026-11-12T22:56:25+08:00。**当前状态：执行及 60 轮 AI_REVIEW 已完成。** v6 共尝试 60 轮，59 轮得到非空完整回答，031 因本地资源护栏中止而为 UNKNOWN，0 轮未运行。独立语义审查为 FULL 38／FAIL 21／UNKNOWN 1；这是 AI_REVIEW，未经人工逐题确认，也不表示最终验收通过。质量结论及逐题依据以[最终独立审查](../var/ecs-live60-20261112/review-v6-report.md)和[60 轮账本](../var/ecs-live60-20261112/review-v6-results.json)为准。

本次范围是修复后继续完成同一份六会话、60 轮评测，不更改题目、Gold、检索权重或输出策略。执行代理负责 ECS 操作及真实模型调用；本报告作者负责来源审计、对应本地最小实现、零调用重放及测试。主运行报告由执行代理维护，本文只记录修复技术证据。

## 1. 冻结范围与不可混合的结果

最终题集为两段纯中文、两段纯英文、两段原有中英混合会话，各 10 轮，共中文 30／英文 30，包含 40 个真实追问及 21 个追问的追问。所有原问题、语言切换和依赖顺序保持原样，后续轮次仅携带本段真实已发生的 user／assistant 内容，不能注入 Gold 或合成参考答案。

这些 60 个 ID 原属 `frozen_test`，按用户明确要求暴露／测试，今后 RSI 训练应排除；未选的 240 题不能与已测试题混称为独立留出集。来源映射 37 轮已核实、23 轮未核实；未核实题照常执行并对照 Gold 判回答，来源召回保持 UNKNOWN，不因缺少绑定而跳过或计为答错。见[冻结 V4 题集及 Gold 定位](../var/ecs-live60-20261112/data-audit-v4.json)。

| 工件／尝试 | 已观察结果 | 解释边界 |
|---|---|---|
| V4 首题 | 外层传输约 180.039 秒超时，退出码 28；改写和检索已完成，226 条引用、无最终回答 | 最终提供商结果 UNKNOWN，不能说改写也失败 |
| V4 后两题 | 各约 121.037 秒；外层允许 360 秒且传输退出 0；API 记录 rewrite 的 `APITimeoutError` | 是服务端 SDK 120 秒期限，不是外层 curl 期限；无最终 usage／finish_reason |
| V4 汇总 | 3 UNKNOWN、57 NOT_RUN、0 完成 | 原记录保留，不改成重试成功或零费用 |
| V5 首题 | 改写约 2 秒、`rag_result` 约 11 秒；131.416 秒最终 UNKNOWN | thinking-off 已到达真实改写，但完整回答尚未修复 |
| 全量输入流式诊断 | 相同 226 引用，120.268 秒 `APITimeoutError`，0 chunk、无 HTTP 响应头 | 是独立诊断，不是 60 题质量成绩，也没有得到已知提供商 HTTP 错误 |
| 50 seed 小输入流式对照 | HTTP 200；响应头 28.130 秒、首个正文 28.134 秒、结束 34.441 秒；`stop`、160 chunk | 只作输入量诊断，不混入正式 v6 回答分数 |
| V6 最终 | 60 尝试／59 完整非空回答／031 资源护栏 UNKNOWN／0 NOT_RUN | 60 轮已完成 AI_REVIEW：38 FULL、21 FAIL、1 UNKNOWN；完整传输不等于答案正确，旧 V4／V5 UNKNOWN 保留 |

原始记录：[V4 汇总](../var/ecs-live60-20261112/execution-summary-v4.json)、[V5 首题](../var/ecs-live60-20261112/qwen-repaired-v5-results/000/result.json)、[全输入流式诊断](../var/ecs-live60-20261112/repair-v5/full-stream-diagnostic.json)、[50 seed 对照](../var/ecs-live60-20261112/repair-v5/seed-only-stream-diagnostic.json)。所有新尝试独立保存，旧 UNKNOWN 不覆盖。

## 2. 已证实的问题与尚不能下的结论

### 默认模型：持久化值必须被实际消费者使用

设置已经通过 API／SQL 切为 `Qwen/Qwen3.5-4B`，`system_settings` revision 从 1 到 2，唯一变化为 `default_llm_model`。ECS 旧 `api/chat.py` 仍把请求中的 `model_id=None` 原样传给服务，普通链路可能继续使用部署配置中的旧 LLM，因此仅 UI／设置回读成功不够。

修复在 SSE 开始前调用现有 `resolve_model(session, 'chat', model_id)`：显式请求 ID 优先；仅 None 时读取持久化默认值；无效显式 ID 返回 HTTP 400，不能静默换模型。普通回答及 Agent 均消费已确定的 CustomModel ID。手动 `/chat/rewrite` 同样从解析后的绑定构造 LLMService；现有 RewriteRequest 只有 query／history，不声称支持其 DTO 未声明的显式模型覆盖。`sys_llm` 仍是原有部署别名；真正未选择模型时返回 400，而不是新加自动云端／旧模型回退。

[默认设置变更回执](../var/ecs-live60-20261112/default-model-change.json)、[实际消费者源码片段](../var/ecs-live60-20261112/default-binding-live-proof.txt)、[独立审查](../var/ecs-live60-20261112/default-consumer-review.md)。Embedding／VLM 及其他设置未随此次默认 LLM 变更。

### 因素一：thinking-off 参数未接入实际请求

修复前部署的 LLMService 使用 `AsyncOpenAI(timeout=120, max_retries=0)`；改写、普通直调及 ChatOpenAI 工厂没有共享的 thinking-off 参数。路由角色曾能附加 `extra_body`，但本轮 router=OFF。普通最终回答还会在 `grounded_generation` 内直接调用 SDK，因此只修 rewrite 或只修 `get_chat_model()` 都会漏路径。

新增共享 `provider_request_parameters(target_model=None, *, extra_body=None)`，仅对准确模型 `Qwen/Qwen3.5-4B` 且主机为 `api.siliconflow.cn/com` 默认加入 `enable_thinking=False`。显式角色参数优先，复制字典后使用 setdefault，不修改调用者原字典，不向其他模型／自托管 OpenAI 兼容端点乱发字段。

SiliconFlow 文档说明 thinking 开关及 SDK `extra_body` 用法，最终输出 max_tokens 与 reasoning budget 是不同参数：[API](https://docs.siliconflow.cn/docs/api/chat-completions-post)、[推理说明](https://docs.siliconflow.cn/docs/userguide/capabilities/reasoning)。LangChain 对提供商专有字段推荐使用 [extra_body](https://reference.langchain.com/python/langchain-openai/chat_models/base/BaseChatOpenAI/extra_body)。本次以实际 SDK 序列化后的 HTTP JSON 验证，线上 JSON 顶层确有 `enable_thinking:false`，不是只在对象属性里写了一个未发送的开关。

**最终修复不新增 Token 上限或默认值。** 改写原有 200、普通非流式／grounded 原有 4096 保持；普通流式及 get_chat_model 原有 None／未发送上限保持；显式预算及现有 `max_completion_tokens` 别名保持。早期提出的 native-cap 候选已撤回并标为 NOT_DEPLOYED，未混入最终修复。120 秒期限、max_retries=0、Prompt、检索权重均未改变。

实际部署 SDK 的 11 项 wire 前后对照，除目标 thinking 字段外其他 JSON 全部一致。直接 SDK 及 LangChain 注入 read timeout 均只有一次 HTTP 尝试，分类 UNKNOWN；已知 HTTP 400 为 FAILED，不伪装 UNKNOWN。[wire 前后与一致性检查](../var/ecs-live60-20261112/repair-v5/wire-single-factor-parity.json)。

独立小请求 OFF 探测 608.8 ms 返回 HTTP 200、实际模型 Qwen、`stop`、reasoning_tokens=0、一次请求。它验证参数支持及当时提供商可响应，不证明原三次 UNKNOWN 确实正在 thinking，也不能用它当完整 RAG 延迟。[OFF 探测](../var/ecs-live60-20261112/repair-v5/provider-off-diagnostic.json)。

### 因素二：当前实际扩展路径缺少聚合交付预算

V5 完整回答仍超时后，按真实部署 builder 和首题 SSE 零调用重建输入：50 个原始 seed 扩为 226 引用，涉及 15 文件，基础 messages 共 153,694 字符／310,608 UTF-8 字节；cl100k_base 估计 122,483 内容 Token。它不是 Qwen tokenizer 计数，也不是实际 usage；历史应用请求的偏好后缀未记录，故这只是重建基础量，不是假称完整线上 wire 已捕获。

106 次相同文本重复全部来自**不同文件及不同源文件 SHA**：同文件／同文件+context group／同源字节哈希内可安全折叠的重复均为 0。因此不能按文字相同跨来源去重。

实际 `chat.py` 导入并调用旧 `rerank.expand_context`，它只有每个父块的 MAX_PARENT_LENGTH，无聚合请求约束；`context_builder` 验证并输出全部成员。另一份已部署 `parent_context.py` 虽含 24,000 字符逻辑，却不是这条旧 ECS 调用路径。不能把“代码文件存在”当成“本轮已启用”。证据：[真实调用路径](../var/ecs-live60-20261112/repair-v5/actual-context-expander-proof.txt)、[重建规模](../var/ecs-live60-20261112/repair-diagnosis-v5-full-prompt.json)。

一次受控小输入诊断保留全部 50 个 SQL 原始 seed、全部 15 源文件、原问题及模板，省去 176 个非 seed 扩展，仅改变输入材料范围；stream/OFF/max_tokens4096/temperature0.7/include_usage 与前次全输入诊断一致。50 seed SQL 原文 16,366 字符，基础 messages 18,802 字符／38,012 字节，冻结请求 38,469 字节。选择先冻结，再单独打开 Gold，核实原 source quote `hj_dates` 1/1 保留；Gold 不进入选择器或请求。

小输入实际 prompt_tokens=11,619、completion_tokens=157、total_tokens=11,776、reasoning_tokens=0；这些是返回的真实 usage，与 cl100k 估算分开。与全输入 120 秒无头／无 chunk 的对照支持“输入规模也是影响因素”，但没有提供商内部追踪，仍不能断言全部延迟只由输入量造成。见[冻结请求与来源映射](../var/ecs-live60-20261112/repair-diagnosis-seed50-audit.json)。

## 3. 第二因素的最小实现与来源保护

新 helper 在现有 SQL 文件／KB 核验之后收集原 seed，在**实际活跃扩展器返回处**应用已有 24,000 字符聚合 allowance：

1. 原始独立 seed 的完整 SQL body、来源映射及已知标题／表格／单位／脚注／原生结构材料先列为 mandatory；不剪字符串。
2. 先预留 mandatory floor，只将剩余空间给原顺序中的完整可选 sibling／neighbor。
3. floor 超过 allowance 时仍保留 mandatory 并标 `mandatory_overrun`，不新增拒答门或偷偷丢 seed。
4. 未核实来源行整体保留并明确 UNKNOWN；不伪造 offset。冲突的 ID／文件／KB／字节／已知定位不被静默合并。
5. 不按文本相似度跨文件去重，不读取 Gold／qid 特例。所有保留成员维持真实 chunk ID 和文件归属；新展示序号与 provider 实际引用清单一致，重放另存旧序号→新序号映射。

24,000 是独立源成员内容及分隔开销的字符 allowance，**不是 Token，也不含全部系统指令／文件头**。已知结构字段全部保留，不代表已证明所有未编码的外部单位／表头依赖都可省；163 个省略项只标为可选扩展未交付，不宣称“与答案无关”。

冻结策略后的零模型调用重放执行了 helper 与捕获的实际 serializer：50/50 seed SQL body 哈希通过，15/15 源文件保留；226→63 引用，省略 163 可选成员；预算层原始唯一成员 23,992／24,000 字符，现有边界空白渲染后为 23,943；系统 Prompt 26,765 字符，cl100k 估计 23,521。serializer 全局按真实 ID 去重，实际最终重复 ID 为 0，没有隐藏的跨组重复把预算重新放大。Gold 仅在策略哈希冻结后用于独立检查，原 source quote 1/1 仍在。见[重放及完整来源／引用映射](../var/ecs-live60-20261112/repair-diagnosis-delivery-replay.json)。这不是全 60 题质量证明。**该零调用重放为 63 条引用；v6 首题真实运行实际返回 61 条引用，两者不是同一次请求，不能把重放计数写成线上实际计数。**

## 4. 本地与 ECS 差异、文件及版本

本地版本较新，已有池化 HTTP transport、查询理解／图像入口、流式回调和 parent_context 聚合路径；ECS 是旧 rerank 扩展器。没有将旧 ECS 整文件覆盖本地：本地保留新流程，只添加共享 thinking 参数以及 parent_context mandatory floor 的最小修复；旧 ECS 则使用捕获旧源生成的独立 rerank delta 和新 helper。

| 部署文件／阶段 | SHA-256 | 证据 |
|---|---|---|
| api/chat.py 修复前 | `4a23c83394b5839eeb0ebf46161568c61362f4d376f34badc473173cc8e5db92` | 原源捕获 |
| api/chat.py 默认消费者修复后 | `9bcd319db4844223cf9bc3004420f97a98bee4a0a1fe005d0d57a6781e9591f1` | default-fix-install-receipt.json |
| llm.py thinking 修复前 | `60bc77532bbed22e73d565f4fc7cbc04d7ae7b11d4ef8631f277dceded64b93c` | runtime-factory-evidence.txt |
| llm.py thinking 修复后候选／安装源 | `c3c9eb11d5d4f387b161ac6033b8b3182294087805a0f0fb37a88bdca6efb216` | repair-v5/install-receipt.json |
| grounded_generation.py thinking 修复后候选／安装源 | `7332414a9d57addd5092476999b59f8b62ee6c7f08035ed60522fbda6bedbfaf` | repair-v5/install-receipt.json |
| rerank.py 聚合修复前 | `cdaee354680ece45672d20a0afa099e4484feeaeaba5292cd09abdf01a1e7dca` | captured actual source |
| rerank.py 聚合修复后 | `3a5a55c80dfee6b24991794a67e95099405ec76abda7f8cae2905df676a3338a` | repair-v6/install-receipt.json |
| 新 context_delivery.py | `764349d0e4ad4cdfd94415e178994a4bd29b9fb427281485d32db4ea0c7b2ae8` | repair-v6/install-receipt.json |

[默认消费链路安装回执](../var/ecs-live60-20261112/default-fix-install-receipt.json)、[thinking 安装回执](../var/ecs-live60-20261112/repair-v5/install-receipt.json)、[聚合预算安装及回滚回执](../var/ecs-live60-20261112/repair-v6/install-receipt.json)、[实际 rerank 变更 diff](../var/ecs-live60-20261112/repair-v5/rerank.delivery-budget.diff)。v6 仅重启 API，health 200；本次回执未改 worker、Embedding／VLM、索引、Prompt、输出 Token 参数或设置 revision2。

本地实际修改：`backend/app/services/llm.py`、`grounded_generation.py`、新增 `context_delivery.py`、`parent_context.py`；新增对应 payload／delivery 测试，原 parent 测试仅把“预算不足就丢 seed”的期望改为本次明确要求的“保留 seed＋overrun”。本地当前 SHA：

- llm.py：`c4be9b0ffe25fb3b5996c7985248024e2b66fffbc95bd697fd5738b474a872f3`
- grounded_generation.py：`1ab8936cced0e2424db987f9fa06e5c4ae915becec4710571a33f82cc9646804`
- parent_context.py：`7743fbd7c92dd9c9bcf4928c33073d3c01cce7d397f8bec779ac109c03b30ccd`
- context_delivery.py：`764349d0e4ad4cdfd94415e178994a4bd29b9fb427281485d32db4ea0c7b2ae8`

本地 SDK openai3.40.0／langchain-openai1.7.3 与 ECS 1.109.1／1.1.5 不同；两边分别验证，未拿本地版本代替部署版本。httpx 两边均0.28.1。基础 HEAD 为 `7c7415f85a063e3f7645f676bd22f7c8f766a732`；本次修复尚未提交／未打 Tag，HEAD 不是本次工作区改动的发布 SHA。

## 5. 实际执行与结果

以下是实际执行的局部检查。v6 正式 60 轮运行与逐题 AI_REVIEW 已完成，结果另列；未使用不存在的 make target 冒充通过。

| 检查 | 实际命令与退出码 | 关键结果／日志 |
|---|---|---|
| thinking 实际序列化及受影响模块 | `python -m pytest backend/tests/test_qwen_thinking_payload.py backend/tests/test_llm_answer_limit.py backend/tests/test_provider_unknown.py backend/tests/test_grounded_candidate_snapshot.py backend/tests/test_grounded_streaming.py backend/tests/test_e12_model_integration.py -q --tb=short`；exit0 | 77 passed，9 个已有弃用警告；[日志](../var/ecs-live60-20261112/repair-diagnosis-local-affected-final.log) |
| delivery 最小测试 | `python -m pytest backend/tests/test_context_delivery_budget.py backend/tests/test_parent_context_compatibility.py -q --tb=short`；exit0 | 21 passed；[日志](../var/ecs-live60-20261112/repair-diagnosis-delivery-tests.log) |
| delivery 关联不变量 | `python -m pytest backend/tests/test_context_delivery_budget.py backend/tests/test_parent_context_compatibility.py backend/tests/test_parent_context_v2.py backend/tests/test_e11_context_delivery.py backend/tests/test_e20_context_delivery.py backend/tests/test_e21_context_selection.py -q --tb=short`；exit0 | 72 passed，328 个已有弃用警告；[日志](../var/ecs-live60-20261112/repair-diagnosis-delivery-affected-rerun.log) |
| wire／来源重放 | 捕获部署 SDK 实际 HTTP JSON 的 MockTransport；本地 helper＋捕获 serializer 的零调用重放；exit0 | wire11一致；50/50seed、1/1已知Gold来源quote；不能当作真实提供商／全题质量通过 |

上述 pytest 使用项目 `.venv/Scripts/python.exe`，`PYTHONPATH=D:/RAG knowledge/backend`。thinking 新测试最初因未隔离历史设置读取出现 7 failed／41 passed，修正测试 fixture 后48 passed，随后77 passed；原[失败日志](../var/ecs-live60-20261112/repair-diagnosis-local-tests.log)与[修正日志](../var/ecs-live60-20261112/repair-diagnosis-local-tests-rerun.log)均保留。delivery 扩展检查曾有71 passed／1 failed，原因是旧测试仍要求 mandatory seeds 在 allowance1 时全部消失；只更新该过时期望后72 passed，[原失败日志](../var/ecs-live60-20261112/repair-diagnosis-delivery-affected-final.log)保留。没有通过替换模型输出、改 Gold 或删除失败记录制造 PASS。

## 6. 内存与资源证据

[独立 memory-audit](../var/ecs-live60-20261112/memory-audit.md)及[机器可读快照](../var/ecs-live60-20261112/memory-audit.json)记录的是 **21:28–21:30 CST 的只读快照**，不能冒称当前 v6 时刻的内存：

- 主机总内存3,714,252KiB，可用1,171,688KiB（约1,144MiB）；无 Swap，PSI各平均值为0.00。
- Chroma cgroup 当前659,271,680B（约628.5MiB），memory.high为640MiB；历史累计high计数10,131,902，但max／oom／oom_kill均0。累计high不是此次模型超时的因果证明。
- API／index／parse／recovery均属正式服务。recovery的T状态进程仍是服务MainPID，不是已确认孤儿；另一个旧开发API估计PSS约167MiB只是有条件候选，未取得可安全停止的事实依据。
- 审计时已确认可安全回收0MiB、实际回收0MiB；没有停止进程、清页缓存或调整资源阈值。失败评测 unit 的MainPID0不能算成可回收活内存。

上述只读快照之后，经用户授权，执行代理核实旧开发 API 的独立 PID、工作目录、18201 端口及无活动开发连接，再仅对该 PID 发送 SIGTERM。公开回执确认进程结束、18201 不再监听、正式服务 PID 不变，API／Chroma health 200、Redis ping 正常。停止前 PSS 为 174,963,712B（约166.9MiB）；主机 MemAvailable 样本均值增加约147,117,397B（约140.3MiB），受到并行问答／缓存活动影响，不能当成完全归因的精确释放量。私有恢复材料仅留服务器且不镜像。见[开发 API 停止公开回执](../var/ecs-live60-20261112/repair-v6/dev18201-stop-public-receipt.json)。这项后续操作不改写早期只读审计“实际回收0MiB”的历史结论，也不是全机清理完成声明。

v6 的031在约29.060秒被本地资源护栏以 curl SIGTERM（exit -15）中止，采样 MemAvailable 为968,355,840B（约923.5MiB），低于1000MiB阈值。改写和检索已发生，但无回答、done 或提供商终态；因此保留 UNKNOWN，不算 Gold 错误或已知提供商故障，不重试补成功。后续会话保留真实 user-only 历史缺口。

本次复用现有虚拟环境、冻结题集、SQL／索引和已有来源；无新依赖、无完整语料／索引复制。本轮只保留必要代码候选、诊断请求／回执、原 UNKNOWN、逐题结果和日志。SQL seed50 私有实验输入并非新增入库语料。

## 7. 计时、Token、质量与当前遗留

HTTP200／SSE建立不代表回答成功；response headers、首个正文、finish、总调用时间应分别报告。50seed对照在headers返回后仍持续约6.31秒才finish，实测说明该`with_raw_response.create(stream=True)`没有把完整流先缓冲后才返回。原全输入诊断没有`receive_headers.complete`，不能把它说成已经收到正文但UI没显示。原失败无 usage，Token／费用均 UNKNOWN；已返回的 reasoning_tokens0仅适用于相应成功诊断，不能追溯填入旧 UNKNOWN。

正式 v6 已结束：60轮全部尝试，59轮完整非空回答、1轮资源护栏 UNKNOWN、0轮 NOT_RUN；主恢复运行 unit 最终 exit0 不会抹除031的中止记录。59个成功回答的观测总耗时平均19.530秒，P50为16.024秒、P95为45.337秒；它包括改写、检索与回答，按约1秒轮询观察，不能写成纯模型时延。首轮开始到最后 SSE 终态1859.674秒（约31.0分钟），含资源暂停恢复、调度与交错独立 recall，不能直接当连续吞吐测量。[最终执行统计](../var/ecs-live60-20261112/execution-summary-v6.json)。

59轮最终回答 SSE 报告 prompt_tokens=964,816、completion_tokens=12,136、total_tokens=976,952；59个已知 reasoning_tokens 均为0，031未知。这只覆盖已暴露的最终回答 usage，不含未暴露的改写／Embedding调用及 UNKNOWN，不是完整账单用量。逐题实际模型名及 finish_reason 未由 SSE 暴露，不能仅凭请求路由或内部 COMPLETED 推断。

完整60轮独立 AI_REVIEW 为 FULL38／PARTIAL0／FAIL21／UNKNOWN1；完整回答分母59，38/59约64.4%，全部请求分母60，38/60约63.3%，二者都不能外推为完整生产语料准确率。引用检查为PASS26／PARTIAL12／FAIL15／无需引用6／UNKNOWN1，来源忠实性为PASS41／PARTIAL11／FAIL7／UNKNOWN1；这些维度与 Gold 核心覆盖分别判断。诚实说明证据不足仍可能未覆盖 Gold，但不等于编造或拒答行为不当。独立审查允许合理同义表达与等价计算，另查错误数值、无来源额外事实、引用及追问上下文；不使用严格短语命中，亦不把冗余但有效的引用判错。质量判断为AI_REVIEW，未经过人工确认。[最终独立审查](../var/ecs-live60-20261112/review-v6-report.md)、[完整60轮账本](../var/ecs-live60-20261112/review-v6-results.json)。

外部单位／标题依赖未完整编码、23轮来源绑定UNKNOWN、未逐请求暴露的上游候选／完整provider wire、提供商内部排队／上下文行为仍是明确限制。不能仅凭返回阶段缺失 Gold 就归因到具体检索或交付函数，也不能因未核实绑定而自动把回答判错；单个source quote保留1/1不是答案质量证明。原V4／V5与诊断尝试仍各自保留，未混入最终60轮分数。本报告记录修复与实测完成，不宣称60题全部正确、人工验收通过或已打Tag。
