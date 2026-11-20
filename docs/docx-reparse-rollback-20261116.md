# DOCX 原库替换与回滚

本轮仅更新 QA500 office50 原知识库 `7cdb1ee4-ae8e-5cb6-b2ee-4f42a385cf00` 的五份 DOCX。原文件和文件 ID 保留；其它文件不重解析。使用已部署统一分块代码，开启父子块（4096/0、384/48）。legacy 参数是目标尺寸，原子内容可超出目标，不能作为硬字符上限。

ECS 工作目录：`/opt/amrag/evaluation/unified-docx-20261116`。D盘证据目录：`D:\RAG knowledge\var\unified-docx-20261116`。

## 回滚材料

- `docx-before.json.gz`：仅五个目标文件的原始 file/chunk/build/BM25 posting 数据及原文件哈希。
- `docx-vectors-before.json.gz`：仅五个目标文件原活动构建的向量、正文和元数据。
- `backup-manifest.json`：备份文件 SHA256；已下载至 D 盘并核对。
- `kb-before.json`、`stats-before.json`：知识库和 BM25 统计恢复依据。
- `rollback-dry-run.json`：实际 SQL 恢复函数在仅含目标数据的内存数据库执行两遍并检查通过。未对正式库执行回滚演练。

## 必要时回滚

先确保当前没有正在进行的解析、索引或其它数据写入；脚本会拒绝存在未终结任务或非目标范围变化的情况。不要为回滚擅自停止其它正式请求。

```bash
cd /opt/amrag/backend
UNIFIED_CHUNKER_RUN_DIR=/opt/amrag/evaluation/unified-docx-20261116 \
PYTHONPATH=/opt/amrag/backend \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python \
/opt/amrag/evaluation/unified-docx-20261116/ecs_docx_replace_and_replay.py rollback
```

脚本验证备份哈希，恢复并验证旧向量，使用一个 SQLite 事务恢复五个文件的 SQL/BM25/原构建、活动指针及统计。`desired_generation` 保持递增，避免旧通知恢复有效。回滚保留新向量供审计，活动构建过滤负责可见性；后续清理应按构建清单执行。原始任务回执与本轮结果不删除。

本轮没有更新正式后端代码，也没有重启 API。仅临时启动原本停止的解析和索引 worker，完成任务后恢复原状态。全局历史恢复 hold 保留，仅通知本轮目标任务。
