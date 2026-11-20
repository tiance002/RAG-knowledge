# RSI v2.2 P-A 只读审计

日期：2026-11-15。审计输入为用户新附件 `e7767e71-12e9-44b5-8631-7c0b4192a21d/pasted-text-1.txt`，原始 SHA-256 `dfe4f5703730096dde128c50b81a69b6b707e274f0f41120c25203bc73f7049e`。

**审计调查完成，存在停止项；不表示基座或实验闭环通过。** 随后用户明确取消逐阶段人工等待，要求自行测试后推进；本报告仍保存接口限制。后续允许通过新增受控适配层解决可验证的工程缺口，不能修改原 AST/来源/血统保护。真实生产操作仍未授权。

## 1. 实际工作树

|路径|用途及当前版本|只读状态快照|
|---|---|---|
|D:/RAG knowledge|用户当前产品仓库；codex/bm25-stats-write-optimization-20261112；7c7415f85a063e3f7645f676bd22f7c8f766a732|51 tracked 变更、1348 untracked 条目；backend/app 11 个未跟踪文件。不是冻结回放业务根。|
|D:/RAG-R0|同仓库的 detached 工作树；90d86ce87e8c76c8c45f5f296160ddf53de71d54|1 tracked 变更（scripts/run_e20_delivery_ab.py）、25 untracked 条目；backend/app 对本提交 diff=0、untracked=0，可作为待运行验证的业务根。|
|E:/codex_workspace/2026-11-03/task/rsi-work|现有 RSI 所有者；codex/rsi-receipt-decisions；aeb2c89bc66d724472621c581107093d7dd2ad32|14 tracked 变更、37 untracked 条目；包括既有 runner、registry 扩展和旧轮新增文件。不能重置。|
|E:/RAG quention|独立项目 main；0606063ad972c167de759f70137e9c1b07cd4b4f|1 tracked 变更、231 untracked 条目。此仓库不存在 R0 提交，ancestor/diff 返回128；不是同一 RAG 产品或回放业务根。|

状态是审计时快照，其他会话可能继续工作。上述工作树身份来自实际 `git --no-optional-locks rev-parse/branch/status/worktree list`，没有根据旧说明混同路径。目标是既有 RSI 所有者，产品仓库只保存任务计划与审计入口。

## 2. 冻结字节

实际 RSI 所有者下的原文件 raw SHA-256：

|文件|SHA-256|相对 HEAD|
|---|---|---|
|scripts/rsi_candidates.py|2f537753f740ce832790f25b2af2de6d3e96563e6b18d50b2d5108d25beb36a4|已有修改|
|scripts/run_rsi_offline.py|9b58b19149c08768ea865314fc0431d1ec0bbd8f46291ba2099ce18dfbe8c81f|已有修改|
|scripts/rsi_feedback.py|5e16f1d153ac936cff3a54530f87b1bc8708544cd2481ea3b359a254383942cb|相同|
|scripts/rsi_delivery_worker.py|a5f793b9f8f6ad7908c78580a670c0cc5c909898dc8e4c36d05fdbc2a330d3da|相同|
|scripts/replay_source_preparation.py|484274d899761eb0641a2ccc15886b8ae6b70dd242a0fb53f6f5adef9c49c020|相同|
|scripts/rsi_decision.py|b8f0d28211aca4ffb01b9f0c3bb66bc4ffd4669193430ac49a5acd86a1512fa1|相同|

审计前后还核对了 run_rsi_loop、rsi_roles、rsi_roles_loop、旧 rsi_foundation，共 10 个文件，全部字节不变。审计没有导入 worker、运行测试/回放、调用供应商、读取密钥值或改变服务。仅报告落盘不属于源码修改。

## 3. Registry 与血统：已验证源码及存量对象，耐故障未验证

实际公开 Python 接口，均在上述 rsi_candidates.py：

```python
register(registry, manifest_path, run)
derive(registry, parent_id, config_path, worker_path, notes, from_run, observation_id, worker_patch=None)
materialize(registry, parent_id, child_id, out, cases_path=None, evaluation_path=None)
verify_arm_sources(manifest, manifest_directory)
```

CLI 第365行起实际支持 `--registry ... register-r0 --manifest ... --run ...`、`derive --parent --config (--worker|--worker-patch) --from-run --observation-id`，以及五个说明字段；`materialize --parent --child --out`。替换 fixture 还必须同时提供 `--cases --evaluation --engineering-fixture`，不能拿旧路径猜参数。

- `put/get`（21/34行）使用内容寻址和 `xb` 独占创建；相同对象存在时比对字节，读取核对文件名摘要。没有 fsync、原子临时文件提交、事务或工作区锁；中途失败会留下部分对象，不能声称可恢复。
- `register`（68行）要求原 run manifest 一致、summary PASS/failed=0/外部调用=0、行数和每行期望通过、worker/版本/config 匹配。先验证后的多次 put 不构成跨对象事务。
- `derive`（139行）核对真实 feedback 重算、唯一 observation、run/qid/results 摘要及行绑定、被观察臂与父候选、存档 worker/config 身份。精确补丁为 `{old,new}`，old 必须在父 worker 字节中恰好出现一次。
- 空 worker/config 改动拒绝 `child must change code or configuration`。但 derive 在 seam/空改动检查之前已 put observation 的归档对象（196–205行），静态拒绝也可能留下未引用对象。新增流程必须先完成安全静态检查，再调用原接口；不能承诺原接口的多文件事务性。
- `materialize/verify_arm_sources` 核对 parent/child、template/cases/evaluation 等继承和冻结字段。普通血统锁定 fixture；显式工程替换始终带 `engineering-only; no comparison to R0 results`。已带替换的观察必须继承同一工程 fixture。
- 本次只读扫描 `var/repair-round/registry/objects`：46 个对象、摘要不匹配0；10 个 rsi-candidate-v1 的 worker/config/template/cases/evaluation/直接 parent 引用全部存在。另有7个质量域候选和2个历史 observation，不能混成 Context Selection 候选。

**模式 B 根：源码层有条件支持，实际未验证。** 原生 R0 格式允许新 cases/evaluation，20题上限；先产生事实完整、无固定序列/长度方向的 passing R0，再经 register-r0 登记。不能把旧故障子代或工程替换当新 B 根。必须在 P-1 跑真实接口证明，而不是仅看 schema。

**独立 holdout：高层现成路径阻塞。** 普通 materialize 不许换 fixture，已有替换只能标工程用途；非r1回放默认用共用 WORKER，不能偷偷代入任意候选。可行性建议：独立适配层先用原 verify_arm_sources/validate_source_roots 验证训练候选，再调用原 child/checked_case/execution_view/assess，在独立授权目录产生血统外核查证据；这是否完整保留保护条件必须测试证明。不得伪造 registry 血统、修改旧 manifest 或冒充原生训练结果。

## 4. Seam、调用白名单与隔离

唯一函数为 `select_member_indices(members, chunks, files, seed_ids, selection)`，worker第130行。原 AST guard 在 rsi_candidates.py第112行：移除函数体后整个 AST 必须相同；签名/其他模块逻辑不能变。禁止 import/global/nonlocal、危险名称和 dunder；允许名称调用：

```text
require duplicate_key any all range len enumerate list tuple set sorted min max sum abs
```

原 guard 仅按属性名允许 `.append/.get`，并非完整只读效果证明。旧 run_rsi_loop.py第29行还有更严的 validate_policy：仅参数/局部变量/允许 helper、禁止对象写入、append 限定私有新列表。两层需共同复用；原 derive 本身不自动调用后者。当前流水线未发现完整“编译+定向测试”静态阶段，不能把 ast.parse 等同编译。

**冲突：保留 P3 不等于现有接口允许重排。** worker 文档要求按原序返回，runner assess 第83–86行把任何重排判为 `invalid_selection_order_or_membership`，旧 decision 亦将它列为 critical。保序守卫属于不能放宽的保护边界。P3 可保留角色和弃权/拒绝证据，但不可能声称一个真实重排序提案在当前口径下通过；若要求实际重排成功，需另行明确授权版本化守卫变更。

worker 使用真实 SQL 扩展和普通 ChatService 流程，但替换检索、会话、生成供应商及 spreadsheet grounding；生成是客户端本地替身，不运行真实模型/Chroma检索。prepare 使用全新内存 SQLite；来源归属必须显式提供，缺归属拒绝、不从检索结果猜测。

**隔离为阻塞项。** runner.child 第54行为子进程设置环境白名单、内存数据库和90秒 timeout；worker覆写部分 socket API 禁网。这是 Python 进程约束，不是 OS 网络隔离/只读挂载/低权限身份，也没有 CPU/内存/输出量上限、进程树终止或可核验 ACL。继承 HOME/USERPROFILE 等路径不等于可以读取生产凭据，但同账户没有 OS 文件权限屏障，不能证明不能读取。本机 D/E 均 NTFS、Healthy；未做锁竞争、目录fsync、杀进程或容器沙箱测试。

## 5. 回放、数据 schema 与实际交付证据

`run(manifest_path, out, business_root=None)` 是现有真实回放接口（174行）；CLI没有 business_root 参数，Python接口可指定 D:/RAG-R0。validate_source_roots 要求冻结提交为业务根祖先、backend/app 无 diff 和未跟踪影子文件；r1还核对worker和source helper与297e66ed...提交字节一致。

原 `FIXED`：window_size=1、max_parent_length=4000、rewrite=false、rerank=false、router=OFF、provider=local_request_capture。`EVALUATOR=delivered-source-facts-v1`。两者需原样冻结。

- manifest 必需 experiment_id/status/base_commit/evaluator_version/fixed/cases/evaluation/arms/budget；arms必须A/B，B.parent_id=A.candidate_id。cases是 `[{qid,case}]`，evaluation按qid唯一配对。artifact 的 sha256 是原规范化 JSON 指纹，**不是原文件 raw SHA**。
- case 包含 question/allowed_kb_ids/files/chunks/seed_ids/expected_seed_count。worker要求 chunk.original_index 为非负整数，同文件序号唯一；不是只要块正文+hash即可。registered路径用checked_case进行公开投影后执行。
- evaluation[qid] 至少 status；正常为PAIRED；facts 项至少 kind/file_id/text。members和length_change均为可选，故可构造不固定方向的基础模式B。forbidden是对所有模型消息正文的精确子串检查，不是语义禁止来源规则。
- 事实核对为“指定file_id的任意已交付成员包含text”。kind仅作标签；未校验表格对象/关系/条件/异常/原文位置。同一文件其他不相关成员出现数字可假通过。原核对器不修改；额外谓词需从受控sidecar和真实capture证明具体来源区间。
- capture保存baseline_members/members/messages/context/proof、request_calls、上游摘要。proof含member ID及context/prompt起止；能证明本地客户端收到的消息内容，不能证明真实外部模型的生成质量。
- 本次只读检查3个已有工程归档的8份capture，共14个member/proof条目，成员ID与context/prompt区间全部相符（8/8 capture）；存在source_locator字典不等于独立原文核实。未回放或重新裁决这些样本。
- 原字符数为 `len(context)`（Unicode码点），不做Unicode规范化；context含编号、来源文件名、location、成员正文及双换行，去掉固定指令prefix。不可悄然改成仅正文长度或字节/token；P-1应冻结该原口径。
- prepare BLOCKED 可被missing_file_id断言认作预期PASS；原3题工程集有1个BLOCKED，33.3%，**不符合新模式B<=10%的PRE_BLOCKED规则**。运行失败在原summary.failed中混入质量失败，必须由新增裁决层依据原证据区分infra/候选缺陷，不能照搬FAIL含义。
- out已存在拒绝覆盖；按题save结果，结果不全不会自动按题续跑。archive-root仅在已有decision可复判时复用，否则保留不完整归档并退出2；没有新方案的intent/receipt/checkpoint事务恢复。

## 6. 预算、依赖、模型与时间

原离线预算 `0 < len(cases) <= max_questions <=20`，external_calls=0、attempts_per_arm=1。每题prepare一次、A/B各deliver一次，20题为60子进程/40题臂；3候选为180子进程/120题臂。每次child timeout=90秒，但无300秒bundle或1800秒整轮deadline；按每次上界串行可能5400秒/bundle，不能把它当实测耗时。已查3个历史归档的results/versions无可靠开始结束/耗时，P50/P95 **未验证**，没有用mtime或模型耗时替代。

Python 3.13.0，复用D产品.venv；按现有dist-info目录核查pytest8.3.5、httpx0.28.1、SQLAlchemy2.0.55、pydantic2.14.1、openai3.40.0、filelock4.2.7。这是静态安装记录，不是import/实际兼容性测试。旧角色transport timeout=180秒且未设置temperature；旧header-roles配置为10次/8192输出tokens，不符合原新方案默认，但用户后续已覆盖调用次数/费用规则。

最新用户规则：OpenAI、复用现有密钥、禁止Astra、测试成本优先；调用次数无限；本任务累计5美元、不预留费用、最后单次允许超支，usage缺失必须UNKNOWN。密钥仅检查存在，不复制不打印。默认可优先同一低价Luna的独立任务；模型能力与价格以实取官方页面为准，实际账户可用性仍需GET models验证。费用/usage记账与调用失败路径需在实现中测试。

官方已查页面：
- https://developers.openai.com/api/docs/models/gpt-6-luna.md
- https://developers.openai.com/api/docs/models/gpt-6.1-sol.md
- https://developers.openai.com/api/docs/guides/latest-model
- https://developers.openai.com/api/docs/pricing

Luna支持reasoning none及结构化输出；其标准价格0.10/0.50美元每百万input/output tokens。Sol为2/10，推理不为none时不支持temperature；因此不能为满足temperature=0随意组合不支持参数。低价Luna优先且保存响应，不声称再次生成相同。

## 7. 数据、保密和生产

当前原文盘点：旧6题readiness仍BLOCKED；旧黑盒8题4源80块，其4个源文件存在且raw SHA均匹配，authorized=true只是既有声明，不自动扩展为本任务云端原文外发授权。QA portable草案156条均UNASSIGNED、至少有多份不同source SHA，但整体状态DRAFT_NOT_FROZEN_NOT_ASSEMBLED，不能直接当20题/8簇的已审定新基座。只取经过本任务重新核验的真实问答与原文，另记自检审核来源，**不得伪造HUMAN审核**。

原e1c-readiness SHA `c0774a228e1b724a4883681866416cc3c3bfdfcb89b77f1312b973747ca1662a`；旧8题inputs SHA `6069a6d72ae63ae324ccfa344bc7e86b0b5c523ccf469af0f49145b6adeaa904`；QA草案SHA `83dffc54cc9d8ac509b391fe9420f54cd34a9f3ee0ab76d3cc4b729935776f24`。不把6+8+156加成有效独立样本。

旧roles.evidence没有新规范的逐级随机别名投影，含parent/run/observation ID和成员ID；虽然未直接放事实原文，也未证明Unicode哨兵、重试和手册泄漏测试。旧经验为append JSONL和UNVALIDATED_CANDIDATE；没有新方案事务事件、冲突隔离、同episode计数和ACTIVE资格，不能直接沿用。

当前没有已核实的源簇独立、封存权限、一次性消耗holdout；没有读取或向模型发送秘密holdout答案。生产当前版本、已批准候选、CAS发布、实际回滚对象及在线观测 **未验证**。旧`promotion()`仅返回PASS_ENGINEERING_ONLY等标签，不部署。产品目录本次未发现可证明RSI候选审批/CAS/回滚的实际路径；不据此宣称整个系统没有任何运维脚本。真实生产保持阻塞，后期只能先做隔离演练。

## 8. 现有测试的安全执行范围

本次P-A **NOT_RUN测试，NOT_RUN回放，模型请求0**。静态确认：candidates有6个test函数、decision5个、feedback10个、offline_runner6个、source_preparation6个、business_source2个、roles9个；这些是函数数，不是参数化收集总数。

后续建议只在独立测试目录执行candidates/decision/feedback/roles/business_source定向测试。它们有合成fixtures和替身，但会写临时文件/registry、部分拉起子进程；角色transport必须保持monkeypatch，不能去掉替身造成真实调用。source_preparation导入真实app，需要隔离环境/数据库设置。原offline_runner两项生命周期测试默认把dirty RSI root当业务根，会被保护检查拒绝；不能为绿灯删除保护或清理用户改动。

上一轮保存的existing-regression.log原始结果为32项、30pass/2fail（退出1），SHA `78ba9cae50d214078f83fb2cf305c41fbe39f7a18649895f85b33211212c0bf1`。原始错误含 `git ... diff --quiet aeb2c89bc66d724472621c581107093d7dd2ad32 -- backend/app` returned non-zero exit status 1。它是历史日志，本次没有重新执行，也不合并为P-A通过项。

## 9. 上轮文件与新方案差距

旧新增rsi_foundation.py尚未接入原回放；它的evaluation格式没有原status/file_id契约、测试块未要求original_index、缺exception事实类型、>=8簇、holdout/权限/完整来源谓词。其DATA_READY仅代表其自定义schema通过，**不能等同原回放READY或新P-1验收**。保留原文件和25项历史合成测试，不覆盖它们冒充新实现。新增组件应置于rsik，通过经核验的原接口适配。

## 10. 可复核命令、错误与收尾

实际只读命令包含：git --no-optional-locks rev-parse/branch/status/worktree/diff/merge-base/ls-files/show；Get-Content及带当前行号读取；Get-Volume D,E；Python -B标准库hashlib/json/ast读取对象/归档（没有import被审计模块）；.venv python -B --version。各静态Python检查退出0。D-R0基线ancestor/diff为0/0，RSI为0/1，产品D为1/1，独立E项目为128/128；这些是核查结果，不隐藏为一律成功。对不存在D产品experiments目录、E独立项目config/monitoring的rg曾返回路径错误，后续改用实际拥有目录；输出过长处改为分段和摘要，未从截断输出推断测试通过。

仅复用现有工作树、venv、registry与只读归档；未安装依赖、创建/停止容器、复制数据/索引或删除资源。新增本审计报告并保留旧证据供后续复核。初始D used/free=162000117760/132972695552字节，E=450594238464/37499011072；审计读取结束D相同，E=450593411072/37499838464（报告落盘前整盘观测，不归因于本任务）。本阶段没有待清理数据副本或下载文件。

结论：P-A证据调查已完成，P-1仍未通过。关键缺口为原保序与P3冲突、血统外holdout适配未验证、附加来源谓词、OS隔离/锁/恢复和真实20题基座。依最新用户指令，不再等待阶段人工审查；只在自检通过后推进，不能因取消审查而把未通过项目写成完成。
