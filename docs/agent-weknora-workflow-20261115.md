# Agent 历史证据、按需读取与累计上下文改进

日期：2026-11-15。状态：本地定向验证通过，ECS 隔离 60 题运行中。正式服务未替换。

基线：同一批60题、Hybrid、Qwen/Qwen3.5-4B、BAAI/bge-reranker-v2-m3、综合分0.6/0.3/0.1。与上一轮ecl6 Agent结果比较的是整体工作流改动，不是单因素归因。

## 改动

- agent_workflow.py：利用服务端当前有效且范围核验过的历史原文，不将历史答案变成原文；没有工具调用也没有证据的首份草稿转入明确的工作流检索，不直接替换为UNKNOWN。
- retrieval_tool.py：兼容默认自动扩展，Agent显式关闭，只返回命中子块；普通问答不改变原扩展路径。
- read_knowledge_context.py：模型以source_id申请读取周边；后台解析为真实切片，核对知识库、文件删除/替换状态及ACTIVE构建成员，限制邻块窗口。
- agent_context_budget.py：统一处理每轮模型请求里的累计证据与预算，跨工具结果重复来源只保留一份，完整来源按预算选取，保持工具协议对与来源编号；最多6个实际父块，种子不冒充父块计数；记录省略来源及PARTIAL。
- agent_evidence_projection.py：仅适配新读取工具名称，沿用原引用及身份精简协议。
- chat.py：透传context_delivery回执，便于看到本轮实际送达来源和省略。

没有新增严格答案校验。没有更改Rerank权重、阈值、模型或Gold。

## 本地验证

Agent关联11文件162 passed，退出0；搜索/读取真实临时SQL专项23 passed，退出0。主代理独立联合87 passed，退出0，另历史存储/投影69 passed，退出0。套件有重合，不相加为独立测试数。原始三条旧参数断言失败已保留，按新expand_evidence=False合同修订，未修改Gold。

独立代码审查发现历史复用继承last_score=0.0问题，已修复为None且NOT_RUN，补测真实初始化状态，不伪造已评分。

## ECS隔离

目录/运行ID：/opt/amrag/experiments/aw-dc7d9ab9。端口127.0.0.1:18342。新源码只以补丁覆盖此前已验证的隔离源码，数据库sqlite只读，Chroma复用，无数据/索引副本。生产API PID778457未改变。部署前可用磁盘约12GiB、MemAvailable2253320kB，保护线8GiB/800MiB。

启动前Bash检查发现CRLF换行，已改LF，发生于API/模型请求启动前。源码编译、冻结题集SHA核对与健康检查通过。逐题数据输出在隔离目录results，本地归档位置var/agent-weknora-20261115/aw-dc7d9ab9。

当前真实模型质量、引用支持及60题完成度：待原始回执复判，不使用本地替身结果宣称真实质量收益。


## 请求级来源 Registry 补充

用户随后批准整理模型身份与引用。新增 agent_source_registry.py，以现有source_registry/source_id为唯一权威，生成s1/s2短别名；模型输出的已注册且实际送达的[s1]还原为前端既有[1]。不分配第二套ID，不把映射成功当作事实核验。来源注册本地还补入明确可得的构建版本与来源Hash，已知版本不同不合并。

模型视图删已知后台requested_chunk_id/history_turn_id/origin_reference_id/source_content_hash，保留原文及必要定位；读取工具接受短别名并由服务端查回真实来源。最终别名编码后的完整请求纳入32000字符预算，旧工具协议对、原始回执和后台来源不改写。

13文件联合198passed退出0（含新增模型实际HTTP替身请求与别名往返测试），版本身份专项86passed退出0；套件重叠不相加。测试日志见var/agent-source-registry-20261115-final-tests.log及对应JUnit，版本测试见var/agent-source-version-20261115-tests.log。

最终隔离运行awr-af1fd9f6、端口18343，沿用原60题/模型/Hybrid/Rerank配置。中间版本aw-dc7d9ab9因用户批准Registry后被替代并停止，保留6题回执、UNKNOWN和scope-revision-stop.json，不与最终运行混算。awr的来源版本去重键补充仍以manifest中的实际源码为准，本地后续补丁只在独立服务端历史验证中使用。

服务端历史补充验证排在60题后串行执行：/opt/amrag/experiments/server-history-registry-20261115，端口18344，使用同批0/1题，经实际会话API只传会话ID+本次问题，独立4表小型SQLite仅保存本次会话与引用，知识库/索引继续只读复用。不将旧messages回放冒充完整后端历史链路。

网页来源现状（只读核对）：Tavily结果保留URL/title/content，WEB_SEARCH_GROUNDED独立链按列表编号生成[1]等引用，尚未接入本次Agent来源Registry；也没有历史网页证据复用。此次60题web_search_enabled=false，网页来源测试NOT_RUN，未修改该联网路径。
