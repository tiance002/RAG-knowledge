# Agent 综合重排与六父块隔离回归：原因定位

日期：2026-11-15。本轮只读诊断，外部模型新增调用 0，产品代码未修改，正式服务未变更。

## 证据范围与身份

复用 `var/agent-composite6-20261115/ecl6-de2c7183/` 中已保存的 60 题账本、45 道尝试、63 次 HTTP 回执、telemetry 和隔离源码 payload。通过 SSH 只读比对，四个关键部署源码与 manifest 哈希一致：agent_workflow.py、agent_evidence_projection.py、parent_context.py、rerank.py。诊断脚本退出码 0；不是新的质量测试或真实模型实验。

## 1. 16 个追问被替换为 UNKNOWN

证据：16 题均有请求历史，Provider 实际调用均正常返回 stop；没有知识库工具调用/工具结果，且 done.retrieval_status=null、answer_mode=UNKNOWN。例 legacy-session3-v5-s1-t02：实际请求 4 条消息（含 System、2 个 Human、1 个 Assistant），Provider 输出 116 字符、79 Token；客户端最终收到的是“知识库检索状态不确定，无法确认是否存在相关资料，请稍后重试”。原模型草稿只留了哈希，不宜推断草稿内容及正确性。

直接原因：agent_workflow.py::agent_node 使用 bind_tools 而未指定必调，模型可直接完成；response.tool_calls 为空即进入 _finalize_knowledge_answer。_guard_knowledge_answer 只认可当前执行的 ToolMessage/source_registry；历史对话正文即使有答案，也不等于已登记的原文证据。因此在没有工具调用/当前引用的情况下，模型输出被覆盖成固定 UNKNOWN。没有第二次补检索的执行分支。

现有 direct_first_retrieval 为实验开关，route_based_on_intent 也明确排除多轮历史请求，不能用它说明追问已保证检索。历史证据复用 merge_history_citations 放在 run_tools 内，只有执行工具才触发。本次旧 60 题回放发送 messages，没有使用新 conversation_id/history_evidence 的完整会话证据路径；因此本次只能确认旧消息入口失败，不能声称已经验证新的后端会话证据复用。

业务影响：时间换算、同日期比较等基于先前对话即可回答的问题也被同一“必须有当前证据”的收尾流程处理。安全保护避免无依据输出，但执行流程没有补齐证据或识别可合法使用的历史依据，造成过度拒答。不能把这 16 题统计成真实检索后 NO_RESULTS。

## 2. 18 题字符预算拒绝：单轮预算与全请求预算不一致

36 次失败尝试（每题 2 次），request_chars 范围 32,059–59,283，limit_chars 均 32,000。错误 generation_request_sent=false、retryable=false。Provider 没有在这些最终请求上返回截断，因为请求未发出。

源码：grounded_generation.py::enforce_request_budget 对 messages.model_dump 和 tools 的整份 JSON 计字符，包含 System、历史、工具协议、嵌套 JSON 转义和保留元数据。parent_context.py::expand_parent_context 的 24,000 字符预算主要用于单轮正文及可选扩展；它预留独立种子，必要种子超预算时保守保留，并不等价于完整请求一定小于 32,000。_source_context 还会添加 Document/source 标题；图片描述保留的语义元数据也占空间。

例 legacy-session3-v5-s1-t09：只有一轮、5 个父块，工具 Context 已达 24,149 字符，加上其余消息与协议后 request_chars=36,223。这证明单轮也会超限；降低父块数量或限制历史轮数不能独立保证不超限。

## 3. 多轮累计没有统一执行六父块和去重

18 个失败题首尝试中，8 题单轮检索，10 题两轮。messages 使用 LangGraph add_messages 累加；project_agent_evidence_messages 遍历保留每个 ToolMessage 的完整 Context，只删除该消息内重复的 citation.content，不合并不同工具轮次的重复正文，也不按整个最终请求重新选最多六个父块。

例 isolated-session6-v4-s1-t01：两轮各 6 个不同父块，累计 12 个，正文 13,798+14,809=28,607 字符，完整请求 43,214 字符后被拒绝。

例 forward-session14-v7-s1-t01：两轮同样的 4 个父块、相同 Context 哈希，每轮 12,978 字符；累计正文 25,956，完整请求 34,955 被拒绝。此题即使对父块 ID 去重计数只有 4，仍有重复正文。

必须修正上一份报告的范围：66 次单轮扩展最多六块、30 个已经成功发出的含工具证据请求最多六块，二者成立；但未成功发出的多轮累计请求没有统一保证最多六块。不能把前者当成整个 Agent 生命周期均满足最终请求六父块不变量。

## 4. 低相关评分触发重试，但它只看前 2,000 字符

agent_workflow.py::grade_documents 只把 rag_context[:2000] 发给评分模型；score<0.8 时添加一个 HumanMessage 提示改写，旧 ToolMessage 继续保留。isolated-session6-v4-s1-t01、forward-session14-v7-s1-t01 的评分为 0.0，触发追加检索，随后累计请求超限。

确认事实是评分视野受限且触发重试；不能在未核对每题后段 Gold 的情况下，断言所有 0.0 都误判。该机制会放大输入，是需要单独优化的取舍，不能直接降低冻结阈值凑通过。

## 5. Clinton 数值错误的首个证据损失点是重排 Top-20

legacy-session5-v3-s1-t02：Gold 587 / 4.9 percentage points；回答 540 / 4.6。正确 PDF 第45页父块为 4c7d37ce-400e-4072-877c-963727f1cd02。只读查询确认子块 bd6732bf-6fbf-4499-bac1-9e54bd31715c 含完整 Clinton 行，进入99个重排候选；模型分0.315883、模型排名42，检索分0.222496，综合分0.356279、排名54，未入Top-20。

因此不是PDF没解析出答案、不是索引没有该行，也不是父块六名额把它挤掉。重排截断先丢失正确证据，模型随后在没有对应表格行的情况下补出了错误数字。纯模型排名42同样进不了Top-20，不能把错误全部归于新增综合权重。

Query改写、模型工具查询、Reranker分别对该行低分的贡献尚不完全可分：本次部分记录仅保存查询哈希而非可回读正文，不能断言“误翻译导致”。

## 6. 引用缺失与泛来源列表

_source_registry / rag_result 保存工具送达的来源，不表示每个来源都被答案实际使用。agent_node 的 Prompt 要求行内[n]，但 _finalize_knowledge_answer 在检索SUCCESS、评分达标、正常结束时直接保留模型回答；严格答案校验已退出，不检查每条事实的引用支持性。

所以可以同时出现“返回了17条来源、答案没有行内引用”和“source_mapping_status=VERIFIED、事实仍错误”。VERIFIED只证明来源身份映射，不证明事实正确或引用支持。UTC+8题核心正确，但本轮引用片段没有支持它；Trump和总样本题的正确表格在来源列表中，正文缺行内指针；Clinton题既缺正确表格又答错数字。无需通过增加严格答案校验掩盖检索和生成问题。

## 7. 运行器的重试与提前停止

运行器对非完成题统一重试一次，没有尊重 FINAL_REQUEST_OVER_CHAR_BUDGET 的 retryable=false，所以确定性本地输入拒绝也再次执行了中间模型调用和检索。这是测试运行器缺陷，不等于产品偷偷重发UNKNOWN；应将Provider不明、可恢复网络错和确定性输入拒绝区别处理。

三段运行均遇到连续5题失败而退出5。15个后续追问因保护停止没有执行，不是已测错题。资源护栏未触发；这批直接错误由请求字符保护触发，不能归因于ECS内存不足。

## 修复顺序建议（本轮未实施）

1. 在模型请求边界统一管理当前轮多次工具证据：按可信来源/块身份去重，保留来源编号，实施整个请求六父块与字符预算，预留历史和协议空间。避免直接抹掉独立必要种子。
2. 补齐Agent未调用工具时的执行策略及可信历史证据接线；不直接放行没有原文证据的模型草稿。历史即可确定的计算和事实追问应有合法的证据复用路径。
3. 用已召回但Top-20丢失的Clinton子块作定位样例，固定相同候选比较查询/重排，之后决定策略，不立即调权重。
4. 修正测试运行器对不可重试错误的处理；行内引用用生成提示及真实请求回归改善，不重启严格句式校验。

本轮结论：原因定位已形成可核对证据；上述修复和效果验证 NOT_RUN。
