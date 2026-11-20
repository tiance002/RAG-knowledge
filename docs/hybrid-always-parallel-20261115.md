# Hybrid 初始双路固定并行（2026-11-15）

本地 API 的文件型 SQLite Hybrid 改为固定并行初始向量路与 BM25 路。删除串行模式环境开关、Session pending writes 判断和 SQLite driver.in_transaction 判断。活动代码、测试、环境示例及预检脚本不再引用旧开关。

BM25 使用执行器拥有的独立 query_only 连接，读取已提交的索引；不提交或回滚调用者事务。API 启动的 managed_lexical_executor 继续提供共享的两个读槽位，Chroma 保留自己的有界执行器。查询扩展在合并初始有效候选后按 top_k 判断，并发预算、取消和 deadline 不变。

兼容边界仍存在：内存数据库、StaticPool/SingletonThreadPool、非 SQLite、非 SQLModel Session、未管理执行器的直接脚本调用保留既有同会话串行路径。因此本次是正式 API 文件型 SQLite 路径固定并行，不是让任意测试替身或共享连接跨线程运行。

验证复用已有 .venv，四个相关测试文件 91 passed / 0 errors / 0 failures / 0 skipped，退出码 0，10.37 秒，1984 条既有弃用警告。barrier 测试验证两路确有重叠，真实 SQLite 测试验证未提交的新行对独立 reader 不可见；其余包含 postings、授权过滤、预算、取消、扩展与排序。模型和向量边界使用测试替身，不是新一轮 ECS 性能测试。

原始记录：var/hybrid-always-parallel-20261115/tests-final.log、tests-final.xml、tests-final.exitcode、changes.patch。复用现有环境，没有复制语料、数据库、索引或创建容器。新增代码快照和测试证据，保留以便核对。磁盘可用空间 133882236928 -> 133882068992 bytes（共享工作目录观测值）。本次尚未向 ECS 发布。

2026-11-15 清理：删除旧开关的测试注释与配置注入；排序对照脚本将串行控制显式限定于实验进程的实例补丁，不再伪装为生产配置。历史证据目录原样保留。
