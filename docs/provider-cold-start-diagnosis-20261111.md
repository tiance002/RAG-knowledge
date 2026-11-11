# Provider 冷启动诊断与启动预初始化（2026-11-11）

## 已证实的结论

用户提出的SSLContext假设已通过**独立进程、纯HTTP loopback**复现：HTTPX在发送前构造客户端时同步加载CA，并首次导入httpcore。没有TLS握手不代表没有SSL上下文初始化。本机首次客户端构造的中位数约287.57ms，其中SSL初始化约226.70ms；ECS对应约42.77ms，其中SSL约16.77ms。二者是嵌套区间，不可相加。

这证明了可复现的同类原因，但不能事后精确分解旧实验每一毫秒。旧实验先运行同步分支，再运行异步分支，不是独立冷进程；旧的约287ms差值不应当直接标为证书耗时。

已在本地实现有限的启动预初始化，完成本地测试后只更新ECS隔离目录。没有重启或修改正式服务，没有新增供应商请求。真实问答正确率/整题延迟、HTTPS握手、Cloud回读均 **NOT_RUN**。

## 设计和数据口径

新增 `scripts/benchmark_provider_cold_start.py`，复用已有loopback服务器与禁止外网连接的护栏。每组3个独立Python进程，每进程3次相同的产品 `RerankService.arerank` 请求，服务端固定等待100ms。A/B/C按重复轮次交错启动，不复用进程，不清理OS文件缓存。

| 组 | 客户端创建 | 网络准备 | 用途 |
|---|---|---|---|
| A | 第一次请求时 | 无 | 原有懒初始化对照 |
| B | 请求开始前，对同一池键提前取得/释放客户端 | 无 | 诊断构造的独立贡献 |
| C | 同B | 相同Origin、相同池键的本地 `/v1/health` | 诊断首次发送/建连的贡献；仅合成实验 |
| D | 实际 `managed_provider_http(preinitialize=True)` | 无 | 验证本次真实代码实现 |

各进程保存实际输入、结果、连接ID、模块状态、Python/HTTPX/httpcore/OpenSSL/CA版本及哈希；`AsyncClient.__init__`、`ssl.create_default_context`、`load_verify_locations`和首次import均有嵌套区间。HTTPX `trace` 使用 `async def` 回调记录TCP、发送请求、接收响应等事件。连接池名额等待另由项目Provider事件记录；没有饱和排队的本实验不声称已测负载下PoolWait。

首请求/稳态耗时是客户端调用总耗时，回调延迟是10ms回调的超期量，**不是模型TTFT**。启动准备及回调仪器额外等待独立记录，不把它藏在测量外。后两次调用用于展示稳态；3次进程样本只报中位数和原始明细，不报生产P95或统计显著性。

HTTP的TLS握手为NOT_RUN。`connection.connect_tcp`包含客户端后端准备等开销，不能把它叫纯线上TCP包耗时。Trace未暴露的细分等待保留未知。

## A/B/C实测

| 地点 | 组 | 首次请求中位数ms | 首次回调延迟中位数ms | 后续请求大致ms |
|---|---|---:|---:|---:|
| Windows本机 | A | 438.55 | 320.85 | 103 |
| Windows本机 | B | 150.12 | 31.23 | 104 |
| Windows本机 | C | 104.83 | 1.90 | 103 |
| ECS隔离 | A | 164.43 | 50.88 | 101–102 |
| ECS隔离 | B | 119.67 | 5.69 | 102 |
| ECS隔离 | C | 101.59 | 0.73 | 102 |

本机B的预初始化约310.52ms，C约284.27ms加health约40.31ms。ECS B约35.64ms，C约41.58ms加health约19.15ms。成本转移到启动阶段，没有消失。

首次构造归因（A组，每项各自中位数）：

| 地点 | AsyncClient构造 | 其中SSL上下文 | 其中CA加载 | 构造内首次httpcore导入 |
|---|---:|---:|---:|---:|
| 本机 | 287.57ms | 226.70ms | 226.43ms | 66.34ms |
| ECS | 42.77ms | 16.77ms | 16.55ms | 16.67ms |

表中为嵌套且分别取中位数，不可相加，不要求数值凑成总量。本机主要是CA加载；ECS不能把所有开销都归于CA。`httpx`首次导入在单独的bootstrap阶段，不能再次计入请求。

两地A/B/C均通过15项适用工程断言：9个新进程/每进程3次业务请求、相同源码、明确期望的输入/输出、B零发送、C同连接预热、后续连接复用、无外网、服务退出。另各用1个进程做空闲期限对照。ECS最外层SSH尾部的退出码打印转义不正确，未得到独立保存的orchestrator退出码；其原始日志及summary为PASS，全部10个子进程退出码0。该外壳记录缺口保留，不重新运行冒充原回执；后续D由Python运行器准确保存总退出码。

## 30秒与60秒

两地实测空闲约31.1秒：30秒配置的连接ID从1变3，60秒配置从2保持2。只证明到期后下一次请求的重连/复用机制。HTTPcore会在池分配等操作时清理到期连接，不保证后台定时在第30秒精确关闭。

本项目原代码已经设置30秒，本轮**保持30秒**。没有真实请求间隔分布、服务端闲置断连和资源开销证据，不把60秒称为最佳值。不保活到无限，也不对供应商发送health或真实重排作为预热。

## 已实施的最小修复

1. `backend/app/services/provider_http.py::ProviderHTTPManager.preinitialize` 在启动阶段创建4个无端点、无Authorization、零请求的备用客户端，精确对应已有超时/环境策略：60/120/5秒且`trust_env=False`，以及60秒且`trust_env=True`。没有更改请求超时值或TLS验证。
2. 首个匹配的请求**一次性领取**备用客户端，然后加入已有的地址、凭据摘要、超时、TLS、环境策略池键；不同凭据不会领取已使用客户端。按原上限复用长生命周期连接池，未使用的备用客户端在退出时关闭。
3. `backend/app/main.py::lifespan` 开启预初始化，位于提供请求服务之前。独立脚本/旧`managed_provider_http()`默认仍为懒初始化，保留冷对照与Worker生命周期。
4. 自定义CA、非匹配配置、相同配置下第二套不同地址/凭据，仍可能触发懒初始化。`OPENAI_PROXY`分支继续使用LangChain自身代理客户端。本轮不声称消除了所有首次构造阻塞。
5. 预初始化失败时关闭已建备用客户端，记录错误类型，保留原按需创建路径；真实请求若仍有CA错误会按原错误返回，**不会改成关闭TLS校验**。设置修复入口仍可访问。
6. 新增 `provider.client_initialize` / `performance.stage=provider_client_initialize` 计时。`provider.slot_finished`修正为取得名额至释放，含构造和临时关闭；原来这一指标不含构造。更新脱敏受控枚举，避免阶段名变成不可读哈希。

没有引入动态线程创建客户端及取消清理的新状态机，没有更改业务Prompt、模型、检索或重试策略。

## D实际实现验证

| 地点 | 启动预初始化中位数ms | 首请求ms / 回调延迟ms | 第二、三请求ms |
|---|---:|---:|---:|
| 本机 | 1409.47 | 154.61 / 36.96 | 102.13 / 102.74 |
| ECS隔离 | 100.19 | 123.18 / 10.58 | 101.31 / 101.64 |

D一次准备4种配置，比B仅创建一个客户端启动成本高，不能隐藏这个代价。本机预初始化阶段回调延迟约1399.58ms，ECS约90.28ms；此时应用尚未ready。首请求仍有首次发送/后端初始化/连接开销，本轮不启用真实供应商网络预热来追求C的数字。

每地D共3个新进程/9个业务请求，均退出0；真实构造全部发生于启动阶段，业务阶段零构造；启动零发送；3个未使用+1个已领取客户端均关闭。D-only报告中对不存在的C/idle的布尔条件不代表重测这两项，其验证依据仍是前面的A/B/C归档。

另 `backend/tests/test_provider_startup.py` 进入真实FastAPI lifespan，保留真实HTTPX/SSL构造，只隔离数据库/目录/settings/flush副作用，并禁止任何HTTP send。验证4个备用客户端、120秒请求领取、同凭据复用、异凭据隔离、零发送和完整关闭。

## 测试与可复制命令

| 范围 | 本轮最终结果 | 退出码 | 原始证据 |
|---|---|---:|---|
| 本机相关集成回归 | 191 passed / 0失败错误跳过 | 0 | `.planning/2026-11-11-qa-performance-p0-p1/integrated-cold.log`、`.xml` |
| ECS隔离相关集成回归 | 149 passed / 0失败错误跳过 | 0 | `ecs-startup-evidence/pytest.log`、`pytest.xml`、`receipt.json` |
| 启动备用客户端定向测试 | 12 passed；属于上述范围 | 0 | `clients/pytest-preinitialize-final.log` |
| 实际FastAPI lifespan | 1 passed；属于上述范围 | 0 | `baseline/pytest-provider-startup.log`、`.xml` |
| 本机D | PASS | 0 | `rerank/cold-start-actual-d-local-20261111/summary.json` |
| ECS D | PASS | 0 | `ecs-startup-evidence/actual-d/summary.json`、`receipt.json` |

不同环境与子集不能相加成独立测试总数。旧的186/144是修复前源码范围，保留，不覆盖为新数字。测试中首次计时替身错误地影响了asyncio时钟，已仅修正替身并保留原失败日志；未改业务断言凑通过。

可复制本机命令（每次产生新输出目录，0外部模型调用）：

```powershell
Set-Location 'D:\RAG knowledge'
& '.venv\Scripts\python.exe' scripts/benchmark_provider_cold_start.py --output-dir ('.planning/2026-11-11-qa-performance-p0-p1/rerank/cold-' + [guid]::NewGuid().ToString('N')) --arms A B C D --repetitions 3 --delay-ms 100 --callback-ms 10 --idle-seconds 31
```

上述为以后复跑命令；本次A/B/C与D分开保存，实际命令逐项在summary.children.command及ECS receipt.json，不能称为本次执行过一个合并四组命令。完整定向pytest命令是P0/P1报告中的命令增加`backend/tests/test_provider_startup.py`，输出改为`integrated-cold.xml`；实际日志和XML交叉核对一致。

## 归档、资源和未运行项

- 主报告：`docs/qa-performance-p0-p1-20261111.md`。本诊断修复7文件增量包37300字节，SHA256 `a67e095e4ae4a9e36384429f722ab51dc10363624a1f078e66ee0e872cccda70`。源码哈希清单为`startup-source-r2.json`；修改前字节保存在ECS `startup-r2/before`，原始源码包也保留。
- ECS复用目录：`/opt/amrag/experiments/qa-performance-p0p1-20261111-r1`。隔离目录当前约6.2MiB，不复制数据库/语料/索引/环境；所有临时服务关闭。未删除历史结果。
- 同步完成后7个文件hash一致；正式8200 API仍为PID3597452。无正式切换、无Git提交/推送。
- 本次冷启动诊断新增真实供应商调用0；此前P0/P1传输冒烟的2次保留在原报告，不混成这轮成本。
- 当前结果是SYNTHETIC传输机制验证。真实问答正确率/Token账单/完整端到端性能、HTTPS握手、Cloud回读均NOT_RUN。动态配置新增池仍可能冷构造，生产网络闲置策略仍待实测。

资料依据：[HTTPX trace扩展](https://www.python-httpx.org/advanced/extensions/)、[连接池资源限制](https://www.python-httpx.org/advanced/resource-limits/)、[SSL配置](https://www.python-httpx.org/advanced/ssl/)。本机与ECS均以已安装HTTPX 0.28.1实测，不只根据网络讨论归因。
