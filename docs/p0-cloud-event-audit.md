# Cloud Agent 事件只读复核（2026-10-28）

Trace `93746d54dbfed14ebda0b7c9316ba0e6` 此次重新读取到 13 条 `agent.actual_query`，与本地 `events.jsonl` 的 13 条 stage 记录逐一对应；此前摘要报告 11 条，目前差额已消失。13 是事件条数，不能当成 13 次独立查询：这些事件属于 7 个本地 span，6 个 span 各有两条事件。

使用 Langfuse CLI Observations v2 按 trace ID 查询，返回 HTTP 200，184 条 observation，`pages=1`、`fetchedItems=184`、`truncated=false`，无后续分页；结果为 `var/reports/p0-agent-accept-20261028/p0-agent-accept-20261028-200132-f9b3e74e/cloud-audit-readback.json`。仅请求 core、basic、time 字段，不含问题、输入输出或凭据。命令如下，凭据从 `backend/.env.langfuse.local` 注入当前进程，未打印：

```powershell
npx --yes langfuse-cli api observations list --trace-id 93746d54dbfed14ebda0b7c9316ba0e6 --fields core,basic,time --limit 1000 --all --max-items 10000 --json
```

该命令退出码 0；云端响应 HTTP 200。`--all` 的最大条数为 10000，但实际响应 184 条且明确未截断。官方文档说明该接口采用 cursor 分页，且部分旧版 SDK/导出器数据在 v2 接口可能延迟最多 15 分钟：[Langfuse Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)。

以下按本地 `span_id`、时间与云端 `parentObservationId`、`startTime` 匹配，时间误差均小于 1.1 毫秒。云端事件 ID 是独立 observation ID，不能直接与本地 span ID 比较。

| 本地 span / 云端父 ID | 云端事件 ID | 本地时间 UTC |
|---|---|---|
| 9f9ea336b2ade251 | e1cad4dd718cd249 | 12:02:00.757 |
| 844f85fcd8ddb5d6 | e7f61a5a63793d2b | 12:02:04.177 |
| 844f85fcd8ddb5d6 | 2fe2852883fc35c5 | 12:02:04.477 |
| 4d0719a76a4d8c2d | d75cef3f162662fe | 12:02:07.360 |
| 4d0719a76a4d8c2d | 63bb706e4b9421a2 | 12:02:07.366 |
| 4559ee3be6a4b13f | a1dec427d2359b10 | 12:02:11.420 |
| 4559ee3be6a4b13f | 78dea0dc1241dece | 12:02:11.426 |
| d373724d17e0dfaf | d508de01b00c6494 | 12:02:13.015 |
| d373724d17e0dfaf | c2cd8987d03b0d0c | 12:02:13.020 |
| fda4866c6c6523d7 | 3ff9345c6b86ea13 | 12:02:16.309 |
| fda4866c6c6523d7 | 23028f7dcc5ecfd4 | 12:02:16.315 |
| d04521b4b7ce32fd | cb87db0148af7ce7 | 12:02:30.337 |
| d04521b4b7ce32fd | 49f708a7906ead54 | 12:02:30.343 |

最后两条云端 `createdAt` 为 12:03:18.382 和 12:03:18.383 UTC，早于它们的前一对的创建时间为 12:02:55.806 UTC；云端异步写入或读取可见性延迟是合理解释，但旧摘要仅保留数量 11 和空 cursor，没有保留原始 11 条 ID 与查询执行时间。摘要文件修改时间也不能证明查询时间，因此无法确定哪两条当时缺失或证明具体根因。此前查询摘要亦称 cursor 为 null，但不能依据该摘要排除其他读取或过滤差异。此次只能确认当前记录已对齐，不能声称历史差异的根因已证实，也不能推广为所有 observation 均无丢失。没有调用模型或修改远端数据。
