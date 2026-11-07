"""
应用配置模块
"""

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List
import os
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[2]
_PROJECT_DIR = _BACKEND_DIR.parent
_runtime_secrets = Path(os.getenv('RAG_RUNTIME_SECRETS_FILE') or 'config/runtime-secrets.env')
if not _runtime_secrets.is_absolute():
    _runtime_secrets = _PROJECT_DIR / _runtime_secrets


class Settings(BaseSettings):
    """应用配置"""

    # 服务配置
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ENV: str = "dev"

    # 数据库与存储。运行时数据默认保存在当前部署目录，不进入 Git。
    DATABASE_URL: str = "sqlite:///./local.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    MEMORY_REDIS_URL: str | None = None
    CHROMA_DB_DIR: str = "./storage/chroma_db"
    UPLOAD_DIR: str = "./storage/uploads"
    IMAGE_DIR: str = "./storage/images"

    # LLM 配置
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_API_KEY: str = "ollama"
    LLM_MODEL: str = ""
    # Non-streamed answer generation only; streaming and Agent retain provider defaults.
    LLM_ANSWER_MAX_TOKENS: int = 4096
    # Relevance grading only: bound one request, without retrying an unknown result.
    AGENT_GRADING_TIMEOUT_SECONDS: float = 60.0

    FALLBACK_STRATEGY: str = "model"
    FALLBACK_PROMPT: str = "请根据通用知识回答用户问题；无法确定时说明不确定。"
    FALLBACK_RESPONSE: str = "未在所选知识库中找到可用于回答的相关资料。"

    # Optional Tavily web lookup; only explicit per-request opt-in invokes it.
    TAVILY_API_KEY: str = ""
    TAVILY_API_KEY_FILE: str = ""
    TAVILY_TIMEOUT_SECONDS: float = 20.0
    TAVILY_MAX_RESULTS: int = 5

    # Optional PEM trust bundle for Embedding/VLM HTTPS endpoints.
    PROVIDER_CA_BUNDLE: str | None = None

    # Embedding 配置
    EMBEDDING_BASE_URL: str = "http://localhost:11434/v1"
    EMBEDDING_API_KEY: str = "ollama"
    EMBEDDING_MODEL: str = ""
    EMBEDDING_MAX_CONTEXT_LENGTH: int = 8196  # Embedding 模型最大上下文长度

    # VLM 配置
    VLM_BASE_URL: str = "http://localhost:11434/v1"
    VLM_API_KEY: str = "ollama"
    VLM_MODEL: str = ""

    # Bootstrap-only inputs for the first system_settings migration. Runtime
    # UI defaults come from services.runtime_settings after initialization.
    # Provider connection identities above remain deployment configuration.
    DEFAULT_LLM_MODEL_ID: str = ""
    DEFAULT_EMBEDDING_MODEL_ID: str = ""
    DEFAULT_VLM_MODEL_ID: str = ""

    # Celery queues are separate from Redis preference keys and other projects.
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    CELERY_QUEUE_NAME: str = "amrag_ingest"
    CELERY_WORKER_CONCURRENCY: int = 1
    CELERY_TASK_TIME_LIMIT: int = 3600

    # 任务配置
    MAX_PARSING_WORKERS: int = 10

    # 切分配置
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50

    PDF_PARSER_MODE: str = "legacy"  # enhanced is opt-in until the quality gate passes
    PDF_TESSDATA_DIR: str = "./storage/tessdata"
    PDF_OCR_DPI: int = 150
    PDF_SCANNED_TABLES_ENABLED: bool = False
    PDF_TABLES_ENABLED: bool = True  # enhanced PDF structure; raw text/OCR survives when disabled
    PDF_AUDIT_DIR: str = "./storage/pdf_manifests"

    # VLM 提示词
    VLM_PROMPT: str = "请详细描述这张图片的内容，包括图表中的数据趋势、关键文字信息。"

    # 文件限制
    MAX_FILE_SIZE: int = 100 * 1024 * 1024  # 100MB
    ALLOWED_EXTENSIONS: List[str] = [".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md", ".jpg", ".jpeg", ".png", ".bmp", ".webp"]

    # ==================== 检索配置 ====================
    RETRIEVAL_TOP_K: int = 100              # 粗筛数量
    RETRIEVAL_DEFAULT_MODE: str = "hybrid"  # 默认检索模式: vector | fulltext | hybrid
    RETRIEVAL_VECTOR_WEIGHT: float = 0.7    # 默认向量权重
    RETRIEVAL_BM25_WEIGHT: float = 0.3      # 默认 BM25 权重

    # BM25 检索配置
    BM25_K1: float = 1.5                    # BM25 词频饱和参数
    BM25_B: float = 0.75                    # BM25 文档长度归一化参数
    BM25_TOP_K: int = 50                    # BM25 检索返回数量

    # ==================== Rerank 配置 ====================
    RERANK_MAX_CONTEXT: int = 8196          # Rerank 单批最大 token
    RERANK_DEFAULT_TOP_K: int = 20          # 默认 Rerank 返回数量
    RERANK_MIN_TOP_K: int = 5               # 最小 Rerank 返回数量
    RERANK_MAX_TOP_K: int = 50              # 最大 Rerank 返回数量

    # ==================== 上下文扩展配置 ====================
    CONTEXT_WINDOW_SIZE: int = 1            # 前后上下文 chunk 数
    MAX_PARENT_LENGTH: int = 4000           # 父文档最大拼接长度（字符）
    CONTEXT_MAX_CHARS: int = 24000         # All expanded evidence, including neighbors
    GROUNDED_REQUEST_MAX_CHARS: int = 32000 # Serialized messages + references + prompt; character guard

    # ==================== 对话配置 ====================
    MAX_CHAT_HISTORY_ROUNDS: int = 20       # 最大保留历史对话轮数

    class Config:
        # Later dotenv files override earlier ones; process environment overrides both.
        env_file = (str(_BACKEND_DIR / '.env'), str(_runtime_secrets))
        env_file_encoding = "utf-8"
        extra = "ignore"


def provider_tls_context():
    """HTTPX accepts either its default verified trust store or a custom CA context."""
    import ssl
    ca_bundle = get_settings().PROVIDER_CA_BUNDLE
    return ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


# 确保存储目录存在
def ensure_directories():
    """确保必要的目录存在"""
    settings = get_settings()
    dirs = [
        settings.UPLOAD_DIR,
        settings.IMAGE_DIR,
        settings.CHROMA_DB_DIR,
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
