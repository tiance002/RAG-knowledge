# 单机文档任务

上传会保存文件。点击解析、自动入库、确认分块，或修改已发布分块时，API 返回 SQLite 任务 ID。一次自动入库会依次执行解析、向量计算和索引发布。知识库详情的「处理任务」可查看状态、尝试、心跳、下次重试和当前生效构建。

## 单机启动

所有进程使用同一虚拟环境、同一个 `backend/.env` 和相同的工作目录 `backend`。生产环境可将数据库、上传、图片、任务暂存和 Chroma 持久化目录配置为绝对路径。使用已运行的专用 Redis，或启动绑定本机回环地址的 Redis 实例。设置 `REDIS_URL`、`CHROMA_HOST=127.0.0.1`、`CHROMA_PORT=8001`。确认 8001 用于 Chroma，8000 用于 API。

在 Linux ECS 上分别启动以下常驻进程。Chroma 的 `--path` 填现有可用索引的实际路径，下例与默认目录对应。

```bash
cd backend
chroma run --path ./storage/chroma_db --host 127.0.0.1 --port 8001
```

在相同工作目录初始化数据库并启动 API。

```bash
python -c "from app.core.database import init_db; init_db()"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

再分别启动恢复进程、解析 Worker 和索引 Worker。三个命令各自占用一个终端或服务单元。

```bash
python -m app.tasks.file_task_recovery
FILE_TASK_WORKER_ROLE=parse celery -A app.tasks.celery_app:celery_app worker -Q document.parse -n parse@%h --pool=prefork -l INFO
FILE_TASK_WORKER_ROLE=index celery -A app.tasks.celery_app:celery_app worker -Q document.index -n index@%h --pool=prefork -l INFO
```

新安装的解析并发默认是 2，已有安装沿用设置页持久化的解析并发，需要在设置页调整并重启解析 Worker。仅运行一个索引 Worker，索引并发固定为 1。HTTP 接收只提交回执，首次投递、阶段交接与补投均由恢复器执行。恢复器默认每 5 秒扫描，未领取的消息间隔 60 秒可补投。业务租约默认 90 秒，每 20 秒续期，续期受首次领取时的执行期限约束。默认软超时为 3300 秒，硬超时为 3600 秒。

```bash
redis-cli -u redis://127.0.0.1:6379/0 ping
curl -fsS http://127.0.0.1:8001/api/v2/heartbeat
celery -A app.tasks.celery_app:celery_app inspect active_queues
celery -A app.tasks.celery_app:celery_app inspect stats
```

检查 `active_queues` 中的订阅和 `stats` 中的实际池大小，再提交两个不同文件。通过知识库的「处理任务」观察队列，并在两份任务均完成后检查召回内容。

## 从旧部署升级

先停止接收新的文件操作，让旧版执行中的任务结束，停旧 Worker 和使用嵌入式 Chroma 的 API。记录现有 SQLite 及 Chroma 的真实路径，使用维护窗口的可恢复备份，再以同一目录启动单一 Chroma 服务。执行上述数据库初始化；升级期间避免旧版 Worker 消费旧队列并写入。

新索引保存在独立构建中，发布事务同时替换切块、BM25、活动指针和回执。旧向量沿用 SQL 中已有的切块 ID。选择原数据库与对应的 Chroma 目录后，用原有文档抽查全文、向量召回和引用。历史活动回执缺少输入快照或执行租约时会进入 `UNKNOWN`；历史完成记录和结果保留。

## 重试和核对

- 客户端可通过 `Idempotency-Key` 提交 parse、vectorize、分块提交和已发布分块编辑。同一次操作重复提交返回原任务；同键不同输入返回 409。
- 发布前继续使用旧构建。索引 Worker 按已登记的 ID 清理退役向量，恢复器负责再次安排清理。
- 模型请求已发出、响应或持久化结果不明时，任务进入 `UNKNOWN` 并保留文件占用。在任务页填写依据后，可使用已核实的本地结果、明确授权重试或终止并释放。缺少冻结输入的历史任务需要先终止，再重新提交。
- 普通删除遇到活动回执会返回 409。明确取消并删除可调用 `DELETE /api/v1/files/{file_id}?cancel_active=true`，撤销执行权和文件可见性在同一事务内完成。
- 技术说明见 [SQLite WAL](https://sqlite.org/wal.html)、[Chroma 并发约束](https://cookbook.chromadb.dev/core/system_constraints/) 和 [Celery Redis 消息可见性](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)。
