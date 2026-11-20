# 网页证据接入请求级 SourceRegistry 验收报告

日期：2026-11-15。项目 D:\RAG knowledge。分支 codex/bm25-stats-write-optimization-20261112，HEAD 7c7415f85a063e3f7645f676bd22f7c8f766a732。工作区原有大量未提交成果，已保留；本轮未提交、推送或切换正式服务。

## 结果

本地接线 PASS；ECS 隔离工程验证 PASS；真实 Tavily 搜索与云端模型质量 NOT_RUN。没有新增严格答案校验，没有修改检索参数、解析器、模型或 Gold。

网页来源保留 URL、标题、实际搜索正文片段、抓取时间、正文 SHA256。去重身份为 URL 加完全相同片段哈希；同 URL 不同片段以及同片段不同 URL 保留独立来源。沿用 RequestSourceRegistry 数字 source_id 和模型短别名 sN，不创建第二套持久化登记系统。本轮回答 [sN] 经现有 Registry 转回前端 [N]，未知标签保留并计数。

服务端完成的会话引用才可取得 CAPTURED_SEARCH_RESULT 信任状态。引用正文仍复用 ChatEvidenceBody，元数据仍复用 ChatTurnReference，无迁移/新表。SNAPSHOT_ONLY 明确表示已捕获的搜索片段，不是实时网页验证，也不是完整网页。浏览器历史导入不获得同等信任。

后续联网请求在明确启用联网时才读取相关历史网页快照；重新分配当前 source_id，不沿用旧编号。普通知识库路径默认不复用网页片段。历史回答中的数字引用和未解析 sN 标签均改为带历史轮次的标记，防止本轮同号冲突。

本次网页历史补充每次请求最多 3 条，且受已有正文预算约束；相关性为本地词法估计，明确引用上文及部分代词形式可选择最近历史网页轮次，记录启发式原因。不是语义理解的确定性保证。更早或未覆盖的复杂指代可能未选中，需保留该限制。Agent 知识库历史每次补充最多3条，不是整个回答所有工具轮次累计最多3条。

NO_RESULTS 可以在保留实际搜索状态的同时使用历史快照；FAILED、UNKNOWN、NOT_CONFIGURED 不被历史快照掩盖、不新增生成调用。成功搜索返回不可用片段且无历史证据时，NOT_RUN，不发送空证据生成请求。

## 修改文件

- backend/app/services/web_source_registry.py：网页快照身份、筛选、去重和请求级 Registry。
- backend/app/services/chat.py：仅联网分支接入 Registry、短别名及恢复诊断。
- backend/app/services/grounded_generation.py：证据携带 source_alias 时展示该别名；无别名的原有知识库编号保持不变。
- backend/app/services/conversation_context.py：受控网页历史复用、URL 送达身份、历史数字及短别名隔离。
- backend/app/services/conversation_runner.py：网页请求不强制要求知识库；按联网开关加载历史网页，送达回执身份包括 URL。
- backend/app/services/conversation_store.py：捕获快照信任、读回仍标 SNAPSHOT_ONLY。
- backend/app/api/conversations.py：无知识库的联网会话仍按选择解析模型绑定。
- backend/tests/test_web_source_registry.py、test_web_search_chat.py、test_server_conversation_store.py、test_server_conversation_api.py：定向工程断言与旧替身适配。

## 实际命令与退出码

1. 本地联合：
```powershell
& '.venv\Scripts\python.exe' -m pytest backend/tests/test_web_source_registry.py backend/tests/test_web_search_chat.py backend/tests/test_web_search.py backend/tests/test_server_conversation_store.py backend/tests/test_server_conversation_api.py backend/tests/test_agent_source_registry.py backend/tests/test_agent_source_projection.py -q --tb=short --junitxml=var/web-registry-20261115/final-local-junit.xml
```
退出0，146 passed，0失败/错误/跳过。发生于最终历史未解析别名补修之前。最后补修后再次执行存储专项53 passed，退出0，见 alias-final-junit.xml。两次范围重叠，不能相加。

2. 本地独立可移植探针 ecs_probe.py：真实会话 API/SSE、SQLite、产品请求构建，仅搜索及模型网络边界替身。普通与 Agent 各4请求，8用户请求、6次替身模型生成、0外部模型、0真实搜索。44项断言，退出0。只证明接线与来源身份，不评价真实模型答案质量。

3. ECS 隔离：
```bash
bash /opt/amrag/experiments/web-registry-3f9dfdc4/run.sh
```
退出0，SOURCE_HASHES_PASS，44项断言 PASS。相同8请求/6替身生成；HTTP200之外核对SSE终态、实际请求中的URL/正文/别名、历史读回、引用重新编号以及UNKNOWN不生成。

4. 最终历史别名补修在ECS单独核对实际文件SHA并运行 prepare_history，退出0，history_alias_neutralization=true。SHA a207069d04e37329f055d74ce98b780ee901e5e137dca0ee2f9f56529ce3f7eb。未重复8条网页流程，因为改动只影响历史对话投影，联网流程不发送该历史对话。

## 保留的失败与修复

独立探针首次失败：持久化读回把 verification_status 覆盖成 CAPTURED_SEARCH_RESULT。已改为 SNAPSHOT_ONLY，复验44项通过。独立审查另发现历史未知 [s99] 未中和，可能与以后本轮编号冲突，已补修并加回归。

ECS 首次哈希验证命令因 PowerShell/SSH 引号转义失败，退出1；发生于探针执行之前，保留为启动命令问题。改为上传 LF run.sh 后校验及运行退出0，没有重发未知模型请求。

## 证据与资源

本地：var/web-registry-20261115/，包含 final-local-tests.log、final-local-junit.xml、alias-final-tests.log、alias-final-junit.xml、probe-local.log（原失败）、probe-fixed.log、results-fixed/（本地回执）、ecs-evidence.tar.gz（ECS原始回执）、ecs-readback/、manifest.json、final-local-source-hashes.json。
ECS：/opt/amrag/experiments/web-registry-3f9dfdc4/，保留测试报告、SSE、实际替身SDK请求、合成SQLite及原始输出。生产DB、索引及Redis均未写入。

复用现有虚拟环境与已有隔离源码基础。没有安装依赖、创建容器或复制真实语料/数据库/Chroma。测试使用独立小型合成 SQLite。远端临时源码副本 2,418,760 字节已删除，保留增量源码归档及结果用于恢复。最终上下文补修文件单独保留。

ECS 测前/测后 MemAvailable 1761/1803 MiB；磁盘可用 12,354,342,912 / 12,353,306,624 字节；清理后观测12,353,957,888字节。共享主机前后变化不能归因于本测试，不报告其为精确开销。未测峰值。均高于800 MiB和8 GiB护栏。测试前后正式API PID同为1029438。本任务无正式重启命令；此前更早观测PID778457，不能宣称整天进程从未变化。

## 仍未验证

真实 Tavily 新搜索、真实云端问答质量、浏览器实际点击跳转、Langfuse Cloud回读均 NOT_RUN。Redis提示读失败后使用默认偏好，测试未验证Redis持久化。网页历史复用不是完整网页归档或重新抓取机制。启发式选择不保证复杂指代全覆盖。本次没有改变 Agent 全过程历史来源总量策略。
