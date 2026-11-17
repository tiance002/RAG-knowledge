# RSI v2.3 S7 最终 CI 审计记录

- 基线 head：`d47e9a37505a0a726e320120377d62bd4a1e8ddb`
- 记录日期：2026-11-16
- 说明：以下八 Job 的 518/518 明细来自独立审查记录；本次未重复运行。Run 与 Job 的 `conclusion=success` 已通过 GitHub API 复核。

## 1. 最新两个 Run

| 触发 | Run ID | 链接 | 结论 |
|---|---|---|---|
| Push | 43894292539 | https://github.com/tiance002/RAG-knowledge/actions/runs/43894292539 | success |
| Pull Request | 43894305537 | https://github.com/tiance002/RAG-knowledge/actions/runs/43894305537 | success |

## 2. 八个 Job 的统计

每组均为：collected 518、passed 518、failed 0、skipped 0、errors 0、pytest step conclusion success。

| Run | OS | Python | collected | passed | failed | skipped | errors | pytest step |
|---|---|---|---|---|---|---|---|---|
| Push 43894292539 | Ubuntu | 3.11 | 518 | 518 | 0 | 0 | 0 | success |
| Push 43894292539 | Windows | 3.11 | 518 | 518 | 0 | 0 | 0 | success |
| Push 43894292539 | Ubuntu | 3.13 | 518 | 518 | 0 | 0 | 0 | success |
| Push 43894292539 | Windows | 3.13 | 518 | 518 | 0 | 0 | 0 | success |
| PR 43894305537 | Windows | 3.13 | 518 | 518 | 0 | 0 | 0 | success |
| PR 43894305537 | Ubuntu | 3.13 | 518 | 518 | 0 | 0 | 0 | success |
| PR 43894305537 | Ubuntu | 3.11 | 518 | 518 | 0 | 0 | 0 | success |
| PR 43894305537 | Windows | 3.11 | 518 | 518 | 0 | 0 | 0 | success |

同一测试选择在两个 Run、四个环境各执行一次；不得把八次执行写成 4144 个独立测试。

## 3. 退出码口径

- GitHub 原始日志未打印数字形式的 pytest 退出码；准确记录为 Job success、pytest step success。
- 本地干净环境同口径执行（518 tests）退出码为 0，日志：
  - `local-ci-selection-518-r1.log`，sha256 `b39a2dad9701d1e959d2de29ef26a41ec25ffc84e9fecf4d3a544e3d4316acec`
  - `local-ci-selection-518-r2.log`，sha256 `4e4b3035d712214cabe471fe21612618276c08522cd082cc1ba7cc31e16f7c14`

## 4. 历史失败链（保留）

| 提交 | 结果 | 根因 | 修复 |
|---|---|---|---|
| `d3e7651` | 4/4 Job 失败；每 Job 2 failed / 516 passed | 便携 CI 只安装 pytest，冻结 worker 缺 `sqlmodel` | 安装冻结 worker 最小运行依赖 |
| `d13f6e4` | Ubuntu 绿；Windows 1 failed / 517 passed | Windows checkout 将 worker 模板转为 CRLF，与 LF 字节比较不一致 | 将 worker 模板标记为 `-text` |
| `d47e9a3` | 4/4 Job success | 最终修复提交 | 保留为当前基线 |

- 旧 Run `43856221040` 的 478/478 结果仅保留为历史记录，不再作为最新结果。

## 5. 本地全量口径

- 本地全量回归 R2 在 55% 终止（2 个冻结 worker 子进程 45s 冷启动超时），未跑完，不声称本地全量通过。
- 本地干净环境按 CI 完全相同选择运行：518 passed、exit 0，日志哈希见第 3 节。
