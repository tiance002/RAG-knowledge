# E1.2 手动与自动采集对比

版本：ECS隔离依赖Langfuse5.0.4、OTel1.45.1、LangChain1.2.0/Core1.2.2/OpenAI1.1.5、LangGraph1.0.5。OpenInference instrumentation及semantic-conventions包未安装。本轮不升级依赖。

|项目|A 原生手动|B CallbackHandler + 手动|
|---|---|---|
|LangChain模型|在invoke_provider_model实际await边界generation|官方handler自动generation，关闭同调用旧手动generation|
|Tool|run_tools每个真实ainvoke建tool，保留call_id|官方handler采集真实工具；旧批量run_tools只当agent内部阶段|
|直接Provider SDK|手动generation可覆盖|CallbackHandler不能覆盖，仍需手动或专用Provider集成，二选一|
|手写检索/重排/扩展|原生retriever/span/chain|同A，callback不理解Jaccard和证据送达业务语义|
|父子关系|依赖实际OTel上下文|handler需在同client和活跃root内，config.callbacks透传|
|模型身份/usage|精确控制，但每种响应需适配|官方适配；项目自定义响应及隐藏推理需专项核验|
|重复计量风险|旧llm.response和llm.generate必须合并|禁止再叠OpenInference LangChain instrumentor或Provider双埋点|
|关闭观测|入口返回原业务函数，SDK失败fail-open|不注入callback；旧回退行为不得变更|

## 已执行的零模型探测

`var/e12-20261029/callback-probe.py` 经SSH stdin在既有ECS隔离Python执行，退出0。真实官方CallbackHandler + FakeListChatModel + @tool，在原生根chain中：两次模型invoke产生2 generation，两次tool.invoke产生2 tool，连同根共5节点，全部同Trace/同根；direct lambda不产生自动节点。外部模型调用0，Cloud写入0，InMemorySpanExporter本地收集。假模型无法解析真实model名称有警告，不能据此宣称真实ChatOpenAI模型身份与Token已通过。

这证明所安装依赖的基础callback与OTel上下文可兼容；没有运行完整LangGraph生产Agent、没有测试流式中断/UNKNOWN穿透及实际Provider回包，所以这些为NOT_RUN。普通官方SDK手动根/子span在控制探测中已执行；完整手动Generation覆盖同一业务函数属于第二阶段。

推荐可控混合B：LangChain/LangGraph模型和工具由官方handler唯一负责；直接AsyncOpenAI路径由原生手动Generation负责；手写RAG由手动操作负责。先在批准后的替身AB中验证一次请求没有双Generation/双usage，再接当前隔离入口。备选A：全部手动，变动较小且便于异常计量，但维护模型输入与tool序列化成本更高。

采用OpenInference语义作为标准字段：openinference.span.kind，llm.input_messages/output_messages及message.role/content/tool_call_id；retrieval.documents.N.document.id/content/score/metadata；reranker.input_documents/output_documents与query。ID/分数/文档位置不得混义；Langfuse原生input/output优先展示，避免为同一内容再写一份完整OI文本副本。OI instrumentation负责其配置和自动字段，OTel负责生命周期/上下文/processor；项目保留转换原因、source hash、score_kind、交付差异、证据状态等业务字段。

参考：[官方LangChain](https://langfuse.com/integrations/frameworks/langchain)、[LangGraph](https://langfuse.com/integrations/frameworks/langgraph)、[OpenInference语义](https://arize-ai.github.io/openinference/spec/semantic_conventions.html)。
