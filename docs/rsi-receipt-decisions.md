# RSI 候选判定与下一轮派生

原故障题的冻结评分仍为 **FAIL**，进程退出 **1**。子代恢复了“季度 | 收入”和“单位：万元”，保留了“春季 | 120”。实际请求与历史健康臂逐字节相同。这个子代可以作为开发父代，本次决定为 **恢复完成，等待新的真实观察**。正式 E1c 数据 **BLOCKED**，业务效果 **NOT_RUN**。

## 下次怎样看一个总体 FAIL 的候选

在本机 PowerShell 运行以下整段。换题时把 `--run` 换成待审运行目录。若物化目录不是该运行的同级 `bundle`，加 `--bundle <目录>`。有健康参照时指定当初观察到父代故障的运行，当前实现核对该历史运行的 A 臂。没有可比参照时省略该参数。进程日志和退出码文件是一对可选的原始凭据。

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_offline.py review --run var/repair-round/run-local --reference-run 'D:\RAG-R0\var\r1\engineering-failure\run' --process-log var/repair-round/paired-local.log --exit-file var/repair-round/paired-local-exit.txt --store var/rsi-review --brief
```

输出逐题列出恢复、丢失、保留、仍缺的断言，两臂的未满足项、长度变化、健康参照、原始记录路径、下一步和“可作开发父代”。去掉 `--brief` 可取 JSON，包括各事实的命中成员、两份捕获的路径和 SHA-256。成功读取档案的命令退出 0，冻结实验的退出语义另列。没有进程收据时，“进程退出”为 UNKNOWN，“评分对应退出”仅表示旧 CLI 的确定性映射。

## 实测与历史回执

下表的路径相对本工作树，`R` 表示 `var/repair-round`，`C` 表示 `var/rsi-controlled/165518765a7f7bde02ba6ceb3cd7c2b07f2ddd8ade8ef70098e9d51a43febbf4`。配对行的证据均为题目录下的 `result.json` 及 `A/capture.json`、`B/capture.json`。

|运行与对照|冻结评分 / 进程退出|实际送达与断言|健康参照|下一步与证据|
|---|---|---|---|---|
|历史原题 6909b60ec24d4129bd232573fe2d1b6d，故障父 → 修复子|FAIL / 1|1/1 配对。父仅 row；子 header,row。恢复表头、单位，数值保留，丢失 0。上下文 32→72 字符，46→118 UTF-8 字节；消息正文也增加 40 字符|子 B 等于历史健康 A，现父 A 等于历史故障 B，两份完整捕获分别一致|恢复完成，可作开发父代。R/run-local/case-001，R/paired-local.log 和 R/paired-local-exit.txt|
|历史独立工程验证 1f7d36d478284866ab0118a833f78999，同文异位置|无整轮冻结等级 / 编排退出 0|2 题中 1 对。子保留 header,another-header,row；恢复表头、单位，数值保留。上下文 32→112 字符，46→190 字节|UNKNOWN，历史健康 A 没有这一输入|支持工程恢复；此夹具不能独立授权继承。R/independent-engineering-local/case-001，R/extra-local.log 和 R/extra-local-exit.txt|
|同一独立运行，归属缺失|同上|另 1 题在 A/B 前按预期阻断，缺 FileDocument ownership: f1，没有 A/B 目录|UNKNOWN|补归属；不记作子代失败。R/independent-engineering-local/case-002|
|本轮新运行 887240dec3e24e7aaf44aa9e0ca33fb7，复用原故障题|FAIL / 1（执行工具实测）|1/1 配对，外部调用 0；两份新捕获 SHA 与原题逐一相同|MATCHED_HEALTHY_CAPTURE|同样等待新观察。C/case-001。首次自动 decision.json 未指定参照，含参照的独立复判在下方决定对象中|
|另查历史超时 3a353e08c9fd41d4b271778fff2f558e，A 交付失败|FAIL / 原进程退出本次未取得独立凭据，评分对应 1|计划 1、配对 0、异常 1、预期阻断 0。逐题记录显示 A 约 90 秒超时|UNKNOWN|BLOCKED，不能据此选择开发父代。R/run/case-001/result.json|

原题的父臂违反三项断言（表头、单位、成员顺序），子臂无 faults。冻结规则还要求两臂等长，实测增加 40 字符。以上各项共同保留，未改分母或重标 FAIL。历史两次目标运行合计 3 道工程情形、2 对、1 个预期阻断、4 份捕获。本轮新增执行只有 1 道已知合成题，A/B 各一次、2 份本地捕获、外部业务模型调用 **0**。同一输入重复验证不计新增样本。

### 证据来源和边界

- 本机起点与远端可见分支均为 `09a3146d04e9c0c9f2cd1feed842c86e8af2ef74`，业务基线为 `90d86ce87e8c76c8c45f5f296160ddf53de71d54`。原分支为 `dots/rsi-work`。在独立本地分支 `codex/rsi-receipt-decisions` 将上轮五个未跟踪文件按原样固定在 `43fb9f65c927915307ef70fd49d94f6258827cea`，对该提交检查工作区差异为空。五个文件为 ROUND_REPORT.md、REPRODUCE.ps1、repair_round.py、extra_checks.py、audit_evidence.py。运行归档仍在忽略的 var 下。
- 先审阅了 REPRODUCE.ps1。它会执行两次编排，且 extra_checks.py 会更新既有 restore-check.json。本轮直接回读既有回执，仅为验证新增受控执行入口单跑原题一次。
- 本轮重算原生 feedback 并与已存版本比较，校验物化清单、候选记录、配置、worker、评分口径、冻结用例和逐题输入，重算原 assess 并检查实际请求内的偏移和正文哈希。历史审计列出的 4 个 capture SHA 全部回查一致。健康参照绑定源运行 ID、manifest/results 摘要、候选谱系、固定参数、评价与题集、逐题输入、共同父臂捕获。不同候选 ID 即使输出一致仍保留各自身份。
- 独立工程脚本只有 fixture、results、逐题和捕获，没有标准版本清单、feedback 或整轮冻结等级。夹具声明了父子 ID，本轮从请求复核断言。该脚本使用标准 prepare/deliver/assess 的依据来自归档源码，其执行环境没有独立的逐臂 worker 版本回执。
- 报告中的历史沙箱原因、上轮测试规模和全机资源盘点没有被当成本轮实测。必要事实只覆盖独立列明的断言；回答正确性、真实推理延迟和费用本轮没有测试。

## 实现、兼容和恢复

- `scripts/rsi_decision.py` 从已评分回执计算直接父代、健康参照、执行完整度、开发父代资格和下一步。恢复与损失同时出现时显示两者；发生明确退化时拒绝将此次收益作为选择依据。仍缺少共有事实但有有效回执时可以继续开发，缺少配对或必要断言时保留 UNKNOWN。没有冻结晋级规则时输出 NOT_EVALUATED_NO_FROZEN_GATE。
- `scripts/run_rsi_offline.py` 保留 `--manifest ... --out ...`、原评分器和原退出码。注册候选完成运行后自动保存 decision.json。`review` 从原入口只读复判，`--store` 把结果作为不可覆盖的内容寻址对象保存。`--archive-root` 让同一个归档根中的同一冻结提案使用确定的目录；再次调用回读已有完整结果，返回原评分对应退出码。若目录只写了一部分，保留现场并退出 2。显式新建运行仍使用旧 `--out`，应为有意发起的新尝试。
- `scripts/rsi_candidates.py` 继续沿用 register、derive、materialize。可在 derive 增加 `--decision <决定对象或 decision.json>`。程序复核决定所绑定的原始证据，要求父代为被评的子 B，观察属于有效配对且适用于 B；该路径一次只改 selector 或配置。相同提案的内容寻址身份不变，不同假设可有不同子代。预算来自候选已绑定的模板（物化时校验）；本轮没有扩大预算。
- 若负责人要为新提案冻结一个**工程**晋级条件，derive 可显式加 `--engineering-gate all-paired-safe-with-measured-gain-over-reference-v1`。条件进入候选哈希：要求全部计划题有效配对，子代无 faults，对直接父代无事实损失，各题有可比健康参照，至少一题在该参照上增加已测必要事实。该工程判断与正式业务效果分别记录。历史提案未设此门。
- `backend/tests/test_rsi_decision.py` 覆盖部分恢复仍缺事实、恢复同时发生回归、健康参照与冻结条件、缺失断言、篡改决定及父子观察错配。旧 feedback 格式与旧候选哈希沿用。

**从观察到下一轮。** 历史来源运行 `f7a6792cd94b4dc79f868c73b44a82d0` 的 `obs-e513126ba2bd0d1f9cdbf162` 记录了故障子代丢必要事实。上轮开发代理提出只移除 `return selected[-1:]` 的切片。既有 registry 按源码、配置、来源和假设得到父 `0f178f4d77c2532e322f991ef5bd733ee8a371bb61dbd9d55de439d26358117e` 和子 `5ce3ad183a3980518f61a76edb2767a701097f3db331c7be69ca7768de061eab`。本轮 Astra 提出把“冻结断言失败”和“子代证据恢复”分别判读；没有提出新的业务变异。程序用上轮实际观察和原始提案重新派生出同一个子 ID，registry 仍为 20 个对象；独立物化的 13 个文件逐字节一致。运行、配对、断言复核和决定由程序执行。

以后有新的真实观察时，先在 review JSON 的对应题找到 `child_observations`，对照 feedback.json 中同一 ID 的事实，再提供单项变更及反证条件。把本次决定对象交给现有命令，如下为接口契约：

```text
rsi_candidates.py --registry REG derive --decision DECISION --from-run RUN --observation-id OBS --parent CHILD_ID --config CONFIG --worker-patch PATCH --observation OBSERVATION --hypothesis H --change CHANGE --impact IMPACT --falsification F
rsi_candidates.py --registry REG materialize --parent CHILD_ID --child NEW_ID --out NEW_BUNDLE --engineering-fixture --cases FROZEN_CASES --evaluation FROZEN_EVALUATION
run_rsi_offline.py --manifest NEW_BUNDLE/manifest.json --archive-root SAME_LOCAL_ARCHIVE_ROOT
```

只给真实新增观察派生候选。归档损坏或超时，先读 case-NNN/result.json、worker.log 和已存在的 capture；从 registry、相同 cases 与 evaluation 物化到新目录恢复，保留原运行目录和结论。旧手动指定输出位置的运行须显式用 review 复用，受控入口不跨磁盘查重。

## E1c 就绪审查

沿用 `var/repair-round/pdf-readiness-audit.json`；本轮没有收到新来源材料。以下为已有授权盘点范围内的判定。

|材料|已有证据|尚缺|
|---|---|---|
|PDF 源哈希前缀 40a74d3f、43be9592|各自有原 PDF|没有已存区域解析；两者均无能独立绑定索引版本的知识库归属|
|PDF 源哈希前缀 8f49f93f|已有 202 块，其中 192 块带 regions|满足该候选严格顶层定位的块为 0；同样缺独立的文件→知识库→索引映射|
|可删副本与评分|已证实含同源、同位置、同内容、非独立命中副本的业务题 0；独立审定必要字段、表头、单位及定位完整的业务题 0|需要冻结一个实际命中种子、全部展开成员及独立评分的 PDF 题|
|留出集|可确认未用于设计的文档或会话 0；反复审查的 Office 六题是开发材料|需要独立留出文档或会话，及其冻结标注|

最小补料为独立的 file_id、KB、源哈希和索引版本映射，一道含独立种子和同位置非种子副本的 PDF 成员快照（页、bbox、parent、heading、正文哈希），独立审定且带原文位置的字段/表头/单位，以及未参与设计的留出样本。**E1c 数据 BLOCKED，正式效果 NOT_RUN**。

## 执行记录

|本轮命令或检查|退出码|结果|
|---|---:|---|
|上方 review 命令；同入口复核 independent-engineering-local|各 0|报告前两条历史运行的判定。原退出码分别由对应进程日志和退出码文件提供|
|run_rsi_offline.py --manifest var/repair-round/bundle/manifest.json --archive-root var/rsi-controlled|1|新运行完整配对；符合原评分 FAIL 的退出契约|
|再次运行完全相同命令|1|直接复用 887240de…；对归档全部文件前后求哈希，一致，无新增请求|
|原观察重新 derive，独立 materialize、verify_arm_sources，再核验历史 4 份 capture 摘要|0|候选 ID 相同，13 份物化文件相同，20 个旧对象未新增|
|原题已归档决定 authorize_derivation|0|子代及该运行观察 obs-fb0e7c624b74b16fb209babe 匹配；没有因此新建子代|
|python -m pytest -q backend/tests/test_rsi_decision.py backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_offline_runner.py --disable-warnings|0|50 passed。日志 var/rsi-review/tests-final.txt。属于单元/契约测试，独立于业务题数|
|git diff --check；git diff --exit-code 90d86ce -- backend/app|各 0|改动局限于离线工程|

本地只读使用原有运行和 venv。新增一份单题合成归档及复判对象，验证后移除临时物化目录。观测时 var/rsi-controlled 与 var/rsi-review 合计 217,951 字节；E 盘可用空间由 56,975,200,256 变为 56,974,376,960 字节，含同时运行的其他进程。新回放用于验证接线，末次命令行及判定细节收尾后再用最终代码回读，未为这些变化重发 A/B。没有推送或部署。
