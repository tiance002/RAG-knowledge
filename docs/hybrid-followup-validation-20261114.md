# 短期记忆状态、排名因果核验及真实 Rerank / Agent 隔离回归

日期：2026-11-14，结束核验23:14+08。项目 `D:\RAG knowledge`。本轮未替换或重启正式服务，未提交/推送，未修改Gold、模型、Prompt、检索权重、评分阈值或索引。

## 一、先回答三个问题

1. **短期记忆：本地已迁到后端，正式ECS尚未接入。** 浏览器和前端不是两个独立持久化后端；目标是后端数据库保存会话/回答/引用，前端提交会话ID与本次问题。正式库没有四张会话表，正式OpenAPI没有session/turn接口。旧浏览器历史仍只读保留，用户实际旧记录没有自动导入。
2. **排名：本轮确认Embedding输出波动能够单独导致同类换序，未发现当前配置下的并发排序缺陷。** 固定向量24次串/并行完全一致；复用两组不同向量再做4次纯串行，学者题第4/5名、第8/9名互换。原七对未保存当时向量，不能把每一对都追溯定责为同一个原因。
3. **真实Rerank通过；Agent并未全部通过。** 普通3题全部完成，Agent3题只有1题完成、2题在最终生成前失败。其中一题直接确认42,359字符超过32,000输入预算；不是输出4096截断、检索无结果、Rerank失败或Provider超时。

## 二、版本与隔离范围

- 本地分支 `codex/bm25-stats-write-optimization-20261112`，HEAD `7c7415f85a063e3f7645f676bd22f7c8f766a732`；原有大量dirty修改完整保留。
- 排名实验复用上一轮R7隔离源码，不把新源码差异混进原七对诊断；继续只读访问实际SQLite/Chroma、相同十库、Hybrid 0.7/0.3、目标Top10、Vector/BM25总深度500/500。
- Rerank/Agent使用本轮开始冻结的**当前本地源码**，共90份Python文件、1,187,242字节；只复制源码，不复制数据库、切片或向量。其清单是 `var/hybrid-followup-20261114/current-source/source-manifest.json`。
- 正式ECS在本轮开始时PID已是778457，不同于前轮728757；main、chat API、llm、chat四文件有其他工作更新，已单独记录，未覆盖。结束时正式81份核对范围内的文件与本轮开始一致，API仍778457，Chroma仍567489。
- 复用现有 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv`，未安装依赖或新建容器。独立API仅监听127.0.0.1:18334，禁止生产lifespan/schema初始化，源SQLite使用 `mode=ro`；检索写日志关闭。

## 三、短期记忆的实际状态

| 对象 | 本地实际实现/既有验证 | 正式ECS本轮只读核查 |
|---|---|---|
| 新会话权威 | `conversation_store.py`及四张会话表；后端history窗口、revision/CAS、幂等和来源校验 | 四表不存在，未部署 |
| 新前端提交 | `Chat.tsx`经conversationApi提交会话ID/当前问题/回执，后端决定历史和检索范围 | 8200 OpenAPI只有旧无状态completions和rewrite，没有sessions路由 |
| 旧浏览器记录 | `rag-workspace.chat-history.v1`只读备份，显式导入/导出，不清除旧记录 | 未访问用户日常profile，实际全量导入NOT_RUN |
| 取消/不同会话隔离 | 上轮229项+Chrome27已有验证；本地修复仍在 | 完整会话链尚未上线，不能把旧stateless请求当成后端记忆 |

原始现场回执 `followup-live-audit.json`；详细文件/函数见 [记忆现状核查](server-history-current-status-20261114.md)。本轮没有创建正式会话表，也没有把此前隔离聊天侧库并入正式库。

## 四、七对排名差异：受控因果实验

七个历史 `(qid,repetition)` 差异配对实际涉及六道不同题。直接复用冻结题目、作用域、模型绑定及索引；Gold不进入执行或向量注入逻辑。

### 4.1 固定向量后串/并行比较

每题先真实调用两次 `BAAI/bge-m3`，共12次，均返回1024维向量。私下冻结第一组向量，每题串行两次、并行两次，按SPPS/PSSP平衡顺序，共24次搜索。

在注入点、每个目标及每个Chroma分页都核对向量哈希；保存Chroma原始ID/距离、有效性过滤后的完整RRF两路输入、融合结果和最终搜索结果。不是只比较最终Top10数量。

**结果：24次全部完成，36个两两比较全部一致。** Chroma ID/距离、Vector/BM25输入、融合记录、最终ID/分数/内容哈希均一致。该配置下没有重现“同一向量因为串并行不同而换序”。

六题的重复Embedding中，两题向量不同：

| qid | 改变的分量 | 最大绝对差 | L2差 |
|---|---:|---:|---:|
| terra-ecs-b2-030 | 1024/1024 | 0.00122041 | 0.01083274 |
| terra-ecs-b1-008 | 1024/1024 | 0.00123186 | 0.01217523 |

另四题本次两次向量哈希一致。不能据此推断它们过去永不波动。

### 4.2 同为串行，只替换已保存的向量A/B

同一运行器新增受控回放参数，复用上述两题A/B，不再调用Embedding；新增4次纯串行搜索。总量为**12次排名实验Embedding、28次搜索**，不是再跑一次12次模型调用。原51份产物哈希不变，重复调用会被一次性回放声明阻止。

| qid | Vector/Chroma评分 | 最终有序ID | 解释 |
|---|---|---|---|
| b2-030 | 不同 | 相同，RRF分数和正文哈希相同 | 序列化哈希不同不等于排名改变 |
| b1-008 | 不同 | 相同10个ID，但4/5、8/9交换 | 无并行时也能重现换序 |

学者题的同两个块：

| 向量 | `9ce53761...` | `2aa46a92...` |
|---|---:|---:|
| A | 0.4029821，第4 | 0.4023987，第5 |
| B | 0.4023809，第5 | 0.4028946，第4 |

这与历史对照中的同两块相对次序翻转一致。**可确认机制是查询向量变化足以改变排序；不能据此声称原七对逐一都已还原。** 没有提供商内部日志，不能进一步断言是批处理、精度、模型副本或硬件原因；也没有为此关闭并行或更换模型。

独立复核：[排名因果报告](hybrid-ranking-causality-20261114.md)。原始目录 `followup/ranking-control`、`followup/ranking-serial-ab`。私有向量仅在获准实验目录，不输出数组到报告，不上传Git或Cloud。实验没有原子冻结SQLite/Chroma全快照，使用同一数据范围、source哈希和SQLite data_version守护，不能夸大为全环境证明。

## 五、真实Rerank和Agent回归

三个已有开发题：打印机两个设置位置、跨账号调试30分钟TTL、模型回归2.7→1.9评分。分别普通/Agent尝试，均Hybrid、相同明确十库，关闭本轮改写与联网，隔离路由OFF以固定Qwen；正式配置不改。

真实模型：`Qwen/Qwen3.5-4B`、`BAAI/bge-reranker-v2-m3`。实际所有Qwen请求均 `enable_thinking=false`。普通temperature0.7、max_tokens4096；Agent现有工具/最终调用temperature0.5、评分0.1，max_tokens没有在HTTP请求中显式提供，未擅自改成4096或推断其无限。

**Normal与Agent不是完全相同的检索池**：请求目标Top10，但Agent现有工具会过召回20，普通只送10项重排；两者重排输出均Top10，再经过原父子扩展。这是回归而不是用于归因Agent准确率提升的同配置A/B。

| 模式/题目 | 状态 | 秒 | 执行与答案复核 |
|---|---|---:|---|
| 普通/打印机 | COMPLETED | 12.258 | 核心正确；第二句引用[2]是扫描仪手册，不支持Print Spooler；[1]实际支持两项 |
| 普通/30分钟 | COMPLETED | 11.638 | 核心正确；明确区分另一个KMS场景的一小时默认值，未混作目标默认 |
| 普通/2.7→1.9 | COMPLETED | 10.689 | 核心端点及下降方向正确；补充把2+释为neutral/problematic不受引用支持，实际片段2=borderline |
| Agent/打印机 | COMPLETED | 12.454 | 真实工具→重排→评分0.85→最终回答；核心及[1]引用支持 |
| Agent/30分钟 | FAILED | 6.438 | 检索、重排、评分0.85成功；第二个agent_node发送前ValueError，无最终请求。原回执未捕获具体异常码/完整预算输入，不补造其精确字符数 |
| Agent/2.7→1.9 | FAILED | 6.020 | 直接捕获预算异常：42359>32000；没有最终回答请求 |

六次尝试全部进入执行分母：**完成4、失败2、UNKNOWN0；Agent完成1/3**。两次空答案不是“模型回答错”的质量样本，但不能从执行完成率分母排除。四个已输出答案的核心复核是代理依据Gold/引用作出的判断，不冒充负责人标签或校准Judge；没有按词面要求必须写“0.8”而拒绝2.7→1.9的等价核心表达。

Rerank真实HTTP共6次，均200并返回全部有效索引/分数：普通每次10项、Agent每次20项；服务端调用耗时约174～503ms。这不是原始检索分数回填，也未走chat打分fallback。三道单次小样本不用于宣称重排提升了正确率或系统P95。

业务回归Qwen真实网络请求10次、Rerank6次、Embedding6次；排名实验另有Embedding12次。没有重试UNKNOWN模型调用。终态仅保存部分usage，失败流程前两次调用的全量usage未保存，Rerank未提供usage，**总Token/金额UNKNOWN**，不能用成功回答usage代表所有调用。

逐题独立复核：[回答与引用审查](hybrid-followup-answer-review-20261114.md)。原始SSE/消息/真实网络请求与节点记录均在 `var/hybrid-followup-20261114/followup/`。Langfuse Cloud回读NOT_RUN，隔离遥测关闭；没有将这些本地回执冒充云端Trace。

## 六、只修复已确认的错误报告缺陷

确认输入预算拦截在SSE中只显示“模型请求未完成，请手动重试”，既不告诉用户输入过大，也不区分尚未发出的这次生成请求。

本轮产品只改两文件：

- `backend/app/services/llm.py`：新增 `GenerationInputBudgetExceeded(ValueError)`，`provider_error_payload()` 返回明确安全字段。
- `backend/app/services/grounded_generation.py::enforce_request_budget()`：原计数方法和32000阈值完全保留，仅抛出带实际计数的兼容子类。

修复后的同一失败输入返回：

```json
{
  "status": "FAILED",
  "code": "FINAL_REQUEST_OVER_CHAR_BUDGET",
  "stage": "generation_input_budget",
  "generation_request_sent": false,
  "retryable": false,
  "request_chars": 42359,
  "limit_chars": 32000
}
```

`generation_request_sent=false`仅指被拦截的这一模型调用，不代表此前工具选择、评分等都没有收费调用。现有Provider超时仍UNKNOWN，无隐式重发；不新增严格答案校验。

**这不是Agent大上下文问题的解决。** 在实际保存输入上复用既有 `CITATION_BODY_DEDUP_V1`，只去掉已在canonical context里逐字存在的citation.content：42359→33224，仍超限。没有提高上限、丢弃字段/事实、针对qid裁剪或默认启用旧候选来凑PASS。该样本后续需要单独的证据保留预算方案。

## 七、命令、退出码及恢复

| 实验 | 实际入口 | 结果 |
|---|---|---|
| 受控运行器本地替身合同 | pytest `backend/tests/test_hybrid_ranking_control.py` | 12 passed，exit0；见ranking-control-final.*与ranking-control-replay.*。不是24个不重复测试 |
| 排名固定向量 | 下方ECS命令 | systemd ExecMainStatus0；12Embedding/24搜索 |
| 排名A/B向量补充 | 同一CLI追加replay-from、新out | ExecMainStatus0；新增0Embedding/4搜索 |
| 普通真实回归 | check_hybrid_chat_delivery.py，indices0/15/13、mode chat、Rerank binding | job exit0，3/3完成 |
| Agent真实回归 | 相同参数mode agent | 首job exit2：1完成1失败，停止；独立执行尚未跑的index13，再失败，客户端shell记录exit1。没有复跑前一个已失败请求 |
| 错误报告修复定向测试 | 下方PowerShell命令 | 57 passed，exit0 |
| 保存的真实超限输入回放 | replay_generation_budget_receipt.py | 本地exit0、ECS旧venv exit0，0模型调用；不是新真实Agent生成 |

排名实验的实际ECS工作目录为 `/opt/amrag/experiments/hybrid-concurrency-20261114-r1`，复用既有解释器：

```bash
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python check_hybrid_ranking_control.py --cases development20.json --pairs-dir serialpair --backend-root /opt/amrag/experiments/hybrid-concurrency-20261114-r1/candidate/backend --db /opt/amrag/backend/local.db --env-file /opt/amrag/backend/.env --chroma-host 127.0.0.1 --chroma-port 8001 --chroma-workers 1 --out /opt/amrag/experiments/hybrid-concurrency-20261114-r1/followup/ranking-control --execute
```

补充回放使用版本化 `check_hybrid_ranking_control_replay.py`，其余参数相同，`--out .../followup/ranking-serial-ab --replay-from .../followup/ranking-control`。两次原始runner源码hash分别83e9a74d...、d80363e5...，旧版保留；运行器不应再次执行同一真实模型阶段来“重建”这份结果。

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH='backend;.'
$env:DATABASE_URL='sqlite:///:memory:'
$env:RAG_OBSERVABILITY_ENABLED='false'
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_generation_budget_error.py backend/tests/test_parent_context_v2.py backend/tests/test_provider_unknown.py backend/tests/test_agent_evidence_projection.py -q --disable-warnings
```

57项最终日志/XML：`var/hybrid-followup-20261114/budget-error-fix-tests/targeted-final-v2.*`。超限实际输入回放和错误JSON分别 `budget-error-local-replay.json`、`followup/budget-error-ecs-replay.json`。本轮没有重跑上轮229项或全量题集。

源码恢复以当前任务源码清单及 `error-fix/changes.patch` 为准；两文件修改前的完整字节在current-source归档，修改后hash在 `local-product-changes.json`。ECS仅实验源码的两文件应用错误报告补丁，正式源码未变；不需要正式服务回滚。私有请求/向量/原始结果只在var和获准ECS实验目录，不加入Git。

## 八、资源及停止状态

排名实验最低MemAvailable 1747378176字节（约1.63GiB）；补充回放最低2112708608字节；Agent guard未触发资源保护。云盘全程观测高于8GiB，结束12524154880字节（约11.66GiB）；Chroma MemoryHigh始终671088640字节，心跳200。

23:14核验：正式PID778457/567489不变；本轮隔离API已停止，普通/排名/资源守护进程结束，失败Agent unit保留exit2现场但PID0，无在跑的测试。结束IO/内存PSI avg10均0。

复用：源数据库/Chroma/切片/父子映射/venv/R7。新建：约1.2MB源码、探针、逐题回执和私有向量；远端followup目录约21.7MB，保留用于复核与恢复。新数据库/索引副本/容器均0，没有删除原始或历史评测数据。

最终状态：**排名受控验证PASS，真实Rerank PASS，Agent完整回归FAIL，错误报告修复PASS；正式记忆迁移NOT_RUN/未部署，原七对逐对因果归属UNKNOWN。** 下一项最值得处理的是Agent模型输入的证据/溯源元数据预算，而不是停用并行、加答案拒绝规则或继续抬高上限。
