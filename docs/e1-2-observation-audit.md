# E1.2 现有 Observation 逐项审查

日期：2026-10-29。只读设计阶段。HEAD `722f000bd874d1fa17bfe94385612f045218f74e`，分支 `codex/pdf-parser-migration`。工作区已有未跟踪运行产物；本轮不改业务、观测、部署或Cloud历史。progress.md、docs/adr/未发现，以根AGENTS和E1.1报告为准。

使用现有 CLI 实际重新回读 [r7b Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/25d63f7688bf7dcd76229f6a825a0c79)，退出0/HTTP200/26节点/校验errors=[]，单根、无缺失父节点。原始API响应及审计JSON只存在私有 `var/e12-20261029/cloud-readback/`，未提交。下表字节是每节点input/output/metadata的UTF-8紧凑排序JSON，不是OTLP、HTTP压缩字节或账单存储量。

25 SPAN、1 GENERATION；替身实际调用1、外部模型0，不能作真实模型性能样本。唯一Generation为llm.response，实际耗时在provider.call，存在语义分散，**本Trace没有重复Generation**。


|名称 / ID|当前类型 / Parent|Start → End (UTC)|标准I/O|字节|建议 → 去向|诊断理由|
|---|---|---|---|---:|---|---|
|qa.request / `5c9e13222a46b9ff`|SPAN / `None`|2026-10-29T12:36:30.923Z → 2026-10-29T12:36:33.546Z|True/True|4204|KEEP → qa.request / chain|真实HTTP/SSE生命周期、最终状态与评测锚点|
|qa.config / `dd6cada08ef6adaa`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.930Z → 2026-10-29T12:36:30.930Z|False/False|982|ATTRIBUTE → qa.request.effective_config|配置赋值，无独立操作|
|retrieval.search / `8fa8a1bdab806fba`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.931Z → 2026-10-29T12:36:30.944Z|True/True|2177|KEEP → retrieval.search / retriever|一轮实际检索生命周期|
|retrieval.bm25 / `f525ca8200f5b380`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.933Z → 2026-10-29T12:36:30.933Z|False/False|1237|KEEP → retrieval.bm25 / retriever|独立BM25步骤；以后在真实搜索边界计时，当前节点只是结果记录|
|retrieval.file_lineage / `9622b90482a1c8e3`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.937Z → 2026-10-29T12:36:30.937Z|False/False|951|ATTRIBUTE → retrieval.search.sources|来源版本关联|
|retrieval.before_dedup / `16e60a5d5c748808`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.939Z → 2026-10-29T12:36:30.939Z|False/False|1958|MERGE → retrieval.search.candidate_stages.before|保留顺序和阶段，避免独立结果事件|
|retrieval.after_dedup / `08bfe5bc8ad961c0`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.940Z → 2026-10-29T12:36:30.941Z|False/False|1958|MERGE → retrieval.search.candidate_stages.after|保留排序与成员，不重复正文|
|rag.candidate_transition / `75bc931e8cdb0805`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.940Z → 2026-10-29T12:36:30.940Z|True/True|1189|ATTRIBUTE → retrieval.search.transitions[]|实际删除原因与规则；属于检索内部动作|
|rag.candidate_transition / `f2079ff3dab2900f`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.940Z → 2026-10-29T12:36:30.940Z|True/True|1123|ATTRIBUTE → retrieval.search.transitions[]|实际删除原因与规则；属于检索内部动作|
|retrieval.metrics / `38004e51bfa30d45`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.941Z → 2026-10-29T12:36:30.941Z|False/False|1119|ATTRIBUTE → retrieval.search.effective_config/counts|配置、计量与耗时归属|
|retrieval.outcome / `f3f4238d97d2854b`|SPAN / `8fa8a1bdab806fba`|2026-10-29T12:36:30.941Z → 2026-10-29T12:36:30.941Z|False/False|826|ATTRIBUTE → retrieval.search.status|真实检索状态|
|qa.citations / `e29439b6187b675f`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.944Z → 2026-10-29T12:36:30.944Z|False/False|1943|MERGE → context.build.output.citations|引用成员与实际送达一起复盘|
|context.delivery / `5a57d894d885967c`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.945Z → 2026-10-29T12:36:30.945Z|False/False|1234|KEEP → context.build / chain|实际构建操作：选中、替换、排图与缺失原因|
|qa.source_verification / `747ea45830842e15`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.946Z → 2026-10-29T12:36:30.946Z|False/False|874|ATTRIBUTE → context.build.source_verification|本例不适用；真实原文件补证有独立IO时可单建span|
|grounded.request / `44e15dfe49e8190c`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.949Z → 2026-10-29T12:36:30.949Z|False/False|1647|MERGE → answer.generate.input|与最终发送消息相同的快照重复引用|
|context.delivery / `f620963c604a6f7c`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.951Z → 2026-10-29T12:36:30.951Z|False/True|1574|MERGE → context.build.output.delivered_evidence|最终证据身份与真实Generation输入哈希关联|
|provider.call / `511dfccbad6721dd`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.952Z → 2026-10-29T12:36:30.958Z|False/False|854|KEEP → answer.generate / generation|包围实际请求；在此统一输入/输出/usage/结束状态|
|grounded.request.sent / `8b725f36727610c1`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.952Z → 2026-10-29T12:36:30.952Z|True/False|1571|MERGE → answer.generate.input|发送前的权威真实输入，不能从检索拼接推测|
|provider.request_started / `e0fb94d688e15863`|SPAN / `511dfccbad6721dd`|2026-10-29T12:36:30.952Z → 2026-10-29T12:36:30.952Z|False/False|865|ATTRIBUTE → answer.generate.request|本地请求ID、provider ID及开始信息|
|synthetic.unauthorized / `3d491cc530431b84`|SPAN / `511dfccbad6721dd`|2026-10-29T12:36:30.955Z → 2026-10-29T12:36:30.955Z|False/True|1459|FILTER → 仅隔离隐私测试账本|测试专用哨兵不是生产业务操作|
|llm.response / `50f24613b5cffd8f`|GENERATION / `511dfccbad6721dd`|2026-10-29T12:36:30.955Z → 2026-10-29T12:36:30.955Z|False/False|835|MERGE → answer.generate.usage/finish_reason|当前唯一Generation是响应瞬时事件，无输入；合并到真实调用|
|grounded.candidate / `380229e07726d664`|SPAN / `511dfccbad6721dd`|2026-10-29T12:36:30.957Z → 2026-10-29T12:36:30.957Z|False/False|1672|MERGE → answer.generate.output_hash/ref|与原始响应快照相同，不重复保存|
|grounded.response / `91db0d956900cf7d`|SPAN / `511dfccbad6721dd`|2026-10-29T12:36:30.957Z → 2026-10-29T12:36:30.958Z|False/True|1557|MERGE → answer.generate.output|原始可见响应归属于本次调用|
|provider.request_finished / `4d32079aa6601cd5`|SPAN / `511dfccbad6721dd`|2026-10-29T12:36:30.958Z → 2026-10-29T12:36:30.958Z|False/False|917|ATTRIBUTE → answer.generate.status/duration|实际结束与失败/UNKNOWN/取消，不是第二次调用|
|qa.validation / `a52bc91b00ac832a`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.958Z → 2026-10-29T12:36:30.958Z|False/False|867|ATTRIBUTE → qa.request.validation_status|保留执行状态，不重启已停用校验|
|qa.answer / `e3148ebb782af949`|SPAN / `5c9e13222a46b9ff`|2026-10-29T12:36:30.958Z → 2026-10-29T12:36:30.958Z|False/False|1058|ATTRIBUTE → qa.request.answer_mode|回答模式|

推荐计数：{'KEEP': 5, 'ATTRIBUTE': 11, 'MERGE': 9, 'FILTER': 1}。KEEP表示保留操作和身份，允许改为正确原生类型，不表示保持旧事件生命周期。

元数据覆盖：根为运行/配置/代码/索引指纹及SSE结束；检索为候选ID/顺序/分数/来源与转换；上下文为替换、排图、native来源及送达哈希；生成相关为消息引用、request ID、模型、usage及finish_reason。逐节点完整metadata键清单保存在 observation-audit.json。正文大多LOCAL_ONLY；content_capture/ref及旧prompt/candidate快照描述符重复出现。未授权测试正文无Cloud明文。

独立耗时：qa.request、retrieval.search、provider.call包围业务调用；其余多数由event()临时创建Span，起止仅代表采集动作。不能用grounded.response或llm.response耗时当作LLM耗时。Evaluator目标：根负责最终质量；Generation负责模型输入输出；retriever负责证据召回；context.build负责实际送达。内部排序/配置不需要独立Evaluator目标，转结构化阶段数组仍保留首次丢失阶段。

源码：observability.py::span/event/observe/candidates/model_response；grounded_generation.py::generate_grounded_candidate；llm.py::provider_attempt。另审查chat.py::_chat_stream_normal、retrieval.py::_hybrid_search/deduplicate_results、rerank.py::batch_rerank/expand_context、agent_workflow.py::run_tools/grade_documents。Hybrid与Agent本轮无新Cloud运行，仅静态与既有替身证据。score_value当前统一取row.score，Rerank可能保留旧score；action/decision/next_action不在sanitize固定可读键内，属于第二阶段待修观测问题。


## 各节点内容、重复与主要元数据

去重只比较确认一致的快照ID，不从Hash推测明文；重复引用不等于重复全文。旧snapshot_id与新snapshot_ref在同一节点去重计数。完整keys和原始大小由私有审计JSON保留。

|节点|主要metadata字段|内容|与前节点重复的内容引用|
|---|---|---|---|
|qa.request / `5c9e13222a46b9ff`|answer_empty, answer_mode, generation_status, original_answer_sha256, chunk_count, final_answer_sha256 … (57 keys)|正文无；快照引用2种|本顺序首次出现或无正文引用|
|qa.config / `dd6cada08ef6adaa`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (27 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|retrieval.search / `8fa8a1bdab806fba`|retrieval_status, candidate_count_out, candidate_count_in, effective_search_mode, kb_scope, requested_parameters.log_retrieval … (30 keys)|正文无；快照引用1种|qa.request/5c9e13222a46b9ff|
|retrieval.bm25 / `f525ca8200f5b380`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (16 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|retrieval.file_lineage / `9622b90482a1c8e3`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (19 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|retrieval.before_dedup / `16e60a5d5c748808`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (16 keys)|正文无；快照引用1种|本顺序首次出现或无正文引用|
|retrieval.after_dedup / `08bfe5bc8ad961c0`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (16 keys)|正文无；快照引用1种|retrieval.before_dedup/16e60a5d5c748808|
|rag.candidate_transition / `75bc931e8cdb0805`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (23 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|rag.candidate_transition / `f2079ff3dab2900f`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (22 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|retrieval.metrics / `38004e51bfa30d45`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (29 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|retrieval.outcome / `f3f4238d97d2854b`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (19 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|qa.citations / `e29439b6187b675f`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (16 keys)|正文无；快照引用1种|retrieval.before_dedup/16e60a5d5c748808|
|context.delivery / `5a57d894d885967c`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (21 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|qa.source_verification / `747ea45830842e15`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (20 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|grounded.request / `44e15dfe49e8190c`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (35 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|context.delivery / `f620963c604a6f7c`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (17 keys)|正文无；快照引用1种|本顺序首次出现或无正文引用|
|provider.call / `511dfccbad6721dd`|event_id, trace_schema_version, stage_attempt_id, dataset_version, qid, attempt_id … (19 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|grounded.request.sent / `8b725f36727610c1`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (18 keys)|正文无；快照引用1种|本顺序首次出现或无正文引用|
|provider.request_started / `e0fb94d688e15863`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (19 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|synthetic.unauthorized / `3d491cc530431b84`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (15 keys)|正文无；快照引用1种|本顺序首次出现或无正文引用|
|llm.response / `50f24613b5cffd8f`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (19 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|grounded.candidate / `380229e07726d664`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (33 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|grounded.response / `91db0d956900cf7d`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (17 keys)|正文无；快照引用1种|qa.request/5c9e13222a46b9ff|
|provider.request_finished / `4d32079aa6601cd5`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (21 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|qa.validation / `a52bc91b00ac832a`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (17 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
|qa.answer / `e3148ebb782af949`|event_id, stage_attempt_id, trace_schema_version, dataset_version, qid, attempt_id … (21 keys)|正文无；快照引用0种|本顺序首次出现或无正文引用|
