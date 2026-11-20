# b6-024 模型发送路径、完整请求头与连接复用审查

2026-11-14。本轮保留三类记录：32次位置完全平衡的主对照、24次仅供传输观察的采集、1次结果未完整落盘的UNKNOWN尝试。全部使用原b6问题及冻结证据；有效调用均9364输入tokens、64输出tokens，finish_reason=length，属于诊断部分输出，不当作正常完整回答或答案质量评测。

## 结论

发现两个确定的工程事实：ECS正式模型客户端缺少应用拥有的确定性关闭/跨请求复用，而本地已经存在尚未部署到ECS的provider_http连接池；相同HTTP请求会经不同的新连接到达供应商的多个入口IP。完整头核对没有发现并行组偷偷改变请求。位置完全平衡、实际socket核对后的主对照，没有复现固定的并行生成惩罚，反而是串行组出现了多次首包长尾。

这不是“模型波动”一句话的替代：发送路径、连接状态、入口IP、首包和读数据等待均有原始记录。但仍不能逆推旧那次2.053秒差距究竟来自哪个入口/连接/服务端阶段，因为旧记录缺少这些字段。不能宣布已证明所有旧差异都纯属波动，也不能说连接池能消除供应商长尾。

## 实际发送路径与部署差异

ECS正式路径：

```text
每个SSE请求的新ChatService
  → 请求内LLMService
  → generate_grounded_candidate
  → LLMService._get_client()
  → AsyncOpenAI.chat.completions.create
  → 自建httpx.AsyncClient(trust_env=False)
  → httpcore / AnyIO → DNS、TCP、TLS → 供应商入口
```

串并行指检索执行方式；两组生成均使用相同的异步模型客户端，未切换同步/异步生成实现。

实际源码：api/chat.py:75每请求创建ChatService；services/chat.py:69创建LLMService，199附近可另建自定义模型服务；llm.py:320–331在服务对象内缓存SDK客户端。API及服务没有负责关闭这些客户端的finally/aclose。SDK1.109.1只有在它自己创建AsyncHttpxClientWrapper时才有对应__del__清理，而项目传入自建httpx客户端，绕过该wrapper。

因此确认的是生命周期所有权缺口和缺少跨请求复用，不是已证明进程持续泄漏。本轮正式API观测17个FD、5个socket，不能从静态代码或单次FD样本宣称泄漏规模。

本地当前llm.py已使用ProviderBorrowingTransport；main.py生命周期中有managed_provider_http(preinitialize=True)。ECS不存在provider_http.py，实际仍走旧实现。本地能力不能当作线上已经部署。适配这部分应作为独立最小改动，不能整包覆盖同时改变其他问答行为；本轮未修改正式业务代码。

## HTTP层固定输入与完整头

所有有效诊断的实际正文SHA256：
`5ff63970c1cf6bfa6da34f25f1a135ddda3137b19f5fd77e4c444cf315efb572`。

请求统一为POST `https://api.siliconflow.cn/v1/chat/completions`，没有query参数。模型Qwen/Qwen3.5-4B，temperature0、max_tokens64、stream=true、include_usage=true、enable_thinking=false。温度固定不保证输出文本逐字相同，但实际输出token数均为64。

主对照中全部32次的完整有序请求头只有一个版本，HTTPX钩子记录与httpcore实际send_request_headers记录逐项相同。保留原始顺序和重复头结构；敏感值仅保存本次运行内HMAC，不落明文，不跨运行比较HMAC。

|头|实际值或核对方式|
|---|---|
|Host|api.siliconflow.cn|
|Accept-Encoding|gzip, deflate, zstd|
|Connection|keep-alive|
|Accept|application/json|
|Content-Type|application/json|
|User-Agent|AsyncOpenAI/Python 1.109.1|
|X-Stainless-Lang|python|
|X-Stainless-Package-Version|1.109.1|
|X-Stainless-OS|Linux|
|X-Stainless-Arch|x64|
|X-Stainless-Runtime|CPython|
|X-Stainless-Runtime-Version|3.12.3|
|Authorization|已脱敏，所有32次HMAC一致|
|X-Stainless-Async|async:asyncio|
|x-stainless-retry-count|0|
|x-stainless-read-timeout|120.0|
|Content-Length|37931|

没有额外Cookie、组织、项目、idempotency或并行标记头。代码虽生成内部幂等值，liveSDK的_idempotency_header为None；实际发送记录未出现该头。环境代理未使用（trust_env=False）。

全部响应HTTP200、HTTP/1.1、TLS1.3、TLS_AES_128_GCM_SHA256，证书指纹一致。完整响应头只有Date、Content-Type、Transfer-Encoding、Connection、X-Siliconcloud-Trace-Id、Cache-Control；未返回Server-Timing、排队或推理耗时。

供应商追踪ID32/32可用，弥补了此前只读SDK_request_id得到None的缺口。Date和traceID变化不是模式差异；Date不用于估算精确服务器耗时。

## 32次完全平衡的连接生命周期对照

八个四次调用区组，四种组合各两次：

```text
fresh(S,P), retained(P,S)
fresh(P,S), retained(S,P)
retained(S,P), fresh(P,S)
retained(P,S), fresh(S,P)
```

整组按seed20261118洗牌；每种生命周期×模式在每个绝对位置均出现两次。每个条件各八组相邻串并行配对，串先/并先各四组，条件先后也平衡。只允许一个请求执行；查询向量冻结，实际检索命中和父子引用逐次断言与冻结版本一致；生成起点距上次结束至少1秒。

|客户端策略|串行均值|并行均值|并行减串行|并行较慢配对|
|---|---:|---:|---:|---:|
|每次新建并关闭客户端|10.411s|2.912s|−7.499s|1/8|
|保留同一客户端|4.585s|4.097s|−0.488s|4/8|

配对bootstrap95%差值区间分别−14.571至−1.752秒、−1.424至+0.192秒。前者被三个串行长尾强烈影响，入口IP队列也不同，不能当作并行让模型本身提速的普遍结论。

实际复用由四元组和连接/TLS事件验证，不靠keep-alive头猜测。八个retained配对均使用同一条TCP连接，第二次调用均真正复用；串并两组各五次复用、三次新建。客户端保留期间可能因默认5秒闲置过期而重连，因此不是每次都热连接。TCP连接复用与ssl.session_reused（新握手的TLS会话恢复）是不同概念。

主对照到达六个入口IPv4地址：8.153.154.229、47.117.38.205、47.117.173.229、139.224.190.37、8.133.255.105、47.100.67.98。fresh两组的IP分布不相同；retained每对的连接与入口固定。DNS候选同时含IPv6，但实际连接均IPv4；因候选记录排序去重，不能据此还原原始解析顺序或推断具体HappyEyeballs回退。

## 网络耗时拆分

主对照fresh条件的平均测量：

|阶段|串行|并行|
|---|---:|---:|
|DNS|1.119ms|1.703ms|
|connect跨度（已包含DNS/地址选择）|12.441ms|12.328ms|
|TLS握手|23.006ms|21.328ms|
|HTTP异步写入返回|0.087ms|0.087ms|
|写入返回至响应头|8.024s|0.820s|
|首正文token至末正文token|2.330s|2.037s|

不能将DNS与connect相加当纯TCP耗时，也不能将异步write返回称为“对端已经收完”。原始TCP_INFO显示，新连接写入返回时仍可能有32157字节在本地发送队列，收到响应头时才观测到队列清空。已保存原始TCP_INFO及Linux结构定义，未伪造精确ACK完成时刻。

客户端可见TLS流read等待占主要时间；32次中读调用之间最大的本地处理间隙约3.023ms，写入返回后的事件循环最大延迟约2.070ms。没有检索任务与生成重叠；前后活动、队列、未完成future均为0。这些本地间隙解释不了十几至几十秒的等待。

TCP往返在响应头时约8.3～16.9ms，本机发出方向的重传计数增量均0，body结束时unacked均0。这不排除服务端到客户端的入站丢包/重传，也不覆盖供应商内部入口到模型之间的网络。

### 具体长尾

|序号及模式|总时长|写入返回到响应头|入口IP|供应商Trace ID|
|---|---:|---:|---|---|
|21 fresh串行|29.849s|27.344s|139.224.190.37|ti_u43djlvd0m88ao9xjd|
|18 fresh串行|20.304s|17.793s|47.117.173.229|ti_to2zk06owb6pwne5hq|
|31 fresh串行|14.692s|12.128s|8.133.255.105|ti_p07mau71ue0gnt1a0o|

最慢三次来自三个不同IP，不能简单列一个“坏IP”。retained同一条连接上也出现串行13.943s、并行10.460s，说明复用不会自动消除首包长尾。

这些证据把差异定位到了HTTP写入返回后等待入站数据的区间。区间仍可能含剩余上传、供应商入口/内部转发、排队/推理、返程网络，客户端不能进一步精确拆分。应使用provider-trace-ids.json与供应商内部记录关联，而不是凭Date或IP推断模型副本和排队时长。本任务未向外部人员发送消息。

## 采集过程、失败和版本边界

- 首轮传输采集在保存结果时遇到bytes域名无法JSON序列化。失败发生在模型请求尝试之后，缺少完整结果/usage，保留为UNKNOWN，不报0次请求、不当成功，也不纳入有效分母。因json.dumps在打开结果文件前失败，未生成部分JSON；保留canonical请求、源码和完整错误日志。
- 修正域名序列化后，24次采集成功。但审查发现其四次区组总为S/P/P/S，绝对位置与模式混淆。这24次仅用于头、socket、阶段观察，不用于因果均值结论；没有删除或替换样本。
- 另行预注册并执行32次位置完全平衡的主对照。上述统计只来自这32次。所有有效调用均64-token诊断，未混入正常完整回答。
- 主对照末尾progress.completed有旧常量24的记账错误，实际32份逐项文件、JSONL、schedule与result均为32。progress.original.json保留原文，仅修正进度元数据，没有改任何试验结果。
- 两个有效实验均退出0，独立审查复算原始头、chunks、token、四元组及区组统计一致。完整原始日志已归档；失败瞬时服务状态复位。
- 发送链路相关源码与前次快照一致；BM25文件与历史快照不同，本轮未修改它。每次实际命中和引用均通过冻结校验，不能声称本轮所有代码与历史评测版本完全相同。
- 采集窗口内正式API日志没有问答/改写POST启动记录。它不覆盖其他进程直接调用SDK或同账户外部流量，因此不能证明供应商负载完全隔离。

## 已确认的修改方向

优先适配本地已有的应用生命周期模型连接池到ECS，并明确客户端所有权和关闭路径；对非共享的短生命周期客户端至少显式关闭。需要兼容ECS较旧接口并回归生成、改写、取消和服务关闭。该修复能控制资源释放并减少重复建连，不能承诺解决十几秒的供应商首包长尾。

本轮没有替换正式代码、模型或配置，也未重启API。正式API本轮PID728757保持不变、并行开启、health=ok；它与更早报告的PID不同，不能混称整个历史过程都未重启。

## 证据和资源

ECS父目录：`/opt/amrag/evaluation/parent-child-parse-20261111/io-reconcile-20261112/`。
本地：`var/ecs-generation-causality-20261114/remote-evidence/`。

- `b6-transport-balanced-20261114`：32次主对照，完整脱敏头/trace/TCP_INFO/流read/chunks，analysis.json、derived-attempts.json、provider-trace-ids.json、live-source/、版本和收尾记录。
- `b6-transport-r1-20261114`：24次观察采集，独立保留统计及其位置混淆说明。
- `b6-transport-20261114`：UNKNOWN尝试与原始序列化错误。

复用原题、冻结证据、向量、SQLite、Chroma及虚拟环境，没有语料/数据库/索引副本。新增约32MiB必要证据，认证明文和TLS密钥均未保存。三份/tmp脚本核实无进程使用且与归档源码哈希一致后删除，测试进程均已退出，未停止正式服务。

收尾可用磁盘约11.77GiB（12634943488→12634177536 bytes，写入归档日志后；整盘变化不全部归因于本任务）；可用内存约2.13GiB。测试单元有500MiB MemoryHigh、750MiB MemoryMax、150%CPU上限，并在调用前检查1100MiB内存/9GiB磁盘保留线。没有把采样当整机连续峰值。
