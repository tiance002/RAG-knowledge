# E1.2 主流开源 Trace 机制审查与优化设计报告

日期：2026-10-29。第一阶段设计提交，**尚未批准实施**。实际HEAD `722f000bd874d1fa17bfe94385612f045218f74e`，分支`codex/pdf-parser-migration`。本轮仅新增研究产物和报告；业务/观测/部署配置无修改，Cloud只读，无新模型请求、无历史UNKNOWN重发、未推送代码。

## 评审结论

推荐“官方CallbackHandler负责LangChain模型/工具 + 原生手动Observation负责直接Provider和手写RAG”的混合方案。首要修复真实调用与Generation生命周期分离，其次减少重复元数据和快照描述符。OpenInference提供标准语义，不再叠加另一套重复自动instrumentor；不新建平台。

|负责人需了解的问题|结论与证据边界|
|---|---|
|26节点如何处置|KEEP5、MERGE9、ATTRIBUTE11、FILTER1，完整26行表见节点审查；保留独立操作和错误阶段，不以数量作硬指标|
|到底重复了什么|Chunk引用6次、问题引用2次、答案引用2次；legacy prompt/candidate重复描述同一消息/响应。大多LOCAL_ONLY，不能称为全文多次上传|
|哪些重复必要|不同实际模型请求重复发送历史属于事实；根最终答案与Generation原始输出分别有诊断价值。同一次调用的多份prompt/candidate事件可以合并|
|新树|根chain→可选改写generation→retriever（真实Embedding/Vector/BM25）→可选rerank span→context chain→真实answer generation；Agent每次模型/工具独立身份|
|采集如何分工|callback自动处理LangChain；直接Provider手动；业务转换仅结构数据。禁止双Generation/双usage|
|预计收益|本合成例26→5节点；保守JSON离线投影36,651→29,396B（-19.8%）；不含Score的对象单位27→6，含S时为27+S→6+S；均为预测|
|诊断风险|省去未送达候选全文会限制原因复查；阶段数组不能丢顺序/原因/裁剪；父span不能粗暴过滤；最终Prompt不得重建|
|拟改模块|observability.py、llm.py、grounded_generation.py、agent_workflow.py、retrieval.py、rerank.py、retrieval_tool.py、chat.py及相关定向测试；尚未修改|
|兼容身份|旧p0.*、Score ID/observation关联、qid/run/attempt/dataset、reviewer/rubric/answer_hash和来源/索引版本保留；旧Trace不回填，新schema版本化|
|仍属未验证|完整LangGraph运行、真实模型usage、流式取消/UNKNOWN适配、媒体Cloud去重、实际压缩网络体积、真实开销、Agent数据量AB和UI阅读体验|

## 新旧结构对照

|旧记录方式|推荐操作归属|
|---|---|
|grounded.request + grounded.request.sent|answer.generate.input（最终发送前真实消息）|
|provider.call + llm.response + grounded.response + grounded.candidate|一个answer.generate，覆盖真实请求时段、原始输出与Token；旧例不是重复Generation而是分散|
|before/after + candidate_transition独立Span|retriever的有序candidate_stages与transitions，保留代表、Jaccard、阈值和去向|
|qa.citations + 两个context.delivery|context.build的选择/送达输入输出和变化记录|
|provider started/finished、配置/模式/来源不适用|对应操作的属性与明确结束状态|
|agent.*所有阶段机械agent类型|实际Agent容器、每调用generation、每工具tool；规则/决策结果为属性|

## 官方机制与实际核验

已读取用户指定Langfuse最佳实践、类型、Python SDK、advanced、sampling、masking、LangChain/LangGraph及媒体文档，OpenInference语义与配置、OTel Python生命周期和exporter。网络下载部分.md失败已保留，改用官方网页/Context7核对，不把失败记成功。

ECS隔离：Langfuse5.0.4、OTel1.45.1、LangChain1.2.0/Core1.2.2/OpenAI1.1.5、LangGraph1.0.5；正式venv未安装Langfuse，OTel1.39.1。没有全局升级。两种环境不能混称正式启用了新版采集。

1. Cloud API重新只读回读：26实际Observation，单根，身份/父子/输入输出/配置核对PASS；仅该fulltext替身场景。
2. 官方Callback内存探测：2个假模型调用→2 Generation，2工具→2 Tool，全部同根同Trace；直接lambda未被callback采集。存在假模型名称解析警告；真实模型身份/usage未验证。
3. should_export_span过滤和mask_otel_spans属性删除探测PASS；没有Cloud写入。
4. 采样首测发现custom TracerProvider下sample_rate=0仍导出2节点，不能报告参数已生效。显式OTel TraceIdRatioBased(0/1)分别导出0/2，支持在唯一provider控制采样的设计。
5. 合成PNG bytes/dataURI产生同media_id；Cloud上传去重NOT_RUN。媒体处理先于export mask，必须提前授权。

## 原始证据及命令

所有本轮命令退出0，以下警告/不符预期单独保留，不等同生产验收：

- `E:/RAG quention/.venv/Scripts/python.exe var/e11-20261029/readback_r7.py --trace-id 25d63f7688bf7dcd76229f6a825a0c79 --run-id synthetic-ffaf2c10795b44b8818bcd2e57441f31 --output-dir var/e12-20261029/cloud-readback`：PASS，HTTP200，26，errors=[]。原始API回复保存在限权目录。
- `Get-Content -Raw var/e12-20261029/runtime-probe.py | ssh -i <existing-key> root@<existing-ecs> 'PYTHONPATH=<existing SDK site>:<existing app site> <existing SDK python> -'`：退出0；ecs-sdk-runtime.json。实际完整路径见E1.1运行清单与本轮私有脚本。
- callback-probe.py / controls-probe.py 同样通过SSH stdin运行：退出0；callback-probe.json / controls-probe.json。采样首测不符合预期、假模型名称警告保留。
- 显式sampler的单独stdin探测退出0：sampling-explicit-provider.json；生产venv包检查退出0：ecs-production-packages.json。
- `E:/RAG quention/.venv/Scripts/python.exe var/e12-20261029/build-audit.py`：退出0，26项审查、KEEP5/MERGE9/ATTRIBUTE11/FILTER1；data-volume-baseline.json与projection-budget.json可复算。

未运行新业务测试套件、未执行E1.2改动后的HTTP/SSE或Cloud写入，因此这些NOT_RUN。不是用参数签名或pytest替代真实Cloud字段回读。

## 审批后的最小实施次序（未执行）

1. 在当前隔离入口统一真实调用Generation与callback唯一归属，保留故障账本及来源身份。
2. 合并叶事件为操作属性/阶段数组；修Rerank score_value和受控枚举可读性；保留original_content替换这一已知业务行为。
3. 相同候选、Prompt、隐私策略下替身AB：普通/Hybrid/Rerank、两轮Agent、UNKNOWN/空/截断/取消、证据丢失、媒体授权。检查结果和请求参数一致。
4. 少量授权合成Trace真实Cloud写入回读并尝试UI；评分沿用已确认身份，新旧运行不混用。统计实际对象单位/JSON/网络/开销后决定是否启用普通流量采样。

备选A为全部手动原生Observation。推荐混合B但完整Agent兼容尚未验证，若callback无法保留当前UNKNOWN终止语义，优先A并记录限制，不能让观测修改业务请求次数。

## 交付索引和审批关口

- [26节点逐项审查](e1-2-observation-audit.md)
- [普通/Agent/失败新树与迁移](e1-2-proposed-trace-tree.md)
- [自动与手动采集对比](e1-2-instrumentation-comparison.md)
- [SDK原生控制核验](e1-2-native-sdk-controls.md)
- [重复内容、数据量及Units预算](e1-2-data-volume-budget.md)

第一阶段审查和设计已交付，第二阶段未开始。依据任务原文“未获负责人批准，不进入第二阶段”，在此停止，等待负责人批准推荐混合方案或选择手动备选。Cloud UI阅读验证NOT_RUN、费用/开销未测不填0；不为补齐这些第二阶段指标修改代码或无限扩大研究。
