# RAG Knowledge

面向 PDF、Word、Excel、Markdown、文本和图片的知识库问答系统。仓库当前的应用由 FastAPI 后端和 WeKnora 风格的 React 前端组成。

## 当前正式版本

| 环节 | 实现 |
| --- | --- |
| 文档入库 | FastAPI 接收文件操作，SQLite 记录任务状态，Redis/Celery 执行解析、向量化和索引发布 |
| 解析与分块 | 各格式提取内容后走统一分块入口，可按知识库任务配置使用父子分块 |
| 检索 | Chroma 向量检索与 SQLite 上的 BM25 倒排检索，支持 Hybrid、重排和来源引用 |
| 问答 | 普通知识库问答与 LangGraph Agent；ECS 正式模型路由为 E（`RISK_E`），Qwen 为默认角色，风险达到 45 分时选择 GLM |
| 运行结构 | ECS 使用独立的 API、Chroma、解析、索引和任务恢复进程；前端由 Nginx 提供页面和 API 代理 |

入库失败时会先检查能否复用已保存的结果。同一不确定的模型请求最多自动补发一次，仍失败时可从前端任务页取消或重新处理。已发布索引在新构建通过校验并激活前继续提供检索。

## 仓库导航

| 路径 | 用途 |
| --- | --- |
| [`backend/app/`](backend/app/) | 正式 API、检索、任务和数据模型 |
| [`frontend-weknora/`](frontend-weknora/) | 唯一正式前端，含任务面板和请求去重代码 |
| [`config/`](config/) | 部署配置模板 |
| [`backend/tests/`](backend/tests/)、[`tests/`](tests/) | 回归测试及其依赖 |
| [`scripts/`](scripts/README.md) | 运维、回归、评测及 RSI 工具，应用启动无需运行 |
| [`docs/`](docs/PROJECT-INTERVIEW-INDEX.md) | 项目学习索引、设计报告和复盘记录 |
| [`RSI-kit/`](RSI-kit/)、[评测资产索引](docs/RSI-TEST-ASSETS.md) | RSI 复用入口、QA500 输入、Gold、证据和逐题结果位置 |

## 本地开发

所有后端进程从 `backend/` 启动，并复用同一个 Python 环境与 `.env`。先准备 Redis，并启动独立 Chroma 服务。API 和前端开发服务器默认分别使用 `8000` 和 `3001`。

```bash
# 先在仓库根目录激活项目 Python 环境并安装依赖
python -m pip install -r requirements.txt
cd backend
cp .env.example .env
```

在 `backend/.env` 中设置 Redis、Chroma 地址和本地数据目录。在 Linux 上，可分别在常驻进程中运行以下命令。Chroma 的存储路径应与要加载的索引一致。

```bash
# Chroma（单独进程）
chroma run --path ./storage/chroma_db --host 127.0.0.1 --port 8001

# API（单独进程）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# 后台入库（三个独立进程）
python -m app.tasks.file_task_recovery
FILE_TASK_WORKER_ROLE=parse python -m celery -A app.tasks.celery_app:celery_app worker -Q document.parse -n parse@%h --pool=prefork -l INFO
FILE_TASK_WORKER_ROLE=index python -m celery -A app.tasks.celery_app:celery_app worker -Q document.index -n index@%h --pool=prefork -l INFO
```

前端在仓库根目录执行：

```bash
cd frontend-weknora
npm install
npm run dev
# 生产构建产物位于 dist/
npm run build
```

访问 `http://localhost:3001`，在设置里配置模型。新环境的路由模板默认关闭。需要复现正式 E 路由时，从 [路由模板](config/router-v1.env.example) 创建 `config/model-router.env`，填入实际 Qwen 和 GLM 绑定，设置 `RAG_MODEL_ROUTER_MODE=DYNAMIC`、`RAG_MODEL_ROUTER_SELECTION_POLICY=RISK_E`、`RAG_MODEL_ROUTER_THRESHOLD=0.45`、`RAG_ROUTER_ALLOW_MODEL_CALLS=true` 和 `RAG_CHEAP_GROUNDING_ENABLED=true`。详见 [E 方案发布记录](docs/router-e-formal-release-20261119.md)。

ECS 的正式 API 使用本机 `8200`，Chroma 使用 `8001`。前端部署时由 Nginx 将 `/api` 和 `/static` 转发到后端。Vite 的 `8000` 是本地开发代理目标。

## 设计复盘和评测复用

- [项目学习与面试索引](docs/PROJECT-INTERVIEW-INDEX.md) 按入库、BM25、并发检索、父子分块、引用预算、E 路由和 Langfuse 串起原报告。
- [RSI 测试资产索引](docs/RSI-TEST-ASSETS.md) 给出 QA500 开发集、冻结会话、Gold、原始证据和已有结果的路径。
- [Langfuse 配置模板](backend/.env.langfuse.example) 可与 [可观测依赖](backend/requirements-observability.txt) 一起使用。各环境在本地配置凭据。
