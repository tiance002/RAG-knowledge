"""
系统设置 API 路由
"""

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
import json
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import Field, Session, SQLModel, select

from app.core.config import get_settings
from app.core.database import get_session
from app.schemas.schemas import ApiResponse, CustomModelCreate, CustomModelResponse, CustomModelUpdate
from app.models import CustomModel, ModelType

router = APIRouter(prefix="/settings", tags=["Meta"])
models_router = APIRouter(tags=["Meta"]) # 单独的 /models 路由
settings = get_settings()


# Model and configuration identity live in one service, including non-HTTP users.
from app.services.runtime_settings import (
    PersistedSystemSettings, get_runtime_settings, update_runtime_settings,
    RuntimeSettingsUnavailable, SettingsConflict, RuntimeValues,
)
from app.services.model_bindings import ModelBindingError, model_references


def load_persisted_settings(session: Session):
    """Compatibility entry point; database settings are the runtime authority."""
    return get_runtime_settings(session)


class ModelInfo(BaseModel):
    id: str
    name: str
    type: str
    provider: str


class SystemSettings(BaseModel):
    defaultEmbeddingModel: str | None
    defaultVlmModel: str | None
    defaultLlmModel: str | None
    maxConcurrency: int
    chunkSize: int
    chunkOverlap: int
    maxChatHistoryRounds: int
    revision: int
    degraded: bool = False
    configError: str | None = None
    restartRequired: list[str] = []


class UpdateSettingsRequest(BaseModel):
    expected_revision: int = PydanticField(ge=1)
    default_embedding_model: str | None = None
    default_vlm_model: str | None = None
    default_llm_model: str | None = None
    max_concurrency: int | None = PydanticField(default=None, ge=1, le=32)
    chunk_size: int | None = PydanticField(default=None, ge=128, le=2048)
    chunk_overlap: int | None = PydanticField(default=None, ge=0, le=512)
    max_chat_history_rounds: int | None = PydanticField(default=None, ge=1, le=100)


def _settings_response(snapshot):
    return ApiResponse(data=SystemSettings(
        defaultEmbeddingModel=snapshot.default_embedding_model,
        defaultVlmModel=snapshot.default_vlm_model,
        defaultLlmModel=snapshot.default_llm_model,
        maxConcurrency=snapshot.max_concurrency,
        chunkSize=snapshot.chunk_size,
        chunkOverlap=snapshot.chunk_overlap,
        maxChatHistoryRounds=snapshot.max_chat_history_rounds,
        revision=snapshot.revision, degraded=snapshot.degraded,
        configError=snapshot.config_error,
        # Worker pool concurrency is initialized at Worker startup.
        restartRequired=['max_concurrency'],
    ))


@router.get("", response_model=ApiResponse[SystemSettings])
async def get_system_settings(session: Session = Depends(get_session)):
    try:
        return _settings_response(get_runtime_settings(session))
    except (RuntimeSettingsUnavailable, SettingsConflict) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("", response_model=ApiResponse[SystemSettings])
async def update_system_settings(data: UpdateSettingsRequest, session: Session = Depends(get_session)):
    changes = data.model_dump(exclude_unset=True)
    revision = changes.pop('expected_revision')
    try:
        snapshot = update_runtime_settings(session, changes, revision)
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, ModelBindingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail='Settings save could not be confirmed; reload before retrying') from exc
    return _settings_response(snapshot)


# ==================== 自定义模型管理 ====================

@router.get("/models", response_model=ApiResponse[List[CustomModelResponse]])
async def get_custom_models(session: Session = Depends(get_session)):
    """获取自定义模型列表"""
    # 1. 获取数据库自定义模型
    db_models = session.exec(select(CustomModel)).all()
    response_list = [CustomModelResponse.model_validate(m) for m in db_models]
    
    return ApiResponse(data=response_list)


from datetime import datetime

@router.post("/models", response_model=ApiResponse[CustomModelResponse])
async def create_custom_model(
    model_in: CustomModelCreate,
    session: Session = Depends(get_session)
):
    """添加自定义模型"""
    # 检查名称是否重复
    existing = session.exec(select(CustomModel).where(CustomModel.name == model_in.name)).first()
    if existing:
        raise HTTPException(status_code=400, detail="模型名称已存在")

    try:
        # 验证 model_type
        m_type = ModelType(model_in.model_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的模型类型")

    db_model = CustomModel(
        name=model_in.name,
        model_type=m_type,
        base_url=model_in.base_url,
        api_key=model_in.api_key,
        model_name=model_in.model_name,
        context_length=model_in.context_length,
        updated_at=datetime.utcnow(), # Fix: set updated_at
    )
    session.add(db_model)
    session.commit()
    session.refresh(db_model)
    return ApiResponse(data=db_model)


@router.put("/models/{model_id}", response_model=ApiResponse[CustomModelResponse])
async def update_custom_model(
    model_id: str,
    model_in: CustomModelUpdate,
    session: Session = Depends(get_session)
):
    """更新自定义模型"""
    db_model = session.get(CustomModel, model_id)
    if not db_model:
        raise HTTPException(status_code=404, detail="模型不存在")

    references = model_references(session, model_id)
    changes_identity = ((model_in.model_type is not None and model_in.model_type != db_model.model_type.value)
                        or (model_in.model_name is not None and model_in.model_name != db_model.model_name)
                        or (model_in.base_url is not None and model_in.base_url != db_model.base_url))
    if references and changes_identity:
        raise HTTPException(status_code=409, detail={"message": "Model is referenced; migrate its bindings before changing identity", "references": references})

    if model_in.name is not None and model_in.name != db_model.name:
        # 检查名称重复 - 已移除限制
        # existing = session.exec(select(CustomModel).where(CustomModel.name == model_in.name)).first()
        # if existing:
        #     raise HTTPException(status_code=400, detail="模型名称已存在")
        db_model.name = model_in.name

    if model_in.base_url is not None:
        db_model.base_url = model_in.base_url
    if model_in.api_key is not None:
        db_model.api_key = model_in.api_key
    if model_in.model_name is not None:
        db_model.model_name = model_in.model_name
    
    if model_in.context_length is not None:
        db_model.context_length = model_in.context_length

    if model_in.model_type is not None:
        try:
            db_model.model_type = ModelType(model_in.model_type)
        except ValueError:
             raise HTTPException(status_code=400, detail="无效的模型类型")

    db_model.updated_at = datetime.utcnow()
    session.add(db_model)
    session.commit()
    session.refresh(db_model)
    return ApiResponse(data=db_model)


@router.delete("/models/{model_id}", response_model=ApiResponse)
async def delete_custom_model(
    model_id: str,
    session: Session = Depends(get_session)
):
    """删除自定义模型"""
    if model_id == "sys_embedding":

        return ApiResponse(message="系统模型已隐藏")

    db_model = session.get(CustomModel, model_id)
    if not db_model:
        raise HTTPException(status_code=404, detail="模型不存在")
    
    references = model_references(session, model_id)
    if references:
        raise HTTPException(status_code=409, detail={"message": "Model is still referenced", "references": references})
    session.delete(db_model)
    session.commit()
    return ApiResponse(message="删除成功")


@router.post("/models/{model_id}/test", response_model=ApiResponse)
async def test_custom_model_connection(
    model_id: str,
    session: Session = Depends(get_session)
):
    """测试自定义模型连接"""
    db_model = session.get(CustomModel, model_id)
    if not db_model:
        raise HTTPException(status_code=404, detail="模型不存在")

    try:
        # 根据模型类型进行简单测试
        if db_model.model_type == ModelType.EMBEDDING:
            from app.services.embedding import EmbeddingService
            service = EmbeddingService(
                base_url=db_model.base_url,
                api_key=db_model.api_key,
                model=db_model.model_name
            )
            # 尝试 embed 一个简单字符串
            await service.embed_query("test connection")
            
        elif db_model.model_type == ModelType.LLM:
            from app.services.llm import LLMService
            service = LLMService(
                base_url=db_model.base_url,
                api_key=db_model.api_key,
                model=db_model.model_name
            )
            # 尝试简单对话，只生成 1 token 验证连接
            # 注意：generate_stream 是个 async generator
            received = False
            async for chunk in service.generate_stream(
                messages=[{"role": "user", "content": "hi"}],
                model_id=db_model.model_name
            ):
                if chunk:
                    received = True
                    break
            if not received:
                raise ValueError("模型未返回内容")
                
        elif db_model.model_type == ModelType.VLM:
             # VLM 暂时没有独立的 Service 类封装，或者复用 LLMService
             # 假设 VLM 也可以用 LLMService 测试文本对话能力 (大部分 VLM 都兼容 LLM 接口)
            from app.services.llm import LLMService
            service = LLMService(
                base_url=db_model.base_url,
                api_key=db_model.api_key,
                model=db_model.model_name
            )
            received = False
            async for chunk in service.generate_stream(
                messages=[{"role": "user", "content": "hi"}],
                model_id=db_model.model_name
            ):
                if chunk:
                    received = True
                    break
            if not received:
                raise ValueError("模型未返回内容")
        
        elif db_model.model_type == ModelType.RERANK:
            from app.services.rerank import test_rerank_connection
            result = test_rerank_connection(db_model)
            if not result["success"]:
                raise Exception(result["message"])

        return ApiResponse(message="连接测试成功")

    except Exception as e:
        print(f"Test connection failed: {e}")
        # 返回 400 让前端知道测试失败
        raise HTTPException(
            status_code=400, 
            detail={"message": f"连接测试失败: {str(e)}"}
        )


# ==================== 通用模型列表 (下拉选择用) ====================

@models_router.get("/models", response_model=ApiResponse[List[ModelInfo]])
async def get_model_list(session: Session = Depends(get_session)):
    """获取模型列表（包含系统预设和自定义模型）"""
    models = []
    print("🔍 [GetModelList] 开始获取模型列表...")

    # Deployment model records remain available through stable system IDs.
    for role in ('llm', 'embedding', 'vlm'):
        model_name = getattr(settings, role.upper() + '_MODEL')
        if model_name:
            models.append(ModelInfo(id='sys_' + role,
                name=f'System {role.upper()} ({model_name})', type=role, provider='System'))

    # 2. 自定义模型 (从数据库读取)
    try:
        custom_models = session.exec(select(CustomModel).where(CustomModel.is_active == True)).all()
        print(f"🔍 [GetModelList] 数据库中发现 {len(custom_models)} 个激活的自定义模型")
        for cm in custom_models:
            print(f"   - ID: {cm.id}, Name: {cm.name}, Type: {cm.model_type.value}, Provider: Custom")
            models.append(ModelInfo(
                id=cm.id, # 使用 UUID 作为 ID
                name=cm.name,
                type=cm.model_type.value,
                provider="Custom",
            ))
    except Exception as e:
        print(f"❌ [GetModelList] 查询数据库失败: {e}")

    print(f"✅ [GetModelList] 返回总模型数: {len(models)}")
    return ApiResponse(data=models)
