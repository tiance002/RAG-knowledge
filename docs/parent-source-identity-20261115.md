# 父子命中、上下文与引用身份适配（2026-11-15）

已完成本地实现、最小ECS部署和原冻结五题验证。参考WeKnora SQLite Lite共用合并/引用机制：文档顺序代表与最高分子块独立，扩展正文以真实来源ID呈现，评测按命中子块而非合并代表ID计数。本项目保留按父块建立上下文组，不宣称逐行复制WeKnora的一般相邻内容合并算法。

## 实现

- `seed_child_ids` / `hit_children`：完整命中子块ID及各自原文、来源、哈希。
- `parent_chunk_id`：父块关联；`context_group_id`：稳定上下文组身份。
- `representative_child_id`：按文档顺序选择，与`score_child_id`明确区分。最高相关性只解释组分数，不将其自动归给组内其他来源。
- `citation_sources`：实际选用的父块或回退子块，每份原文挂真实来源ID、类型、元数据。
- 普通chat/agent继续通过真实source_members组装引用；新增类型、代表元数据一致性校验，拒绝篡改种子映射。
- 召回单库/多库API和响应schema同步；evaluation分别给出matched_seed_count/context_group_count/citation_source_count；缺映射旧响应为UNKNOWN。
- qrel helper仅在显式Gold子块分母和完整映射可核验时返回Recall；来源父ID不会冒充检索命中子块。
- context_window=0明确表示不扩展相邻子块，父块映射照常处理。

## 验证

最终本地10文件回归136 passed，exit0，failures/errors/skips0，14.12s，1378条已有弃用警告。ECS最终overlay实际venv隔离2/2通过，无模型调用或正式DB写入；原schema候选7/7通过。首次本地父组identity验证失败及中间版本日志保留，最终版本以regression-final和ecs/r2为准。

最终五题每题3次，共15次成功，发布前同题15次成功；54次健康探针成功。输出每轮均VERIFIED，10个命中子块，对应10/10/6/8/9组。89个唯一返回来源的ID/文件/KB/正文SHA256/chunk_type均与线上SQLite逐项一致。三轮各题最终子块集合与之前冻结的10-child结构参考集合一致（该参考不是相关性Gold）。上下文分组实际source ID集合、正文及BM25分数与本次发布前各配对轮一致。代表chunkId有意改变，不作为漏召回依据。

| 题目 | 发布前中位数ms | 最终中位数ms | 节省ms | 节省比例 |
|---|---:|---:|---:|---:|
| terra-ecs-b5-013 | 470.47 | 624.97 | -154.50 | -32.84% |
| terra-ecs-b7-013 | 496.32 | 483.66 | +12.67 | +2.55% |
| terra-ecs-b1-024 | 413.03 | 342.23 | +70.80 | +17.14% |
| terra-ecs-b1-030 | 652.98 | 716.47 | -63.49 | -9.72% |
| terra-ecs-b6-024 | 536.64 | 512.98 | +23.66 | +4.41% |

五题中位数均值513.89→536.06ms，增加4.31%。全部15请求算术均值527.63→566.00ms。

本次为来源身份正确性改进，不宣称提速。响应新增完整命中/引用映射，体积增加；重启、缓存、时间顺序与远端embedding时延也影响计时，未做因果拆分。没有新增回答生成/Gold答案评分，因此不宣称QA正确率提升；原b6第二Gold来源映射UNKNOWN仍保留。

API最终PID1081639健康，队列ready/unacked0。请求期间最低MemAvailable1.87GiB；ECS磁盘可用12348076032→12331749376bytes。保留线上父块4000字符和总预算24000，没有将本地父块数量6策略混入本次发布。

复用已有SQLite、Chroma、虚拟环境、冻结题目/Gold。新增小型源码及配置回退、临时SQLite测试和原始JSON/日志；无语料、DB、Chroma索引副本，无新容器/依赖环境。保留v1中间部署和最终r2证据，性能表只用原始before3与最终r2 after3。

ECS最终证据：/opt/amrag/evaluation/parent-child-parse-20261111/io-reconcile-20261112/parent-identity-20261115-r2；原基线同级parent-identity-20261115。本地ecs/r2/remote-evidence。

机器可读：comparison.json、sql-source-audit.json、sql-source-expectations.json。


## 前端计数发布

本地KnowledgeBases.tsx类型检查通过；线上仅有dist，无可复用构建源码/依赖，未上传整个本地前端。对已核对线上JS做精确展示补丁，新增版本化asset index-BYfkyslh-identity-9b33143d191f.js并原子切换index.html引用，保留原asset和HTML回退。显示已核验的命中子块数/上下文组数，每组命中和引用来源数；旧响应映射不明显示待核对。node --check退出0；Nginx返回HTML/JS均HTTP200，实际JS SHA256与候选完全一致。未做浏览器交互/视觉检查。

前端证据：var/parent-source-identity-20261115/frontend-ecs；ECS frontend-identity-20261115。前端发布磁盘12336406528→12335534080 bytes，只有新asset和回退记录，无API重启。保留旧asset用于旧页面缓存和回退，不删除。
