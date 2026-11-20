# 统一分块部署与 Excel 替换后的最终复测

## 已交付

现有 PDF、DOCX、XLSX、CSV、TXT、Markdown 的提取结果进入同一个 Python Chunker。Excel 逐行 Chunk 保留为中间来源单元；先拼成完整文档，再选择普通分块或同模块的父子分块。异步任务将图片描述加入中间文档后再切块，不再先普通切分再独立切父子块。

保留原页/区域、工作表/行、CSV 逻辑记录、DOCX body_index 等来源映射。跨页块不假定只有一个页码。合成表头子块通过真实 legacy 算法验证父子关系，历史索引继续采用精确包含关系，不根据父 ID 猜测内容关联。

本地完整相关回归 127 passed；随后空白过滤/来源标记和行终止符修正的针对性回归分别 27、45、15 passed。各最终命令退出码 0，旧失败日志保留。实际编译 Go 对照普通 legacy 250 组/4772 块零差异；父子核心 250 组/771 父块/5205 子块零差异。Go 的空白输出在入库层过滤，原始 core 的逐块一致性不因此改变。

ECS 最终 13 个后端文件的哈希均核查一致，API health=200。代码回退文件与选择性的旧 Excel SQL/BM25/向量备份位于三个发布目录中；未复制完整项目、数据库或 Chroma 索引。最终有效目录：`/opt/amrag/evaluation/unified-chunker-20261116-verified`。

## 数据替换范围

仅对原 QA500 office50 KB `7cdb1ee4-ae8e-5cb6-b2ee-4f42a385cf00` 的 excel-1 至 excel-5 原文件重新解析并发布，保留 file_id/kb_id，不新增知识库。正式采用 parent 4096/0、child 384/48，首行表头关闭；原文第一行是报表标题而非字段名。

最终 874 条 SQL 切片：121 父块、734 子块、19 个无须父窗口的普通结果。753 个可检索结果的向量全部存在，751 条 BM25 记录与实际分词非空集合一致，另 2 条非空结果没有有效词项而跳过 BM25。空白切片为 0，悬空或跨文件父子关联为 0。

其他 **117409 条切片的 ID、正文、parent_id、chunk_type 有序哈希完全不变**：`abb9bce17267660b29c165add578a4062fd466858089ad48eed01202b3ea9644`；知识库 ID 列表也不变。正式解析/索引 worker 已恢复为本任务运行前的 inactive 状态，API 保持 active。

## 最终 25 题复测

选自上次 300 题中的全部空答/失败题：原 24 题字符预算超限、1 题 UNKNOWN。每题固定原问题、原检索配置和原实际对话历史，不给模型 Gold，网络搜索关闭。逐题串行，失败记录后继续，没有选替代题、没有重试或把中间轮次并入分母。

| 指标 | 最终结果 |
|---|---:|
| 原失败/空答题 | 25 |
| 本轮非空生成且正常完成 | 15（60%） |
| 本轮仍空答/报错 | 10（40%） |
| 剩余错误 | 全部 FINAL_REQUEST_OVER_CHAR_BUDGET |
| 所有 25 题平均请求耗时 | 18.49 秒 |
| 15 条非空完成输出平均耗时 | 22.14 秒 |
| 15 条非空完成输出耗时中位数 | 22.01 秒 |

10 个剩余请求分别为 44222、46025、33942、49100、39343、33303、34678、32308、34420、33587 字符，超过保留的 32000 字符上限；原始错误均标记 generation_request_sent=false。本任务没有通过调大预算取得结果。

**60% 是非空输出完成率，不是正确率。** 未完成全部 25 题的 Gold/证据逐项评分。抽查仍发现回答自相矛盾、混淆历史问题，以及有数据题的拒答。例如 `isolated-session6-v4-s1-t04` 一面说无法确定项目经理，一面列出正确姓名；`isolated-session9-v2-s1-t06` 和 `forward-session14-v7-s1-t08` 产生了无法计算的拒答。这些不能仅凭完整 done 帧算答对。原先请求失败/未生成答案，其耗时也不能当成成功答题的速度基线。

## 证据审计

最终后台服务退出码 0；25 个请求体与冻结请求逐项相等，25 次 HTTP 状态均 200、curl 退出码均 0；其中 10 次在 SSE 内返回预算错误。核对了逐题 result 与汇总一致、没有重复或缺题。原始 SSE、headers、stderr、实际 request、逐题结果共 125 个文件已下载 D 盘并保存哈希清单。

- `var/unified-chunker-ecs-20261116/verified-evidence/audited-summary.json`
- `var/unified-chunker-ecs-20261116/verified-evidence/per-question-results.csv`
- `var/unified-chunker-ecs-20261116/verified-evidence/replay-results.json`
- `var/unified-chunker-ecs-20261116/verified-evidence/raw/`
- `var/unified-chunker-ecs-20261116/verified-evidence/raw-evidence-manifest.json`
- `var/unified-chunker-ecs-20261116/verified-evidence/index-verification.json`

初始完整诊断轮（13/25 非空）和被中止的后续诊断轮（8 个已记账结果与已完成当前 HTTP 的 raw）分别留在原发布目录与 `-final` 目录，不属于最终统计。父子首次 Seq 差异的摘要日志保留，但初次详细 diff JSON 被脚本重跑覆盖；最终两端原始输出和零差异结果完整保留，不把重建内容伪称原始证据。

## 资源与未解决事项

最终轮 MemAvailable 最低 **1340.86 MiB**，高于 800 MiB 保留线；云盘可用空间最低 **8.79 GiB**。首次只读检查剩余约 9.0 GiB，结束剩余约 8.79 GiB；期间包括必要备份、索引发布、日志和并行系统写入，差值不能全归因于本任务文件。没有新增依赖环境或全量数据副本；新增/保留代码候选、13 文件级回退材料、5 文件选择性备份、必要测试原始证据。原始资料、冻结 Gold 和历史正式评测结果未改。

仍需后续处理：整体生成输入预算与证据选择、历史追问混淆及回答正确性/引用评分。其余文件本次按要求复用既有索引，不能宣称重新入库已对所有格式进行真实大语料验证。
