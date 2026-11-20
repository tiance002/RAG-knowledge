# ECS 父子上下文 PARTIAL 原因与修复（2026-11-14）

已修复并在正式 ECS 问答验证。原 PARTIAL 由下游旧扩展入口和重复证据耗尽预算造成，父子切块和持久化映射本身未损坏。

## 原因与影响

正式问答仍调用旧 rerank.expand_context：同父子块拼接后，又将真实父块当邻块加入，未调用已存在的真正父块扩展函数。父/子重复内容耗费预算，可能挤掉需要的上下文并影响回答完整性。

原请求命中 50 个 child，均具有有效父映射，对应 47 个 parent；全部子块都能在各自父块原文中找到。从原 SSE 重建命中集合进行只读重放，99 个来源 ID 与内容哈希与原请求完全相同。重放只使用辅助递减分数选组代表，不用于排名/得分评估。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 原始命中子块 | 50 | 完全相同的 50 个 |
| 对应真实父块 | 47 | 47 |
| 交付引用 | 99，父子混合重复 | 47，真实父块去重 |
| 来源正文字符 | 23,791 | 12,886 |
| 被裁剪扩展组 | 3 | 0 |
| expansion_delivery_status | PARTIAL | DELIVERED |

原题核心证据在旧请求仍被保留，不能据此推断其他历史回答都完整。此次修复后，同一 50 个命中所需的 47 个父块全部交付，无需放大字符预算。

## 修复范围

- rerank.py：统一调用真实父块扩展，按父 ID 去重，保留全部命中子块映射，相关性仍来自最佳子块。
- parent_context.py：先为全部独立命中子块预留空间；父块超限时回退完整子块，记录原因。
- context_builder.py：保留父块存储的来源哈希、页/表格定位和模型生成文本标记。
- chat.py、tools/retrieval_tool.py、api/recall.py：邻块窗口为 0 时仍处理 child→parent；召回分组显式携带真实父来源 ID。

复用本地已有正确实现，对 ECS 6 个文件制作保留线上其它差异的最小覆盖。MAX_PARENT_LENGTH=4000、总扩展预算缺省回退 24000 均未放大。没有重解析、重建索引或修改冻结 QA500/Gold。

## 验证

2026-11-14 14:41:49–14:41:54（北京时间），同一问题经正式 Nginx 80→API 8200 /api/v1/chat/completions 完成 normal/hybrid/TopK50 问答。真实命中集合与原请求一致；逐一检查 47 个父块内容和 SHA256 与 SQLite 完全一致，50 个子块均由对应父块完整覆盖，父子关系及文件范围核对通过。

完整 SSE：HTTP200、curl exit0、journal exit0、无 error；retrieval_status=SUCCESS、generation_status=COMPLETED、source_mapping_status=VERIFIED、expansion_delivery_status=DELIVERED、fallback_used=False、degraded=False。运行日志仍为 mode=parallel。

线上 recall 的 context_window=0/TopK3 另行通过：DELIVERED/SUCCESS，真实父 ID 和存储来源哈希可追溯。完整 Agent LLM 流程未单独运行；其真实检索工具及单库/多库召回入口已在 ECS 隔离环境使用真实合成 SQLite 测试。

本地相关回归 82 项、图片描述父子来源回归 15 项，共 97 项，0 failures/errors/skips。ECS 精确候选与实际依赖通过标准库 unittest 9 项，0 failures/errors/skips，禁止网络连接和非测试数据库连接。隔离测试的检索输入是替身，实际 SQL、父块选择、来源与入口调用真实执行；线上问答则使用真实服务与模型。

单次整答耗时 40.17→5.44 秒、prompt tokens 21610→11447 仅作为该样本记录，不作稳定性能提升结论。本次未开展 QA500 全量正确率或完整率评测。

## 发布、资源、失败记录与回退

三次空闲检查通过后只重启 API，PID 656054→669404，健康检查通过。document_chunks=117507、chunk_index=102229、file_documents=23787 前后不变；解析/索引 ready/unacked 均为 0。

复用现有服务、模型、SQLite、Chroma、知识库和冻结问题。新建仅包含小型源码覆盖、回退源码、测试/SSE/日志；没有数据库/索引/语料副本、容器或新虚拟环境。部署前磁盘可用 12802674688 字节，收尾 12805185536 字节（约 11.93 GiB）；空间波动不全归因于本任务。正式复测最低 MemAvailable 2.10 GiB。

已清理前两轮 staging 的重复 candidate/tests 源码目录，共 329535 逻辑字节。原始失败日志、清单、最终候选和回退源码保留。

原始失败分类及日志均保留：只读诊断脚本的字段名错误已修正；旧测试断言要求丢弃超预算种子，与当前完整种子保护合同冲突，已改成验证完整保留和超限标志；ECS 未装 pytest，改用现有标准库 unittest，未安装依赖。首次 78 pass/1 fail 和后续全通过的原始日志/XML分别保留，未覆盖或改写失败证据。

发布/测试/6 文件回退：/opt/amrag/evaluation/parent-child-parse-20261111/io-reconcile-20261112/parent-context-partial-fix-20261114-r3。
线上 SSE：/opt/amrag/evaluation/parent-child-parse-20261111/io-reconcile-20261112/parent-expansion-fixed-20261114T064149Z-7f216999。
本地完整证据：D:\RAG knowledge\var\ecs-expansion-partial-20261114；机器可读交付 completion.json。

回退前核对当前文件仍为 overlay-manifest 候选哈希，再恢复 deployment/backup 的 6 个源码文件并在空闲时重启 API，不涉及索引或数据库回退。

PARTIAL 状态保留：真实父块超过单父块/总预算时仍应如实报告扩展未完整交付，并保留完整命中子块；有限上下文不能保证所有潜在证据都被召回，也不能把 DELIVERED 当作答案正确率保证。
