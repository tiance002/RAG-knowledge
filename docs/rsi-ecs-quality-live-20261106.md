# RSI ECS 真实闭环实验与候选复核报告

日期：2026-11-06。结论：**本轮真实实验与反馈归档已完成；保留基线，两个提示词候选均不晋级。** 没有切换正式服务、修改默认 Prompt、修改 Gold、提交或推送。

本报告取代本轮“等待额度”的执行状态，不覆盖此前原始报告和失败回执。负责人随后明确授权本轮调用不设次数/费用上限，并允许失败或 UNKNOWN 重试一次；实际实验仍限定为六题、每臂两次重复和两个候选，避免在同一小样本上无限调参。

## 1. 最终效果

以下是独立子代理逐条审查实际回答、实际模型请求、引用正文后，由开发代理统一审查口径，再通过原有判定函数重算的结果。身份是 **AI_REVIEW，不是负责人本人确认或人工金标**。

| 实验臂 | 完整正确 | 部分正确 | 错误 | UNKNOWN | 完整正确比例 | 平均响应耗时 | P50 | P95 | 输入 Token | 输出 Token | 总 Token |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 基线 | 11 | 1 | 0 | 0 | 91.7% | 10.21s | 5.90s | 25.82s | 53,948 | 4,133 | 58,081 |
| 候选①：简短但完整 | 10 | 1 | 1 | 0 | 83.3% | 5.32s | 5.04s | 10.05s | 54,668 | 1,523 | 56,191 |
| 候选②：区分要求与实例 | 11 | 0 | 1 | 0 | 91.7% | 10.97s | 6.76s | 31.53s | 54,896 | 3,834 | 58,730 |

每臂分母 **12=6题×2重复**，合计36条回答、不是36道独立题。核心正确性要求语义覆盖 Gold，不要求逐字相同；正确但不完整记 PARTIAL，同一关键时间或数值出现明确错误不能仅因同时包含正确片段而记 PASS。

候选①输出 Token 减少约 **63.2%**，总 Token 只减少约 **3.3%**，本批平均耗时减少约 **47.9%**。输入占比较高，输出减少不等于总成本同比减少；本轮也没有据此计算真实金额。

候选②输出 Token 减少约 **7.2%**，输入增加后总 Token 反而增加约 **1.1%**，本批平均耗时增加约 **7.4%**。它与基线的完整正确比例同为91.7%，但基线剩余一条是部分正确，候选②是一条明确错误，不能视为质量持平。

所有响应均完成，无空答案、跳过、截断或业务 UNKNOWN；36次业务调用均一次返回。P95采用nearest-rank，在12条样本下接近最大值，不能据此宣称系统整体P95或统计显著收益。各臂顺序执行、temperature=0.7，虽有两次重复，供应商负载和生成随机性仍可能影响耗时。

## 2. 两个假设分别发生了什么

候选均由实际运行的 **gpt-6.1-sol / medium** 返回，原始提案、请求及响应保留。开发代理没有预写候选再伪装成 Agent 输出。

### 候选①

唯一变化是在普通最终生成 Prompt 末尾增加一段英文指令：先回答所问问题，使用符合当前轮偏好的最短完整回答；保留单位、限定语和引用；直接查询或身份题不主动展开无关背景。

实际收益是输出变短、耗时下降；反证则是 `rsi-eval-01` 第1次回答：

> “该报告的时间背景为2016年选举后……下一个季度（即2016年10月至12月）……2016年12月31日前。”

项目报告的当前期是Q2（4月1日至6月30日），Gold原句仅为 `aiming for contract execution before end of next quarter`。回答将同一检索上下文中无关选举PDF的背景混入了项目报告，并给出错误的具体日期。它同时写出正确相对时间，也不能消除该时间字段内的矛盾。**核心 FAIL；候选不晋级。**

### 候选②

唯一变化是增加程序性问题的来源约束：按用户要求组织回答；区分一般要求、例子、上下文和具体项目字段，不将实例提升为通用要求；缺少依据时说明不确定性。它是生成提示，不是新增输出拦截器或严格答案校验。

它补回了季度报告第2次回答的“每个活跃自然季度”要点，使这题从基线 PARTIAL 变为 PASS。但 `rsi-eval-03` 第2次回答首句明确断言：

> “the exact total cost to date for the project is **$1.08 million**.”

冻结原文的三列是：

| Total Project Budget | Total Cost to Date for Project | Percentage of Work Completed to Date |
|---|---|---|
| $1.08M (Maine DOT joined) | 原文为空 | na |

`$1.08M` 是预算，不是累计成本；后文说“无法确认”没有撤回首句的错误。**核心 FAIL；候选不晋级。** 局部恢复和新回归同时存在，已分别归档，没有用平均分掩盖。

## 3. 检索、送达和来源是否相同

固定普通聊天模式、Qwen/Qwen3.5-4B、temperature0.7、输出上限4096、关闭思考；Hybrid向量/BM25权重0.7/0.3、Weighted RRF K=60、top_k10、context_window0、不启用Rerank、不改写。实际返回模型名均为Qwen/Qwen3.5-4B，业务回执reasoning_tokens均为0。

不是只改配置标签：每题实际执行原 EmbeddingService、Chroma、BM25和融合代码；检索回执记录 `weighted_rrf_then_minmax`，没有降级。Embedding使用已验证的非Pro BAAI/bge-m3真实返回向量，预热后按端点/模型/文本Hash复用；不是合成向量，也不是启用产品原先旁路的检索结果缓存。

对两候选共24对实际请求已逐对核对：

- 检索参数、知识库范围、候选顺序、送达证据正文全部一致：**24/24**。
- 两个候选的附加指令均确实出现在实际客户端请求中：**24/24**。
- 去掉唯一的候选附加段后，请求与对应基线相同：**24/24**。
- 各臂平均证据正文 **9,383.17字符 / 9,547.17 UTF-8字节**，没有因提示词候选缩短证据。
- 每臂相关块Hit@10为12/12，冻结相关块覆盖为16/16。该指标只针对这六题的80块开发语料和两块范围内的拒答题，不是历史200题qrel结论，也不证明答案字段一定被正确理解。
- 每条引用正文都能在实际送达上下文中找到。主错误首次体现在生成结果中的字段理解或跨资料关联，不能描述成检索把正确证据丢掉。

曾做过“完整原块文本是否逐字包含”的诊断；表格换行和空白渲染会使它不匹配，**没有将其作为召回率或失败判据**。页码、Word精确位置未提供的继续记未知，不拿冻结Gold定位冒充模型实际收到的定位。

真实模型请求通过隔离FastAPI HTTP/SSE路由的ASGITransport调用，在ECS执行；不是正式域名/Uvicorn网络层回归。记录的是完整响应耗时，没有将它当作底层模型TTFT。每问时间也不含一次性的隔离建库/向量预热；冷缓存性能本轮未测。

## 4. 引用和补充内容的审查口径

| 实验臂 | 全文忠实 PASS / PARTIAL / FAIL | 送达摘录引用支持 PASS / PARTIAL / FAIL |
|---|---|---|
| 基线 | 5 / 7 / 0 | 5 / 7 / 0 |
| 候选① | 4 / 7 / 1 | 4 / 8 / 0 |
| 候选② | 3 / 8 / 1 | 3 / 8 / 1 |

这些是回答级标签，不是逐主张Citation Precision/Recall。PARTIAL通常表示补充说明或部分引用映射不充分，不代表整条回答都捏造；长短、近义表达、数字分组和正常连接语不因此判事实错误。用户偏好的独立复核均为PASS，没有按字数设硬门槛。

Excel需特别区分：原件T07!C7:G7确实有美元格式，但本轮复用的旧冻结文本只有“In thousands”，没有美元格式。候选添加美元时，**原件事实仍可成立**，送达层面的依据却不完整；没有将它说成原文明确不支持美元。详见逐题JSON中的原始来源说明。

这也不证明当前Excel解析修复无效：本轮为保持Prompt对照一致，未重新解析并替换历史冻结文本。当前解析器的格式送达能力不能从这份旧快照反推。

三个独立审稿结果保留原样。开发代理统一口径时，将候选①两条“拒答结论正确、补充字段解释错误”的忠实度从FAIL调整为PARTIAL，与其他臂同类错误一致；未改核心标签、Gold或原始审稿文件。修订原因与前后标签见 `final-reviewed-scores.json`。两个候选仍因核心回归被拒绝。

## 5. 自动Judge为何不能直接批准候选

自动Judge对36条核心标签与独立审查有35条一致，但唯一的关键分歧使推荐出错：它在理由中承认候选①的2016/Q4/12月31日存在矛盾，却给核心PASS、忠实度PARTIAL。因此原自动流程暂时推荐候选①。

以本轮AI复核而非人工真值作为对照，将PARTIAL/FAIL视为“非完整正确”，自动Judge的TP=3、FP=0、FN=1，Precision=100%、Recall=75%。只有4条负例，且用于审查而非独立验证，**不能据此宣称Judge已校准可靠**。

没有改原始自动分数，也没有修改Gold迎合结果。独立复核输入现有 `validate_independent_review`、`write_reviewed_run` 和 `recommendation` 后，程序重新判定：**保留基线，eligible_children=0**。原自动建议、独立审稿、统一口径和新决定均分别保存。最终未将任何候选自动设为正式配置或开发父代。

经验更新也使用实际gpt-6.1-terra/xhigh调用，读取复核决定及两个真实反例，记录“核对日期/数值的来源语境及表头单元格范围，不能用更短、更快或平均分掩盖回归”。状态仍是 **UNVALIDATED_CANDIDATE**，不是模型自行授予validated。

## 6. 真实调用、重试和Token总账

| 类型 | 请求/派发记录 | 成功回执 | UNKNOWN | 已知输入Token | 已知输出Token |
|---|---:|---:|---:|---:|---:|
| RAG业务Qwen | 36 | 36 | 0 | 163,512 | 9,490 |
| Embedding准备 | 3 | 3 | 0 | 25,140 | 不适用 |
| RSI角色本地派发 | 27 | 24 | 3 | 183,653 | 124,717 |

角色输出中的 **117,955 reasoning Tokens已经包含在124,717中**，不能再相加。三个UNKNOWN没有usage回执；已知角色总量308,370是下界，不把缺失用量当0。真实账单金额 **UNKNOWN**，没有用Token或模型选择比例编造节省金额。

角色27次按原始终态回执逐一计数，排除了两份REUSED诊断和状态文件中重复嵌入的usage。24个成功角色响应ID均唯一，模型名与请求一致。主运行的model_calls=24不包含前一恢复运行的两次连接失败及最后一次复核后经验更新；全局为24+2+1=27。

角色模型按负责人指定：诊断/评分/综合评价gpt-6-luna max，分析gpt-6.1-sol high，实施gpt-6.1-sol medium，经验gpt-6.1-terra xhigh。

本轮重试：

1. Worker独占业务题重试；控制器独占角色重试。默认不重试，实验显式 `retry_once=true`。每次运行中最多两个attempt，原请求、响应、回执和用量分开保存。
2. 第一批真实Judge首次UNKNOWN，第二次RETURNED，旧UNKNOWN未覆盖。首次具体异常类型缺失，不能事后断言一定是模型或网络；后续代码补记安全的异常类名。
3. 两次分析派发在临时转发端口连接前报Connection refused，保留UNKNOWN。修复出口后在新恢复run中重新发起未完成分析；因此跨恢复run该操作有三次本地派发，前两次没有HTTP响应，不能只展示最后成功的一次。跨run总账已核对，并未声称存在全局幂等计费保护。
4. 仅截断Judge的已授权第二次尝试可以提高输出上限16384→32768，业务4096不变；本轮没有实际TRUNCATED角色回执。
5. 业务36次均一次完成；真实业务失败重试路径未故意制造故障，相关行为由离线定向测试验证。

`dispatch.json`的UNKNOWN是发送前不可变预留，最终结果看`receipt.json`，不能把所有dispatch预留误算成失败。

## 7. 执行过程、版本与退出码

产品HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`，分支codex/pdf-parser-migration；控制器工作树HEAD `aeb2c89bc66d724472621c581107093d7dd2ad32`。已有未提交改动全部保留。实际产品文件清单SHA256为 `5fb21e28401771c480908e40a8883c1229e6579bcf513ae0630c7891dad49eb5`，不能用HEAD冒充包含全部未提交源码。

可恢复源码包保存在本机 `var/rsi-quality-20261106/code-transfer-v3.zip`，控制器最终修复为 `controller-v5.zip`，后者只覆盖同一控制器的三个文件。它们不含数据库、索引或Key。冻结输入沿用原80块正文，清单原Hash为 `7b5524c8bfd1e8bbbef6298f6950fa4a6cc2462fe336789ad66639417e5e75d3`；本轮只筛选题目和迁移只读路径，没有改正文及Gold。源码包只在获准本地与实验目录保存，没有上传GitHub。

ECS目录 `/opt/amrag/experiments/rsi-quality-20261106`，复用原虚拟环境，不安装依赖。业务生成run_id：

- 基线：`ff2e44f533dd4021bb2c8a3a515dfc75`。
- 候选①：`93b84ec65cda4dfe8445b70d4e593c15`，候选ID `2f6fe822efa7faf36cb19f0d23410b8ee7388c632aa3eb86a68f04ff9f42e0b4`。
- 候选②：`c59da15324944cc4be93c77972d86933`，候选ID `80e552492d4cf4ec5241f3cc1c5847b38291afc301bf843d5e9ff036a996cd76`。
- 共同父代：`108ce37eaf089fb9b93be4a744ec6b01b52b1c3043d645eec642f1801a4c1d1a`。

首次真实流程因有效诊断JSON采用逐题 `diagnoses` 结构而被顶层字段校验拒绝，退出1；改为读取整体有效描述，不改模型原文。第一次恢复因临时出口断开，退出1。修复后复用哈希一致的基线、六批评分和完成诊断，最终恢复流程退出 **0**、`PILOT_COMPLETE`，耗时894.049秒；不是从头重跑。

最终实际执行命令：

```bash
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python \
  /opt/amrag/experiments/rsi-quality-20261106/controller/scripts/run_rsi_loop.py \
  --config /opt/amrag/experiments/rsi-quality-20261106/pilot.json \
  --out /opt/amrag/experiments/rsi-quality-20261106/runs/live-resumed-02 \
  --execute --approved-model-calls 46 \
  --resume-evaluated-baseline /opt/amrag/experiments/rsi-quality-20261106/runs/live-01/baseline \
  --reuse-completed-roles /opt/amrag/experiments/rsi-quality-20261106/runs/live-01
```

46是本次23个逻辑角色操作的保护性最大attempt数，不是负责人新授权的总额度。实际凭据由受保护环境提供，命令本身不含Key。临时凭据已清理，未来真实运行需先配置稳定出口与凭据，不应直接复制旧输出路径重跑。

### ECS定向验证

工作目录分别为实验product/controller，设置 `PYTHONPATH` 指向对应backend、controller/scripts和既有test-tools，`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`：

```bash
python -m pytest backend/tests/test_rsi_quality_worker.py -q --noconftest -p no:cacheprovider
# checks-03：退出0，36 passed。

python -m pytest backend/tests/test_rsi_quality_bounded.py \
  backend/tests/test_rsi_quality_loop.py backend/tests/test_rsi_quality_registry.py \
  backend/tests/test_rsi_quality_review.py -q --noconftest -p no:cacheprovider
# checks-06：退出0，74 passed。
```

最终两组不重叠，共110项。此前82项、102项和修复中的32/57项有重叠，不相加。这110项是替身Provider下的工程回归；真实36条云端回答及27份角色派发另行统计。完整命令、解释器、临时目录和退出码在checks目录JSON中。

## 8. 实际修改范围

产品侧只改 [Worker](<D:/RAG knowledge/scripts/rsi_quality_worker.py>) 和 [Worker测试](<D:/RAG knowledge/backend/tests/test_rsi_quality_worker.py>)。

控制器侧改 [质量循环](<E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_quality_loop.py>)、[执行适配器](<E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_quality_execute.py>)、[预算测试](<E:/codex_workspace/2026-11-03/task/rsi-work/backend/tests/test_rsi_quality_bounded.py>)、[循环测试](<E:/codex_workspace/2026-11-03/task/rsi-work/backend/tests/test_rsi_quality_loop.py>)。

内容包括：显式Hybrid与可信向量缓存、隔离设置准备、逐attempt回执与已知用量、一次重试和角色短暂退避、小批评分、源码清单核对、有效诊断JSON兼容、失败恢复及异常类型记录。没有第二套执行器，没有恢复严格答案校验，没有修改正式检索、Rerank、Agent阈值或解析器。

## 9. 网络、资源和未通过项

ECS可直连SiliconFlow；OpenAI原DNS异常，校正单连接DNS后TLS可验证但API403。没有更换角色模型，临时使用本机现有出口及SSH loopback转发。角色出口依赖本机，期间发生断连后增加自动重连。

**不能把本轮说成完全不依赖本机、从首次启动到结束无人介入。** 恢复后的流程自动完成分析、提案、两候选执行、评分与经验；之前有开发代理修复接线和出口。模型提出候选、程序产生建议，开发代理以用户视角审查并将反馈交回原有函数重算；没有手工编写本轮候选。

全部模型工作结束后，临时出口关闭、临时凭据删除。正式数据库/索引/历史评测未删改，没有新容器。Word、Excel、PDF原件只读复用，成功向量缓存保留86条，避免重复计算；清理本轮可重建的测试/臂索引和jieba缓存约184.8MiB。结束可用磁盘约16.87GiB，实验目录逻辑大小约69.7MiB，主要为原始问答、请求及审查证据；峰值资源开销未测，不编造监控数字。

| 验收项 | 状态 |
|---|---|
| 真实候选提案、继承、隔离执行、结果归档 | PASS |
| 36条实际回答和24对请求唯一变化核验 | PASS |
| 重试保留初始UNKNOWN；已完成结果哈希复用 | PASS，跨恢复运行派发总账单独审查 |
| 独立审查反馈进入程序重新判定 | PASS |
| 两个候选质量晋级 | FAIL，均有明确回归；基线保留 |
| 自动Judge独立决定候选晋级 | 不可靠，不准据本轮自动正式晋级 |
| 全程独立ECS、无需本机出口 | BLOCKED，需稳定获准出口 |
| Langfuse本轮Cloud回读 | NOT_RUN；Worker明确禁用Cloud，使用本地原始回执 |
| E1c正式数据/效果 | BLOCKED / NOT_RUN，未借本轮实验补造可信归属 |
| Agent、Memory、Skill、代码选择器的本轮真实质量对照 | NOT_RUN；本轮仅最终生成Prompt |
| 正式启用/部署/提交/推送 | NOT_RUN |

## 10. 下一步仅建议一个质量机制

优先研究**保持表头、列名和空单元格对应关系的上下文呈现**。本轮正确相关块均已召回，典型错误是将预算错当成本、把完工比例na映射到成本，而非缺少更多检索算法。A/B应只改变结构呈现，保留原数值、空值与引用；候选不得读取Gold或按QID硬编码。

实施前必须从已有原件通过产品解析入口取得可校验的表格/单元格结构并冻结新版本，不能根据本轮Gold猜补坐标。使用未参与这次设计的表格/空值例子作独立留出，再比较字段正确性与Token。当前两候选保留反例，不在同一六题上反复改Prompt直到分数好看，也不新增严格答案拦截器。

在更大规模自动RSI前，还需解决ECS独立模型出口，并校准Judge对同一核心字段内矛盾的严重程度。两者不是本轮质量候选的收益，不能写成已完成。

## 11. 审查材料

完整逐题问题、Gold、答案、引用正文、实际送达上下文、两类评分和请求Hash在 [逐题JSON](<D:/RAG knowledge/var/rsi-quality-20261106/questions-answers-evidence.json>)，使用分行缩进并共享正文Hash，避免把字段挤成一行。

[指标及Judge差异](<D:/RAG knowledge/var/rsi-quality-20261106/final-metrics.json>)；[统一口径修订记录](<D:/RAG knowledge/var/rsi-quality-20261106/final-reviewed-scores.json>)；[原始ECS回执镜像](<D:/RAG knowledge/var/rsi-quality-20261106/live-final-evidence>)。所有原始自动评分、独立审稿及首次失败继续保留。对外审核压缩包不含密钥或执行脚本，不自动上传GitHub。
