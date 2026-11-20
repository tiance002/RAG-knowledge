# 运维与评测工具

正式应用从 `backend/app/` 和 `frontend-weknora/` 启动。这个目录内的工具按需要单独执行。

| 类别 | 入口与使用场景 |
| --- | --- |
| 索引维护和回滚 | `migrate_parent_child.py`、`restore_bm25_legacy_df.py` |
| 发布与回归工具 | Worker 验收、索引发布对照、可观测回归，以及由 `backend/tests/` 和 `tests/` 引用的辅助代码 |
| QA500 和路由评测 | 从 [评测资产入口](../docs/RSI-TEST-ASSETS.md) 与 [E 路由报告](../docs/router-e-formal-release-20261119.md) 开始，按具体报告进入归档时期的回放 |
| RSI | `rsi_quality_worker.py`、`rsi_playbook_verify.py`、`rsi_preparation/` 和 [RSI-kit](../RSI-kit/) |

已有报告、评测脚本和回归测试可能依赖具体的历史文件名或相对路径。保留仍被引用的脚本。已退役的单次 ECS 启动、重试和检查入口在 `var/formal-layout-archive-20261120/scripts/`，归档清单记录了原路径，可按原版本复现。

旧前端快照位于 `var/formal-layout-archive-20261120/frontend/`。两项历史界面测试需要在该快照安装自己的依赖，并通过 `RAG_LEGACY_FRONTEND_DIR` 显式指定路径。正式前端开发与构建只需要 `frontend-weknora/`。
