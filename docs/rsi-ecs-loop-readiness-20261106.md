# RSI ECS 质量闭环：接线修复与执行准备

2026-11-06。本轮状态：工程定向验收 **PASS**；真实候选效果 **NOT_RUN**；等待新的付费角色调用额度确认。未启用候选、未更新正式服务、未提交或推送。

## 1. 起点与复用

产品工作树 `D:\RAG knowledge`：分支 `codex/pdf-parser-migration`，HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`。控制器工作树 `E:\codex_workspace\2026-11-03\task\rsi-work`：HEAD `aeb2c89bc66d724472621c581107093d7dd2ad32`。两处均有大量既有未提交文件，全部保留；提交号不能单独代表此次源码，实际身份以文件清单与 SHA256 为准。

复用原入口：`run_rsi_loop.py` → `rsi_quality_loop.py` → registry/derive/materialize → `run_rsi_offline.py` → `rsi_quality_execute.py` → 产品 `rsi_quality_worker.py` → 实际 FastAPI HTTP/SSE 路由 → 模型客户端请求捕获。没有另建执行器、数据库、Agent 框架或调度平台。

ECS 独立实验目录：`/opt/amrag/experiments/rsi-quality-20261106`。复用现有 Python 3.12.3 虚拟环境 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv`；pytest 复用既有 test-tools。没有安装或升级依赖，没有复制生产数据库或生产索引。

## 2. 发现和修复

| 问题 | 处理 | 证据 |
|---|---|---|
| 旧 Worker 固定 Fulltext 并禁止向量检索，不能冒称 Hybrid | 同一 Worker 新增显式 Hybrid 配置，使用原 EmbeddingService、Chroma、BM25 和 Weighted RRF；旧 Fulltext 兼容保留 | 27 项 Worker 测试、零调用预检 |
| 最新运行设置表可能在隔离建表之后才注册 | 显式注册 PersistedSystemSettings，再建表、写入隔离运行快照；sys_llm 指向捕获代理，sys_embedding 指向本轮真实配置 | 隔离运行设置与实际请求测试 |
| 控制器只认识六候选、112 请求等旧固定规模 | 同一控制器支持显式 scopes、1–2 候选、1–2 次重复、1–8 题，并计算预算下限 | 55 项控制器/登记测试 |
| 源码身份要求 Git 工作区，Linux 增量代码目录不可用 | 显式 hash-bound 源码清单；核验完整文件集合及每个实际文件，不信任仅声称的哈希 | 清单变更、缺文件、额外文件等定向测试 |
| 实验说明仍声称 Agent 不消费 use_rewrite | 修正实验元数据与对应旧断言，未修改产品业务实现 | 控制器回归 |
| Worker 遇 UNKNOWN 后，上层可能继续 Judge 或新候选 | 每臂返回后检查退出码、执行状态、完整且唯一的计划记录；未完成则保存原始 Worker 并停止，不发后续请求 | 四类基线故障、子候选 UNKNOWN、完成的空/拒答分别验证 |
| ECS 暴露请求回执竞态：响应已返回，elapsed_seconds 尚未写入 | 捕获代理先完成并保存回执，再向产品转发响应；成功、503、UNKNOWN 三种情况均验证 | 首次失败日志及修复后顺序断言 |

这些检查约束执行身份、预算和回执，不按答案字符串决定业务正确性；没有恢复严格答案校验，也没有修改生产 Prompt、检索权重、阈值、解析器或 Gold。

## 3. ECS 实测与退出码

首次 Worker 验证：22 passed / 2 failed，退出 **1**。一项是测试命令遗漏产品 backend 的 PYTHONPATH；另一项是上述回执竞态。保留 `checks-01/worker.log`，没有把首次失败隐去。

修正命令和回执后，在新目录 `checks-02` 顺序执行：

```bash
PYTHONPATH=/opt/amrag/experiments/rsi-quality-20261106/product/backend:/opt/amrag/experiments/rsi-quality-20261106/controller/scripts:/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -m pytest \
backend/tests/test_rsi_quality_worker.py -q --noconftest -p no:cacheprovider \
--basetemp /opt/amrag/experiments/rsi-quality-20261106/checks-02/tmp-worker
```

工作目录为实验 `product`。**退出 0，27 passed，35.185 秒**。一项既有 Pydantic 弃用警告。

```bash
PYTHONPATH=/opt/amrag/experiments/rsi-quality-20261106/product/backend:/opt/amrag/experiments/rsi-quality-20261106/controller/scripts:/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -m pytest \
backend/tests/test_rsi_quality_bounded.py backend/tests/test_rsi_quality_loop.py \
backend/tests/test_rsi_quality_registry.py -q --noconftest -p no:cacheprovider \
--basetemp /opt/amrag/experiments/rsi-quality-20261106/checks-02/tmp-controller
```

工作目录为实验 `controller`。**退出 0，55 passed，0.658 秒**。两组共 82 个独立测试；这些是定向工程测试，外部 Provider 响应为替身，不等于真实模型质量通过。实际产品 Chroma、BM25、融合、设置解析与 HTTP/SSE 在对应测试中执行。

控制器和 Worker 最新零调用预检均退出 **0**。预检确认六题、三开发/三评测、两次重复、最多两个提示词候选、最多 36 个用户请求与 36 次业务 Provider 请求、最多 8 次角色调用。**本轮实际业务 Chat、Embedding、付费角色 API 调用均为 0。**

测试由 ECS 独立后台进程启动；本机 SSH 只读取状态，没有驱动每道题。机器可执行的命令、退出码和对应源码清单哈希见 `checks-02/results.json`。

## 4. 冻结小样本与待执行实验

| 分组 | QID | 要检查的事实 |
|---|---|---|
| 开发 | office50:031 | Alabama 108149.892，单位 in thousands；数值/单位对照 |
| 开发 | hotpotqa:5a729fe55542991f9a20c53b | Pieter van Musschenbroek；不增加无来源别名 |
| 开发 | m2-supp-current:quarterly-report-english-procedure-v1 | 每季度、每任务、完成比例、2–3句、进展及问题、无工作也列出 |
| 评测 | rsi-eval-01 | 下季度三项工作及目标时间；保留 aim/before end of next quarter |
| 评测 | rsi-eval-02 | TAC 审阅/纳入意见、RFI drafted/nearing release，不强化成已批准/已完成 |
| 评测 | rsi-eval-03 | 两块摘录没有 exact cost to date；总预算不等于实际花费 |

全部题目和 Gold 只筛选，没有改写。三道评测题不进入本轮提案 Agent；文档和这些问题均曾被历史实验查看，因此这是**开发用途的题目留出**，不是独立文档或全新题目泛化验证。

四份原件哈希在 ECS 全部核对一致：直接复用已有 Word/Excel/PDF，只补传约 53KB HotpotQA。80 个原冻结块不重新解析。为不可回答题保留单独的两块范围；隔离建库有 82 条记录（80+2），不等于 82 份资料。

固定 Qwen/Qwen3.5-4B、关闭思考、输出上限4096、temperature0.7；Hybrid .7/.3、top_k10、context_window0、不启用 Rerank、不改写。Embedding 沿用非 Pro BAAI/bge-m3，官方价格页目前标免费： https://siliconflow.cn/pricing 。不切到 Pro。Embedding 成功向量可按端点/模型/文本哈希复用，真实网络调用、缓存命中和供应商 usage 分开计数。

候选范围仅最终生成提示词附加段，最多两个兄弟候选，由实际提案 Agent 输出，不预写候选冒充 Agent 产物。候选尚未生成，因此不能给出当前候选的正确率、节省比例或延迟收益。

| 实验臂 | 当前状态 | 正确性/忠实性/引用 | Token/延迟/长度 |
|---|---|---|---|
| 最新源码 Hybrid 基线 | NOT_RUN | UNKNOWN | UNKNOWN |
| 提示词候选1 | NOT_RUN，尚未提案 | UNKNOWN | UNKNOWN |
| 提示词候选2 | NOT_RUN，尚未提案 | UNKNOWN | UNKNOWN |

旧 Fulltext 112 条回答可保留作历史观察，不能作为当前 Hybrid 的基线，也不能用它们推算本轮改善。

## 5. 一条预检命令与待授权执行

在 PowerShell 运行下列命令可重新执行**无模型调用预检**；输出目录使用新名称，预检不会覆盖已完成结果：

```powershell
$out = '/opt/amrag/experiments/rsi-quality-20261106/runs/check-' + [guid]::NewGuid().ToString('N')
ssh -i (Join-Path $env:USERPROFILE '.ssh\study-plan-dev.pem') root@120.55.115.162 "cd /opt/amrag/experiments/rsi-quality-20261106/controller && /opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python scripts/run_rsi_loop.py --config /opt/amrag/experiments/rsi-quality-20261106/pilot.json --out $out --preflight"
```

真实执行仍使用同一入口，改为 `--execute --approved-model-calls 8`；先由受保护配置向进程提供凭据、部署独立后台启动，再由控制器顺序执行。当前没有复制新凭据到实验目录，也没有以历史额度启动。

上一轮 12+2 次角色额度已用完。本轮已提交新申请：最多8次（诊断/分析/实施各1、三臂评分各1、综合评价1、经验1），单次输入≤128KiB、输出≤16384 Token，准确费用 UNKNOWN。未批准前不发出这些付费请求。UNKNOWN 不重发，预算或观测不足则保留回执并停止。上一轮 Luna 批量评分曾耗尽思考额度；本轮不能提前保证 Judge 会返回有效评分，若再次发生应保持 UNKNOWN，不能把响应成功当作判分成功。

执行后开发代理会按用户视角逐题复核：核心 Gold 覆盖、补充内容是否相关且有原文支持、引用是否支持主张；保留自动评分原值及独立 AI_REVIEW，不冒充负责人本人确认。再比较实测 Token、上下文字符/字节、P50/P95（注明小样本）和请求耗时。没有可信价格不估算真实账单。

## 6. 文件、资源与边界

本轮修改：D 的 `scripts/rsi_quality_worker.py`、`backend/tests/test_rsi_quality_worker.py`；E 的 `scripts/rsi_quality_loop.py`、`scripts/rsi_quality_execute.py`、`backend/tests/test_rsi_quality_bounded.py`、`backend/tests/test_rsi_quality_loop.py`。registry 和原入口保持复用。

源码v2清单 SHA256：`a5c8cbd32af9c65e14f9625a32b413c7306b659da82dd92dfa79239d9b6092cd`；完整清单在本地 `var/rsi-quality-20261106/source-manifest-v2.json`。传输源包80个文件，ECS回读逐一匹配；旧v1源包和首次失败日志保留可恢复。

原始证据：ECS 实验目录的 `checks-01`、`checks-02`、`controller-preflight-02.json`、`worker-preflight-02.json`、`resources-final.json`。本地镜像在 `D:\RAG knowledge\var\rsi-quality-20261106\ecs-evidence-final.zip`，不包含密钥、私有原文快照或生产库。

复用的环境版本：sqlmodel0.0.27、FastAPI0.115.5、httpx0.28.1、langchain-openai1.1.5、langgraph1.0.5、Chroma1.3.7、Langfuse5.0.4、redis7.1.0。pytest 来自既有 test-tools，未安装新依赖。

用后只删除本轮已退出的合成测试生成的 Chroma 和 jieba 缓存，保留请求、响应、失败与通过日志。清理约160.6MiB，ECS磁盘从实验前16.97GiB可用恢复到约16.96GiB；没有新容器、语料整库副本或生产数据删除。

Langfuse 在本 Worker 中仍明确关闭，本轮只有本地请求、SSE 和检索回执，**Cloud Trace 回读 NOT_RUN**。E1c 正式资料仍 **BLOCKED**、正式效果 **NOT_RUN**。没有真实模型候选比较、留出泛化或正式晋级结论；此次 82 项通过只覆盖工程断言。
