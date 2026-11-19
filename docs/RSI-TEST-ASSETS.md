# RSI 测试资产入口

以下路径相对于本仓库根目录。建立独立 RSI 仓库后，评测工具可以直接引用原 RAG 工作区的资产。

| 资产 | 现有位置 |
| --- | --- |
| QA500 完整题目、Gold 和证据定位 | `.planning/2026-11-10-qa500-bilingual-curation/artifacts/qa500_final_offline.json` |
| 开发集运行文件，只有问题 | `.planning/2026-11-10-qa500-bilingual-curation/artifacts/qa500_development_runtime_questions.jsonl` |
| 冻结集运行文件，保留会话顺序 | `.planning/2026-11-10-qa500-bilingual-curation/artifacts/qa500_frozen_runtime_sessions.json` |
| QA500 来源与版本清单 | `.planning/2026-11-10-qa500-bilingual-curation/artifacts/qa500_final_source_manifest.json` |
| 原始资料的本地快照 | `var/qa500-ecs-sources-20261116/files/`，配套清单是同目录的 `snapshot-manifest-complete.json` |
| 历史 ECS 冻结集执行记录及逐题审查 | `var/qa500-full300-20261115/` |
| RSI 基础实现和测试 | `RSI-kit/`、`scripts/rsi_quality_worker.py`、`scripts/rsi_playbook_verify.py`、`backend/tests/` |
| Langfuse 接入 | `backend/app/services/observability.py`、`backend/.env.langfuse.example`、`backend/requirements-observability.txt` |

给被测系统输入对应的运行文件，评分端单独读取完整 Gold 和证据。资料快照和部分本地资产没有提交到 Git，新仓库需要引用这些本地路径。Langfuse 的本地凭据按配置模板单独设置。

本次收尾保留现有数据和标注，未重新审核题目、Gold、证据或原始资料的正确性。
