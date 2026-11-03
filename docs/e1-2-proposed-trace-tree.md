# E1.2 新 Trace 树设计（待批准）

实际26节点及完整逐项迁移映射见 [节点审查](e1-2-observation-audit.md)。以下是操作边界设计，不是实际运行结果，也不规定所有请求节点数。

## 普通 RAG

```text
qa.request [chain] 问题 / 最终SSE答案、状态、变换、版本和Score锚点
 ├─ query.rewrite [generation，仅真实调用时]
 ├─ retrieval.search [retriever，每次查询独立retrieval_attempt_id]
 │   ├─ query.embed [embedding，仅真实请求；缓存命中为父属性]
 │   ├─ retrieval.vector [retriever，仅实际执行]
 │   └─ retrieval.bm25 [retriever，仅实际执行]
 │   metadata: Min-Max、weighted sum、fusion topk、去重、有序阶段和transitions
 ├─ retrieval.rerank [span，仅实际调用；batch子span按真实请求]
 ├─ context.build [chain，引用、邻块/父块分组、native补证、最终送达差异]
 └─ answer.generate [generation，真实Provider起止；权威消息、输出、usage、finish]
```

Rerank使用实际支持的span，metadata.operation=rerank；OpenInference的RERANKER语义不意味着Langfuse存在reranker类型。真实原文件读取可在context.build下保留span，纯not_applicable状态只作为属性。最终SSE生命周期属于根，原始答案与最终答案不同时保留transform_reason与两份哈希和可回读正文。

本次合成fulltext例映射为5个操作：根、retrieval.search、BM25、context.build和generation。是逐项选择的结果，Hybrid和Agent不会被硬压成5个。

## Agent RAG

```text
qa.request [chain]
 └─ agent.execute [agent]
     ├─ intent.classify [generation，仅LLM分类；规则分类为agent属性]
     ├─ agent.decide [generation，实际调用1]
     ├─ search_knowledge_base [tool，tool_call_id + retrieval_attempt_id]
     │   └─ retrieval.search [retriever，内部按普通RAG设计]
     ├─ agent.grade [generation，实际调用2；前2000字符裁剪必须可见]
     ├─ agent.decide [generation，实际调用3；真实历史再次发送仍记录]
     ├─ search_knowledge_base [tool，下一次真实调用]
     ├─ agent.grade [generation，若实际发生]
     └─ answer.generate [generation，若真实调用；不得与决策同一次调用重复]
```

generation名称由实际调用角色确定；一个decision响应本身就是最终回答时只保留该一次Generation，不另建生成节点。evaluator类型用于独立执行的评估任务；grade若调用LLM则使用generation，并加purpose=evaluation，避免同一次计量双记。工具输入输出、顺序、查询、真实参数和质量/重试决定保留。

## 失败和不可回答

|条件|实际观测设计|
|---|---|
|空库/NO_RESULTS且未调用LLM|retriever输出状态；根不足证据/正常结束；无虚构Generation或Token|
|有部分证据|保留实际候选与缺失，不将检索状态自动等同答案判定|
|PROVIDER_ERROR / UNKNOWN|已发请求有Generation并明确状态、request_id；usage未知为null；UNKNOWN不重发|
|空正文 / 截断|Generation分别记录empty和finish_reason；根记录实际SSE结束，HTTP200不等于正确|
|SSE取消/异常|根CANCELLED/FAILED/UNKNOWN按可观察事实；generation是否结束独立记录|
|Rerank不适用/回退|无调用不建请求span；实际失败有span，fallback_score_origin保持原分数来源|

兼容：保留trace_id/run_id/attempt_id/qid/dataset_version、answer_sha256、rubric和reviewer、source/index/code/prompt版本、retrieval_attempt_id及旧p0.*含义。新Trace新schema_version，旧Trace/Score不重写；对历史ObservationID的Score链接保持原ID，新运行按实际ID关联。Dataset和Experiment不得靠重放历史题建立。根最终SSE字段须与原Score输入答案一致。

最大风险是将阶段数组裁剪后无法解释证据首次丢失，或过滤仍有子节点的父Span形成悬空链。因此合并只对叶事件；真实父操作由新结构重新建链，不能简单should_export_span过滤旧父节点。


## Cloud实际旧树（本轮重新回读，26节点）

```text
qa.request [SPAN]
  qa.config [SPAN]
  retrieval.search [SPAN]
    retrieval.bm25 [SPAN]
    retrieval.file_lineage [SPAN]
    retrieval.before_dedup [SPAN]
    retrieval.after_dedup [SPAN]
    rag.candidate_transition [SPAN]
    rag.candidate_transition [SPAN]
    retrieval.metrics [SPAN]
    retrieval.outcome [SPAN]
  qa.citations [SPAN]
  context.delivery [SPAN]
  qa.source_verification [SPAN]
  grounded.request [SPAN]
  context.delivery [SPAN]
  provider.call [SPAN]
    provider.request_started [SPAN]
    synthetic.unauthorized [SPAN]
    llm.response [GENERATION]
    grounded.candidate [SPAN]
    grounded.response [SPAN]
    provider.request_finished [SPAN]
  grounded.request.sent [SPAN]
  qa.validation [SPAN]
  qa.answer [SPAN]
```
