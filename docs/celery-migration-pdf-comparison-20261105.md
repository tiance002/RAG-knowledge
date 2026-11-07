# Celery 迁移与 PDF 路线对比实测

本轮实际完成代码迁移、ECS 隔离测试和正式 API/Worker 更新。增强 PDF 未切为正式默认。业务外部模型调用为 0；索引工程测试使用一个本机 HTTP Embedding 替身，不代表真实供应商向量质量。

## 迁移结果

- 正式 API：127.0.0.1:8200，部署回执 PID 1701212，健康 HTTP200。
- 独立 Worker：systemd `amrag-ingest.service`，active，control ping pong；prefork、并发1、时间限制3600秒、MemoryMax768M、CPUQuota100%。空闲后观测内存约191041536字节；不是高负载资源评测。Celery5.6.0、redis Python客户端7.1.0。
- Redis 复用现有实例，先确认 DB14/15为空，再分别用于 broker/result；队列 amrag_ingest。未迁移长期偏好键。实例运行配置 AOF yes、appendfsync everysec、noeviction、maxmemory64MiB。逻辑库不是内存或故障隔离；未验证Redis整机断电恢复。
- API 的解析、分块提交入库、手动向量化统一调用 Celery；不再通过 BackgroundTasks 执行这些任务。旧 Celery parse/submit 全异常重试包装已移除，核心处理函数保留。
- 新增 `file_task_receipts`：每文件一个活动任务、原子认领、task_id、状态、参数快照。API可查询任务列表与详情；忙碌返回409，投递未确认返回503并保留相同task_id。后台异步任务完成仍通过原文件状态提供进度。
- 已完成任务重复投递会跳过。执行中断/结果不明保留 UNKNOWN，不自动再次调用可能已经请求过的供应商。排队消息可以在API退出后继续执行；这不等于任何崩溃都能自动安全重跑。
- 当前不自动重试整个业务任务；已明确失败与UNKNOWN分别保留。已有数据库局部提交重试保持原样。要恢复部分已调用模型的任务，仍需核对请求记录及已保存结果，不宣称已实现页级断点续跑。UNKNOWN活动锁需人工核对后解除，尚无自助安全解锁流程。

修改文件：`backend/app/api/files.py`、`tasks/dispatch.py`（新增）、`tasks/celery_app.py`、`tasks/parse_tasks.py`、`tasks/__init__.py`、`core/config.py`；新增 `backend/tests/test_celery_dispatch.py`、`scripts/celery_migration_acceptance.py`、`scripts/compare_pdf_routes.py`。原工作区大量未提交改动已保留，未提交或推送。

## 备份、上线及数据核对

ECS `/opt/amrag/backups/celery-migration-20261105/` 保存旧 app 归档、SQLite 在线备份（quick_check=ok）、权限0600的旧环境配置、API日志和 deployment.json。旧源码目录 `/opt/amrag/backend/app.before-celery-20261105`。

升级前后 `file_documents=11779`、`document_chunks=37271`、`knowledge_bases=6`、`custom_models=5`。未删除源文件、索引或历史结果，没有重新入库正式资料。仅新增必要任务回执表与Celery连接配置。

回滚应先暂停接收新入库任务，确认队列与运行任务状态，停止Worker，恢复备份源码和环境，再重启API；保留回执表、数据库与索引，不拿旧数据库覆盖上线后产生的新数据。不得在Worker仍处理任务时切回旧API触发第二份处理。启动失败自动回退分支已准备，本次未触发；完整回滚演练 NOT_RUN。

## 测试及命令

1. ECS：`PYTHONPATH=/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools:/opt/amrag/experiments/celery-pdf-20261105/code/backend /opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -m pytest /opt/amrag/experiments/celery-pdf-20261105/code/backend/tests/test_celery_dispatch.py -q --basetemp /opt/amrag/experiments/celery-pdf-20261105/pytest-temp`：退出0，5 passed；31条utcnow等弃用警告，未作为产品失败。日志 pytest.log、pytest-exit.json。
2. ECS：`/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/celery-pdf-20261105/celery_migration_acceptance.py`：真实HTTP→真实Redis→真实prefork Worker；无Worker时排队、API退出、Worker完成、重复投递不改变块ID、API重启后读到完成状态，5项工程检查通过。逐项 results.json 已保存；独立后台进程的最终OS退出码未额外落盘，标 UNKNOWN，不能用启动SSH退出0冒充它。
3. 同命令加 `--with-index`：新隔离目录，重复前述链路，加一次本机HTTP Embedding替身；真实Chroma/BM25写入，API已存在的读取端可看到新结果，重复入库不再调用Embedding。7项工程检查通过。1次本机Embedding HTTP、0次外部模型。最终OS退出码同样未单独保存。脚本中 paid provider count 后的旧 basis 文案不完整；以本段区分本机替身/外部调用，不声称从未实例化任何模型客户端。
4. 正式部署命令SSH退出0；API健康200、Worker ping pong。此次没有对正式资料调用入库或付费模型。

未测：真实Worker进程SIGKILL/硬超时、Redis重启、重负载、真实供应商入库、前端浏览器。UNKNOWN分支为确定性测试，不冒充真实供应商超时。任务状态接口已接入，但前端尚无专门的UNKNOWN解锁交互。

## PDF 数据清点及对比条件

此前“3份”只指正式上传目录内容去重。扩展到历史 experiments 与 uploads，共找到106个PDF路径；排除测试目录并按SHA256去重为16份非测试PDF，本轮从中复用6份代表资料，共308页。其余10份未在本轮逐份验证来源完整性。没有新增下载或复制原始PDF。

真实比较调用：`/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/celery-pdf-20261105/compare_pdf_routes.py --manifest /opt/amrag/experiments/celery-pdf-20261105/pdf-inputs.json --out /opt/amrag/experiments/celery-pdf-20261105/pdf-results --backend /opt/amrag/backend --tessdata /opt/amrag/experiments/pdf-table-fix-20261027/var/tessdata --timeout 600`。

两臂均调用真实FileParser，custom500/50，语义切块关闭；增强使用原默认扫描表格false、OCR150dpi。每臂独立子进程、同一输入哈希、禁止socket外连。PyMuPDF1.26.7复用历史语言包进行内置OCR，无需安装Tesseract命令。12次解析退出码均0、network_attempts均0。

ECS冻结runner SHA256 `aafba7487da138b6ad2a5ec91a748ff43cde3999a1c5feb10af4a87e0c3ac6ea`。本地脚本随后增加parse_elapsed_s等记录，SHA256 `4d7d986bbfebfb84e6ce9e5937036384d8d2f314f8c82c74ad4540f66ad8e9e1`；运行中未替换ECS版本。下表耗时为已冻结旧runner每臂总耗时，包含导入与共同的原页统计，不能当成纯解析CPU耗时。

| 来源 | 页数 | 非图片块字符 legacy→enhanced | 表格块 legacy→enhanced | 总耗时秒 legacy→enhanced |
|---|---:|---:|---:|---:|
| CatDog扫描课件 |68|0→24534|0→0|2.015→117.440|
| 物理扫描课件 |21|0→15072|0→0|1.652→38.220|
| 2016选举文字报告 |45|82336→81784|0→10|2.178→14.514|
| 中文审计报告 |58|32234→32907|0→32|2.379→6.158|
| Costco2021财报 |76|244859→245017|0→2|1.790→30.448|
| 显示器用户手册 |40|61805→63116|0→0|2.620→23.713|

字符按实际块内容求和并排除image块；仍包含块间重叠，不能当作唯一事实量。legacy扫描文档的总字符串非空来自图片标记，不是可检索OCR正文。表格块数不是表格检测准确率，尚无全部真实表格数量分母。

## 关键证据复核

- 选举报告物理第45页：两臂均保留Trump533/5.1、Clinton587/4.9、Total1254/3.4。增强输出为明确键值行（例如 `Group=Trump voters | Unweighted sample size=533 | Plus or minus …=5.1 percentage points`）；legacy保留纵向纯文本且含部分切块重叠。确认结构送达改善，不声称新增数字或回答正确率提升。
- 扫描物理第13页：legacy只有图片标记；增强正文已含water/Fat molecules等内容，但仍有OCR杂字，历史关注的1cm未在该页抽取文本发现，需对照原图核验，不能因OCR非空而判为完整正确。
- 扫描物理第16页：增强仍有明显失真和乱码，无法据此确认完整Radiowave行已恢复。本轮scan_tables=false，未验证扫描表格开启效果。
- CatDog第41页界面截图：增强提取出Jupyter/代码等正文，但未产生表格块；有重复与错字，不代表代码可直接执行。
- 增强输出全部1332块有source_locator，legacy对应1143块没有同一套精细locator；legacy仍有页码，不能说legacy完全没有定位。增强OCR来源身份可追踪，不等于OCR事实正确。

结论：增强路线在扫描正文可检索性、来源区域定位和部分文字表格结构上有实测收益，同时更慢且OCR难例未解决。保持正式legacy默认。当前回答正确率、recall@10、引用精确率、真实Token/费用收益均 NOT_RUN；历史12题评测不是当前版本结果，不混入本轮指标。

## 证据与收尾

ECS工作目录 `/opt/amrag/experiments/celery-pdf-20261105/`；本地证据包 `D:\RAG knowledge\var\celery-pdf-20261105\evidence.tar.gz`（约1.41MB），包含冻结运行器、测试源码、逐次日志、chunks.json、extracted.txt、results.json、任务回执和清理清单；没有API Key或原始PDF。

复用已有Redis、Python环境、历史OCR语言包和6份原PDF；仅新建约1MB源码快照、小型合成测试库/索引及必要正式SQLite恢复备份。测试API/Worker均已停止，测试DB12/13仅删除经核对属于本次任务的键，无FLUSHDB。清理14个中间图像/测试索引目录，17577804字节；保留小型SQLite回执和日志、冻结输出、正式备份。主盘显示由18GiB剩余到17GiB（整GiB舍入值，不能据此认定增长1GiB），最终55%使用；正式备份约95MiB。

用户界面解析并发仍需在后续设置权威接线中明确与CELERY_WORKER_CONCURRENCY的关系；当前正式Worker明确为1，不声称设置页并发可热更新。没有顺带修改动态路由、历史轮数或问题改写。
