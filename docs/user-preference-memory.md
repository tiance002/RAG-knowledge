# 长期回答偏好与 Redis 恢复

在「系统设置 → 长期回答偏好」可以选择回答风格、解释深度和输出偏好。点选后立即保存，页面逐项显示来源。也可在聊天中说“记住我是初学者”“以后回答都简洁一点”“之后比较方案都使用表格”。“这道题使用表格”仅影响当次回答。“忘记我的回答风格偏好”“删除我之前设置的输出偏好”“恢复默认设置”分别撤销单项或全部。无法确定长期意图时，程序继续回答问题。

## 数据与接口

当前个人全局 Redis Hash 为 `rag:memory:preferences:default`，无 TTL。空库读取不会创建这个 Key。

| Hash 字段 | 默认与存储 |
|---|---|
| `answer_style` | 默认 `normal`。可显式选 `concise / normal / detailed` |
| `knowledge_level` | 默认 `intermediate`。可显式选 `beginner / intermediate / expert` |
| `output_preferences` | 默认空列表。显式列表按 JSON 数组保存，可选 `step_by_step / table / code_example / principle_first`。空数组也可以是用户明确设置 |
| `updated_at`、`schema_version` | 修改时记录 UTC 时间及格式版本。最后一个显式偏好删除后清除该 Hash |

只存用户设置的字段。显式选 `normal` 和删除 `answer_style` 均得到正常风格，两者的来源分别为 `explicit` 和 `default`。无论设置多少次，同一身份始终使用同一 Hash。不会在此存入对话、检索内容、密钥、Gold 或 RSI 经验。

管理接口使用 `GET /api/memory/preferences`、`PUT /api/memory/preferences`、`DELETE /api/memory/preferences/{field}` 和 `DELETE /api/memory/preferences`。PUT 只发有意修改的字段，例如 `{"output_preferences":["table"]}`。读写成功时返回 `values`、逐字段的 `sources` 和 `redis_fallback`。无效值、未知字段、空更新返回 422。读取故障时返回默认值和 `redis_fallback=true`，写入无法确认时返回 503。网络中断或写后回读失败时，请先用 GET 核对。

目前 API 与聊天共同使用服务端身份依赖 `get_preference_scope`。单用户部署须限制为本机访问或受控网关，所有获准访问者使用同一份全局偏好。未来启用账户时，请在同一依赖中从已认证会话或令牌解析 tenant、user，替换默认身份，并明确旧个人 Key 对应的真实账户。作用域可生成 `rag:memory:preferences:{tenant_id}:{user_id}`，服务端标识按段转义。库级键的预留格式为 `…:{user_id}:kb:{kb_id}`。在启用库级覆盖前，还需要验证服务端知识库权限、实现逐字段继承和数据迁移接口。

回答时按系统约束、本轮明示、用户显式偏好、应用默认值的顺序处理。正常问答与 Agent 共用表达策略，默认未设置时保留原有 Prompt。偏好段仅提供表达指令，不进入文档上下文或知识来源。表格用于合适的比较，原理优先用于技术解释，代码示例用于编程问题。

## 运行配置

未设置 `MEMORY_REDIS_URL` 时复用 `REDIS_URL`。队列和缓存所在实例如果允许淘汰、缺少持久化或由多人共享，先为偏好指定独立的持久化 Redis。仅换 DB 编号不能隔离实例级淘汰、备份或重启。配置 `MEMORY_REDIS_URL` 仅影响偏好，不改变任务队列或检索缓存。

仓库提供 `config/redis-preferences.conf`。在独立的持久数据目录下启动，启动时传入该配置的绝对路径。配置仅绑定回环地址和 6381 端口，启用保护模式、AOF 每秒落盘、RDB 的 `900 1` 和 `300 10` 快照条件，使用 `noeviction`。配置没有强设内存上限；实际部署按可用内存和持久化峰值配置。达到上限时应拒绝写入并报告失败。每秒落盘仍可能在主机故障时丢失最近写入；RDB 恢复点取决于备份时间。数据目录和备份要限制访问。给其他主机提供服务前需另行配置认证、网络访问控制和加密。

下面是使用 Redis CLI 对**已选择的独立本地实例**进行只读核验的示例。已启用认证的环境使用受控凭据提供机制，不将口令写入命令或日志。

```sh
redis-cli -h 127.0.0.1 -p 6381 CONFIG GET appendonly appendfsync save maxmemory maxmemory-policy
redis-cli -h 127.0.0.1 -p 6381 INFO persistence
redis-cli -h 127.0.0.1 -p 6381 TTL rag:memory:preferences:default
```

已有 Key 的 TTL 期望为 `-1`，尚未设置时为 `-2`。真实部署的配置须以目标实例的实测结果为准。

## 备份和故障恢复

1. 为偏好实例创建受限的备份目录，文件名包含 UTC 时间且不覆盖旧文件。执行 `BGSAVE`，轮询 `INFO persistence`，确认本次后台保存已完成且 `rdb_last_bgsave_status:ok`。复制该次完成的 `dump.rdb`，计算 SHA-256，连同实例版本和备份时间保存到独立存储。另一种办法是在有相应权限时用 `redis-cli --rdb <新文件>` 取得一致性快照。给已有数据制定定期备份和恢复抽检计划。
2. 恢复时先验证备份 SHA-256。在**新的空数据目录**放入备份，启动只监听回环的新 Redis，用 `--appendonly no` 首次载入 RDB，避免旧 AOF 抢先载入。通过应用 GET 核实显式字段、值与来源，并检查 TTL。回读前不要把正式应用指向该实例。
3. 在恢复实例执行 `CONFIG SET appendonly yes`，检查 `aof_enabled:1`、`aof_rewrite_in_progress:0`、`aof_last_bgrewrite_status:ok`。确保下次启动配置仍为 AOF 开启，并用该配置再重启一次复核数据。原实例、原 AOF/RDB 及原备份均须保留；切换另行决定。

真实 Redis 的本地验收通过应用 API 写入，先生成 RDB，再修改一个字段。重启原目录必须读出较新值；从旧 RDB 恢复到独立目录必须读出旧值，并能在重新开启 AOF 后再次重启。测试和记录见 [专项验收](user-preference-acceptance.md)。Redis 持久化行为参考 [Redis 官方文档](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。
