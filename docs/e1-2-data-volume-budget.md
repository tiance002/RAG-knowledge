# E1.2 数据量、Units与可复盘性基线

基线仅是r7b固定合成fulltext请求，1文件1块、无Rerank/改写，不能代表真实长文、Hybrid或Agent平均值。2026-10-29重新Cloud只读回读，模型调用身份由原始E1.1运行清单与当前事件交叉核对。序列化规则是每节点input/output/metadata紧凑排序UTF-8 JSON之和；不包含Cloud返回的全部列，不等于SDK OTLP、实际压缩网络字节或存储量。

|指标|r7b实测 / 解释|
|---|---|
|observations_per_trace|26（25SPAN+1GENERATION）|
|generations_per_trace|1，但在响应事件上而非实际调用生命周期|
|model_calls_actual|替身1；外部模型0|
|duplicate_generation_count|0，不为证明优化而虚构重复|
|serialized_content_bytes|36,651 B（35.79 KiB）|
|candidate_payload_bytes|6,936 B；本例BM25、before/after、qa.citations四节点metadata合计。包含候选所在节点的非正文元数据，不等于纯文档正文大小|
|generation_payload_bytes|835 B；当前Generation缺真实消息/响应正文，不能误解为LLM请求很小|
|repeated_content_bytes|标准input/output及metadata中text/content正文直接上传0 B（LOCAL_ONLY）；重复引用下限448 B，见下文。整份OTel属性内容未捕获，不宣称全网0冗余|
|trace_units|官方对象计数公式：1 trace + 26 observations + S scores = 27+S。本轮未查该Trace所有Score，S=UNKNOWN；未查账单，不宣称实扣27|
|complete_trace_rate|这1条已采集请求父子/身份完整1/1；不表示新原生语义完整，当前Generation缺I/O与正确生命周期|
|diagnosis_coverage|需预先固定7类：检索漏证据、去重误删、rerank回退、扩展丢失、生成错误、截断、UNKNOWN。可复盘测试分别在E1.1材料中，但不是同条件7题测量，比例NOT_AVAILABLE|
|observability_overhead|当前请求开启/关闭同条件资源/时间样本NOT_RUN，不能用替身耗时宣称性能收益|

## 重复的具体归属

只统计明确snapshot_ref字段，不重复统计同一描述符里的sha256/snapshot_id别名：13次引用、6个不同快照。源Chunk引用6次，问题引用2次，答案引用2次，完整模型消息引用1次，最终证据组引用1次，未授权测试引用1次。重复引用7×64 ASCII=448 B下限，不包含JSON键/外层元数据。

legacy prompt/candidate描述符还包含相同正文SHA与本地引用：grounded.request与grounded.request.sent指向同一最终消息；grounded.candidate与grounded.response以及根输出关联同一回答。它们主要重复描述信息，**不是重复上传消息全文**。本例Cloud Query=问题两个引用（根/检索），消息内问题保存在私有快照，不算Cloud明文。候选前/后/引用多处重复列表，是可优化的结构冗余。

本例未上传图片，重复Base64=0；Agent历史重复量没有本轮Cloud样本，UNKNOWN。若第二次真实模型调用确实再次发送历史，每次Generation必须记录本次真实消息，这是必要重复；同一次调用另外建立history/request/sent多个全文副本属于遥测冗余。

## 数据归属

根：问题、最终SSE、版本；Retriever：Query、候选身份/位置/各分数/阶段顺序；Reranker：实际送入的裁剪版本及返回；Context：Group成员、替换/排图/预算/送达原因；Generation：每次真实消息/响应/usage/finish；Score：Gold关联、reviewer/rubric、answer_hash和独立质量。

默认候选只记录ID、分数、位置、有限预览（建议上限256字符，设计值），公开授权EVAL/DEBUG可保留详细内容；未保存完整候选会损失非送达内容的理解错误分析，必须标明capture状态。完整Generation输入不能用Retriever内容推测补齐。去除重复引用可利用每Trace候选表和稳定内容哈希，但平台没有已验证的普通文本原生跨节点去重，不额外建独立数据库。

## 设计预测，尚非AB结果

由26节点逐项映射得到5个业务操作，预计节点26→5（-80.8%）；Score数量不变，条件相同且S=0时对象单位27→6（-77.8%）。实际S未知，通式收益21/(27+S)，这些是计数预测不是账单。

保守离线重组把各旧阶段正文/I/O/元数据保留在5个操作下，仅去除重复运行身份键及测试fixture：36,651→29,396 B（-19.8%）。实现与算法保存在私有build-audit.py/projection-budget.json；这是JSON结构预算，未发SDK、不含新的Span字段、不能当作网络/存储实测节省。候选正文授权改为Cloud_INLINE后，字节可能增加；不能拿增加可读性后的体积与旧全LOCAL_ONLY直接作优化AB。

Agent典型场景按实际模型次数M、工具次数T、检索轮R、额外向量/Embedding/Rerank请求E计数，建议根+agent容器+M个Generation+T个Tool+R个Retriever+E个独立操作+实际Context操作；每次调用身份不压缩。无同版本AgentCloud基线，旧/新节点数、字节、重复Generation和Units均UNKNOWN；不得从普通RAG26节点套算Agent。第二阶段需固定两轮Agent替身和同输入/候选/隐私策略后再量化。

计量来源：[官方Billable Units](https://langfuse.com/docs/administration/billable-units)，Units=Trace+Observation+Score；Batching减少网络请求，不直接减少对象单位。账户实际账单和Usage Management未读取。
