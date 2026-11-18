"""
API 路由汇总
"""

from fastapi import APIRouter
from app.api import settings, knowledge_base, files, file_task_admin, recall, chat
from app.api import conversations

api_router = APIRouter(prefix="/api/v1")

# 注册各模块路由
api_router.include_router(settings.router)
api_router.include_router(settings.models_router)  # 新增：/models 接口
api_router.include_router(knowledge_base.router)
api_router.include_router(files.router)
api_router.include_router(file_task_admin.router)
api_router.include_router(recall.router)
api_router.include_router(chat.router)
api_router.include_router(conversations.router)
