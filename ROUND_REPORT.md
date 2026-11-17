# 天策首轮：工程恢复，未晋级生产

已从真实失败候选派生一个修正候选，恢复表头和单位，保留数值行，仍省去同来源同位置的非种子重复副本。此为恢复既有正确规则，不能据此宣称业务质量提升。

## 起点与复用

- 独立目录：`E:\codex_workspace\2026-11-03\task\rsi-work`，HEAD `09a3146d04e9c0c9f2cd1feed842c86e8af2ef74`，由本机克隆 fetch 固定提交建立，保留祖先。`backend/app` 对业务基线 `90d86ce87e8c76c8c45f5f296160ddf53de71d54` 无差异，收尾再次验证退出 0。
- 原目录 `D:\RAG knowledge`、`D:\RAG-R0` 均脏，保留原状；收尾 Git 状态项分别 53、16。未写原 registry、语料、索引或配置，没有提交、推送、ECS 操作或生产切换。
- 复用 Python `D:\RAG knowledge\.venv\Scripts\python.exe`，3.13.0 / 64bit，及 `E:\RAG quention\.venv\Lib\site-packages`。没有建 venv、安装包、下载模型或重建旧 R0/R1。
- 将 R1 小型内容寻址 registry 复制到独立目录；全部对象摘要核验，旧失败 feedback 由实际归档重新计算并完全相等。旧 run ID、题、父臂、worker/config 和 observation 均经原 derive/materialize 校验。五个历史就绪输入摘要也全部相符，见 `pdf-readiness-audit.json`。

## 候选与可反驳假设

来源运行 `f7a6792cd94b4dc79f868c73b44a82d0`；损失观察 `obs-e513126ba2bd0d1f9cdbf162`，类别 `required_fact_loss_vs_parent`。

父候选 `0f178f4d77c2532e322f991ef5bd733ee8a371bb61dbd9d55de439d26358117e`。

子候选 `5ce3ad183a3980518f61a76edb2767a701097f3db331c7be69ca7768de061eab`。

唯一执行代码变化位于物化 `A_worker.py` / `B_worker.py` 的 `select_member_indices`：

```diff
-    return selected[-1:]
+    return selected
```

假设：恢复已验证的选择结果可恢复独立结构，同时仅删除可验证的非种子同位置副本。反证：任何必要字段缺失、独立种子被删、异位置同文被删、来源/顺序或捕获正文不一致即推翻。候选不接收 qid、评分和 Gold；没有按题号、固定位置或夹具正文分支。修正 worker 与固定提交正确 worker 逐字节相等。

原工程夹具 cases/evaluation 原样显式传给 materialize，未替换，未放宽身份或评分。管理器与业务源码均未修改。新增三个编排/审计 Python 文件和复现脚本；实际候选源码保存在 registry、bundle 及运行 implementation 中。

## 逐题实际送达

|范围 / 题|父 A → 子 B 顺序|字符 / UTF-8 字节 A → B|实测结论|
|---|---|---|---|
|原绑定 engineering-selection|row → header,row|32/46 → 72/118|春季 120 保留；季度/收入表头、万元单位由缺失恢复；copy 仍省略|
|独立夹具 different-location|row → header,another-header,row|32/46 → 112/190|两个正文相同、bbox 不同的表头均保留，数值及单位齐全|
|独立夹具 missing-owner|两臂前 BLOCKED|无请求|缺 `FileDocument ownership: f1`，无 A/B 目录|

所有交付成员均为 public / f1；正文从实际 system 消息的证明偏移截回，与成员内容及 SHA-256 一致，顺序与来源已核对。首题子代实际消息与历史正确 A 的完整 capture 逐字节相等；现父代与历史故障 B 的完整 capture 同样相等。首题去掉的是 `copy`：与独立种子 row 同正文、同文件/KB、同页同 bbox；不是删除独立事实。相对历史全量 delivery 的 106 字符/166 字节，恢复后仍少 34 字符/48 字节；该数值引用已核验历史 R0，不额外重跑或计入本轮分母。

原题原评分运行 ID：`6909b60ec24d4129bd232573fe2d1b6d`，路径 `var/repair-round/run-local`。**原 runner 退出 1 / FAIL，paired=1**：父代缺表头、单位及预期成员，且冻结评估要求 equal 长度而恢复后 +40 字符。子 B 无 faults，3/3 项实测事实 PRESENT。保留原断言与 FAIL，不将其改成全局 PASS。

独立工程运行 ID：`1f7d36d478284866ab0118a833f78999`，路径 `var/repair-round/independent-engineering-local`，编排退出 0；1 个配对、1 个预期阻断。它调用原 runner 的 prepare/deliver/assess 和相同物化源码，单独标明工程范围，未伪造成原继承 manifest 的运行。父代损失保留在逐题 faults 中，子代无 faults。

本轮逻辑总分母 3 题：2 配对（父代均有已知事实损失）、1 BLOCKED；实际末端本地请求捕获 4 份，外部模型调用 0。有效题每臂各产生 1 份捕获。来源准备 3 题，旧运行重建 0；工程回归单独记账。不得将失败或阻断移除后声称总分通过。答案质量、token 成本与业务收益均 NOT_RUN/UNKNOWN。

## 执行失败、退出码及回归

- 初次沙箱配对 `3a353e08c9fd41d4b271778fff2f558e` 在 A deliver 约 90 秒超时，退出 1，paired=0；独立夹具初次也在 A 超时退出 1。这两次无请求 capture，现场保留于 `run`、`independent-engineering`，不冒充预期事实损失。环境诊断后在可用本地 Windows socket 的获准执行环境重跑到**新归档**，原网络禁用 worker 未改；有额外中止尝试，不能宣称从未重试。
- 首次回归 `19 passed,32 errors`：pytest 默认沙箱临时根不可写。改为工作区 `--basetemp=var/repair-round/pytest-temp` 后，**51 passed,32 warnings，退出 0**。原始失败日志 `regression.log` 与成功 `regression-2.log`、退出码都保留。警告为既有依赖弃用警告，无跳过。
- derive/materialize/来源核验退出 0；恢复核对 `restore-check.json` PASS，重新物化 13 个文件逐字节一致，不再次交付；`receipt-audit.json` PASS，审计命令退出 0；`git diff --check` 退出 0。
- 已审查未截断的落盘日志与逐项结果；终端曾有无关长目录枚举截断，没有据该枚举计算测试结果。

## 复现与定位

在本机具备正常 Windows 本地 asyncio socket 的 PowerShell 中：

```powershell
& 'E:\codex_workspace\2026-11-03\task\rsi-work\REPRODUCE.ps1'
```

该脚本生成唯一的新目录，原评分 runner 预期仍返回 1，之后独立工程检查返回 0。脚本可审查全部命令。主比较的直接命令为：

```powershell
Set-Location 'E:\codex_workspace\2026-11-03\task\rsi-work'
$env:RAG_TEST_EXTRA_SITE_PACKAGES='E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_offline.py --manifest var/repair-round/bundle/manifest.json --out ('var/repair-round/repro-'+[guid]::NewGuid().ToString('N'))
```

`identity.json` 定位完整父子 ID；`bundle/B_candidate.json` 定位假设和失败 observation；`run-local/feedback.json`、`case-001/result.json` 与 `A/B/capture.json` 定位事实与实际请求。`receipt-audit.json` 提供四份捕获路径和原始 SHA。`repair_round.py` 记录这次注册表复用、派生和物化过程；再次初始化需全新独立输出，不能覆盖本次档案。

## PDF 就绪盘点与下一项

只读复查已有 `var/corpus` 三份 PDF（逐个文件 SHA 与文件名一致）和已存 native-paired-v2 解析，不重新解析、不复制语料、不访问 ECS。该范围不是全盘完备证明。

- PDF 总源样本 3；有既存区域定位解析的源样本 1（202 chunks，其中 192 有 regions）；达到本候选所需可信归属及严格完整定位的样本 0。
- 证实存在可删非种子同位置副本的样本 0；缺独立冻结 E1c 逐题评分的源样本 3；READY E1c 题数 0。regions 元数据不能直接当作本规则要求的顶层 page/bbox、parent_id、heading_text，也不凭原文件相同推断成员重复。
- 历史六题仍是 7 DOCX + 3 XLSX，不能触发该 PDF 候选，且原文评分和独立归属仍缺。现有历史输入摘要五项全部核验匹配。

**正式 E1c BLOCKED / NOT_RUN，按要求停下。** 最小补充：获准的 file_id↔KB↔源哈希↔索引版本映射；带独立 seed、全部展开成员、正文哈希、页/bbox/parent/heading 的冻结 PDF 输入，至少有一份可删的非种子同位置副本；独立审定数值、表头、单位、原文位置和未用于设计的留出题。未具备这些材料前，不给出虚构开发/留出划分。Word/Excel 作为独立后续候选。

## 资源收尾

没有容器、新依赖环境、语料或索引副本。保留小型 registry、固定提交源码、运行归档、失败现场、四份实际请求、原始日志及恢复证明，供审查复现。删除本任务 pytest 临时 fixture 目录，已先核对绝对路径包含于独立工作区；路径和字节见 `cleanup.json`。工作区收尾文件合计约 6.0 MB（含 Git 与日志）；整体磁盘测量仅作观察：开工 D 已用 152392122368、E 已用 431103930368 字节，清理后 D 152392126464、E 431118454784 字节附近，受其他进程影响，不能当任务净增长。
