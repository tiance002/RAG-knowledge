"""
任务模块导出
"""

from app.tasks.celery_app import celery_app

__all__ = [
    "celery_app",
]
