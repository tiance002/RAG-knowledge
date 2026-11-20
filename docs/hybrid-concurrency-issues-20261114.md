# 混合检索与并发：集中问题账本

本轮先保留失败回执再修复，不通过格式规则、降低Gold标准或隐藏降级来宣布通过。正式服务未更新。

| ID | 阶段/证据 | 问题 | 处理与状态 |
|---|---|---|---|
| H1 | 源码审查、`ab20`、`ab500-ready` | 旧150向量/50BM25按KB及查询重复申请，无请求总预算；首版总150/50又过浅 | 首版22/33→19/33，判不合格并保留。只测试一次通用500/500替代配置，逐项恢复至44/66（33单元重复两次），零新丢失；最终按作用域缩放并封顶500，单KB仍150/50。不是答案正确率 |
| H2 | 源码审查/真实SQLite反例 | 初始候选计数早于有效来源过滤，失效块能阻止扩展 | 批量ID/有效文件/KB/ACTIVE manifest检查在并集统计前执行，已测 |
| H3 | 本地/正式源码比较 | 查询向量复用、HTTP池本地有实现，正式ECS未接线 | 最小兼容候选接线，不覆盖其他本地功能；10个实际KB零外调身份等价检查通过 |
| C1 | 源码审查、真实Chrome | 前端单一controller和busy阻止其他会话；路由切换有写入/清空竞态 | 按会话管理提交与controller，路由所有权保护；27项浏览器断言通过，零外部Provider调用 |
| C2 | 真实ASGI故障复现，零外调 | SSE正文send抛错后许可已释放、生成器仍存活 | 显式关闭内外生成器；未完成close任务继续持有许可，直到实际完成；纳入229项定向集成回归 |
| C3 | Chrome取消反例、真实SQLite/ASGI | 已取消生成，但会话仍RUNNING；重复取消可能在清理await处丢失终态或许可归属 | 在可取消清理前持久化CANCELLED/UNKNOWN及部分正文；先挂接后台provider的许可归属，再cancel/await。Chrome复测A取消、B完成、刷新不串会话 |
| E1 | R4两次隔离冒烟 | HTTP200/SUCCESS但vector_count=0，degraded=true；不能把1.5秒当双路优化收益 | 查明候选缺task_effect_context.has_active_effect_scope，复用key构造ImportError，未到Embedding网络边界。补齐同一ContextVar上的只读helper；R6真实1024维Embedding、两路有效，R7五题实际SDK请求捕获通过；R4保留 |
| E2 | 跨版本依赖审查 | ECS无async_batch_rerank，直接同步本地Recall会导入失败 | ECS补丁保留原同步Rerank，本轮显式关闭；本地Recall已用既有async实现。ECS该路径NOT_RUN，不偷换为已部署 |
| T1 | 初次本地集成25失败 | 验收app导入即设置Chroma执行器环境变量，污染其他测试 | 只在创建验收app期间patch.dict，测试结束还原；产品未为此修改 |
| T2 | 第二次集成1失败 | Windows负载下80ms合成deadline与调度抖动竞争 | 仅合成fixture改500ms并保留阻塞/停止断言；产品30秒不变。最终229 passed，零失败/跳过 |
| T3 | `ab500`启动竞态 | 隔离API进程active但socket未就绪，ConnectError被旧驱动记为UNKNOWN | 原回执保留UNKNOWN，复核为NOT_SENT（无服务端receipt）；增加GET健康预检及连接前失败分类，新run目录重新开始。不是重发未知Provider调用 |
| T4 | 真实Chat请求捕获 | 借用Transport的外层SDK与内层连接池各经过一次AsyncClient.send，原探针记录重复 | 仅探针增加send_depth/transport_class/network_attempt；第三道新题确认depth0借用层+depth1真实HTTP层，共一次网络尝试。前两题保留原两层记录，不据此算两倍费用 |
| Q1 | 同预算串并行40对 | 7对有序候选/正文不同，不能宣称输出等价 | 7对在扩展前已不同，共同块BM25分/正文哈希相同、向量分不同；未保存向量及全部route池，精确原因UNKNOWN。40对引文覆盖逐项一致；不盲目修补RRF |
| Q2 | 三道真实Qwen冒烟 | 打印机题核心正确，但Print Spooler位置的[2]引用不支持该句，[1]有依据 | 已保留原回答和引用；生成/引用问题，不新增严格门禁，不在本轮改Prompt；其余真实题不据此宣称完整质量验收 |
| R1 | 2/4会话各40检索 | 四会话吞吐几乎不增而P95约翻倍 | 保留active2/queued4/5秒；8会话NOT_RUN，未提高Chroma限制或Uvicorn进程数 |
| D1 | 来源及评分清单 | 33证据单元只有21可精确映射子块，10仅父块、2无完整同文 | 保留12项UNKNOWN，不编造chunk qrel；只报完整引文覆盖及可确认映射，不报完整recall@10 |

原始日志与候选版本在 `var/hybrid-concurrency-20261114/`、`var/hybrid-request-budget-20261114/`。完整统计、命令、未运行项及回退见 `hybrid-concurrency-implementation-and-validation-20261114.md`；所有旧失败保留。
