# E1.2 SDK 原生控制核验

2026-10-29，Langfuse5.0.4/OTel1.45.1实际ECS隔离环境；正式venv无Langfuse、OTel1.39.1。没有安装或升级依赖、修改环境文件、重启服务。官方文档与实际签名分别保存在var/e12-20261029/official-docs/和ecs-sdk-runtime.json。

|能力/官方出处|实际参数与支持|实际可用性|预期作用、风险和建议|
|---|---|---|---|
|[Sampling](https://langfuse.com/docs/observability/features/sampling)|sample_rate / LANGFUSE_SAMPLE_RATE，签名支持|显式默认TracerProvider + sample_rate=0仍导出root/child两个；显式TraceIdRatioBased(0)后0个，ratio1后2个|当前项目自传provider，不能仅改环境变量即宣称采样；批准后由唯一OTel provider控制。EVAL固定1；普通流量比例待实际基线后选。只报告已采集Trace完整率，不冒充全服务请求数|
|[Filtering](https://langfuse.com/docs/observability/sdk/advanced-features)|should_export_span、is_default_export_span；v5已删除blocked_instrumentation_scopes|本地exporter探测仅root保留/drop被滤，退出0|组合默认过滤和显式框架scope策略；仅过滤无关叶子，不能孤立子树。业务关键根/模型/检索/工具不滤。不是将旧事件改名为event省Units|
|[Masking](https://langfuse.com/docs/observability/features/masking)|mask_otel_spans、MaskOtelSpansResult/OtelSpanPatch；legacy mask|实测导出属性中的合成input标记被移除；签名及patch生效|推荐导出前属性脱敏兼容自动instrumentation，保留前置数据授权和隐藏推理过滤。旧mask只覆盖SDK API设值，不能保护全部第三方OTel属性。hook异常可能整批丢弃，要计失败，不静默验收|
|[Batching/SDK](https://langfuse.com/docs/observability/sdk/overview)|flush_at、flush_interval、flush、shutdown、otel_compression=gzip/none|参数存在；flush后InMemory exporter可读。真实压缩HTTP及网络请求次数NOT_RUN|可减少发送请求数，不减少Observation数量/Units。短进程退出flush；服务关闭shutdown；不要每个增量同步flush。队列容量与内存须AB测量|
|[Media](https://langfuse.com/docs/observability/features/multi-modality)|LangfuseMedia(content_bytes,content_type) / base64_data_uri / file_path|合成1×1 PNG的bytes和dataURI产生相同media_id；SDK代码取内容SHA派生ID；Cloud媒体上传NOT_RUN|用原生媒体引用代替多处Base64。相同ID不证明服务端跨Trace永久去重或仅上传一次。文本Chunk不是媒体，不能假设自动享有相同机制|

**媒体授权必须在SDK处理媒体之前完成。** 官方mask_otel_spans在media handling之后，不能只依赖它阻止未经授权的图片上传。使用预授权公开合成图片；未知权限不进入自动媒体处理。hook不可依赖请求局部ContextVar，manifest决定需随Span携带安全身份并可在后台验证；不得把密钥或隐藏思考写入遥测。

OpenInference：[配置规范](https://arize-ai.github.io/openinference/spec/configuration.html)中的HIDE_INPUTS/OUTPUTS、HIDE_INPUT_MESSAGES、HIDE_RETRIEVAL_DOCUMENTS、BASE64_IMAGE_MAX_LENGTH由OI instrumentation读取，当前包未安装，**不属于Langfuse SDK已生效的设置**。本轮只采用语义设计，不额外叠加自动instrumentor。检索正文诊断档按明确授权保留；Generation最终真实消息在允许情况下保存实际内容，未知授权使用可靠受控引用，不从候选重建。

OTel：[Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)、[exporters](https://opentelemetry.io/docs/languages/python/exporters/)。上下文管理器保证真实span结束，跨async任务传递同一Context，避免根分裂；Attributes用于结构数据，Span Events也必须先验证Langfuse消费可见性，不将OTel event当作已经支持的UI字段。filter在导出时不会自动修复父子树；BatchSpanProcessor和采样职责独立。

命令证据：controls-probe.py / sampling-explicit-provider.json，经SSH stdin在既有隔离Python执行，均退出0。首个采样探测与预期不符的原始结果保留，不将其报告为采样成功；修正provider配置后的单独探测有明确来源。Mask/filter/media只是隔离内存实验，未写Cloud，无付费模型。真实媒体/采样/压缩Cloud AB均留第二阶段。
