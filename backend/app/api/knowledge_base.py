"""
知识库管理 API 路由
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select, func
from typing import List
from datetime import datetime
import os

from app.core.database import get_session
from app.models import KnowledgeBase, FileDocument
from app.schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseDetailResponse,
    ApiResponse,
)
from app.services.vector_store import VectorStoreService

router = APIRouter(prefix="/knowledge-bases", tags=["KnowledgeBase"])


@router.get("", response_model=ApiResponse[List[KnowledgeBaseResponse]])
async def list_knowledge_bases(session: Session = Depends(get_session)):
    """获取知识库列表"""
    statement = select(KnowledgeBase).where(KnowledgeBase.is_deleted == False).order_by(KnowledgeBase.updated_at.desc())
    kbs = session.exec(statement).all()
    return ApiResponse(data=kbs)


@router.get("/recycle-bin", response_model=ApiResponse[List[KnowledgeBaseResponse]])
async def list_deleted_knowledge_bases(session: Session = Depends(get_session)):
    """获取回收站知识库列表"""
    statement = select(KnowledgeBase).where(KnowledgeBase.is_deleted == True).order_by(KnowledgeBase.updated_at.desc())
    kbs = session.exec(statement).all()
    return ApiResponse(data=kbs)


@router.post("", response_model=ApiResponse[KnowledgeBaseResponse], status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    session: Session = Depends(get_session),
):
    """创建知识库"""
    # 检查名称是否重复
    existing = session.exec(
        select(KnowledgeBase).where(KnowledgeBase.name == data.name)
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40001, "message": "创建失败换个知识库名称试试？"},
        )

    # 创建知识库
    kb = KnowledgeBase(
        name=data.name,
        description=data.description,
        embedding_model=data.embedding_model,
        vlm_model=data.vlm_model,
    )
    session.add(kb)
    session.commit()
    session.refresh(kb)

    # 在 ChromaDB 中创建对应的 Collection
    try:
        vector_service = VectorStoreService()
        vector_service.create_collection(kb.id)
    except Exception as e:
        # 如果创建失败，回滚数据库
        session.delete(kb)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": 50001, "message": f"创建向量库失败: {str(e)}"},
        )

    return ApiResponse(data=kb)


@router.get("/{kb_id}", response_model=ApiResponse[KnowledgeBaseDetailResponse])
async def get_knowledge_base(
    kb_id: str,
    session: Session = Depends(get_session),
):
    """获取知识库详情"""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    # 获取文件数量
    files_count = len(
        session.exec(
            select(FileDocument).where(FileDocument.kb_id == kb_id)
        ).all()
    )

    # 计算 chunk 总数
    from app.models import DocumentChunk
    # 查找属于该知识库的所有文件
    files = session.exec(
        select(FileDocument).where(FileDocument.kb_id == kb_id)
    ).all()
    file_ids = [f.id for f in files]
    
    chunk_count = 0
    if file_ids:
        chunk_count = session.exec(
            select(func.count(DocumentChunk.id)).where(DocumentChunk.file_id.in_(file_ids))
        ).one()

    return ApiResponse(
        data=KnowledgeBaseDetailResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            vlm_model=kb.vlm_model,
            chunk_count=chunk_count,
            updated_at=kb.updated_at,
            files_count=files_count,
        )
    )


@router.delete("/{kb_id}", response_model=ApiResponse)
async def delete_knowledge_base(
    kb_id: str,
    session: Session = Depends(get_session),
):
    """删除知识库（移入回收站）"""
    from app.api.files import begin_file_mutation, reject_busy_files
    begin_file_mutation(session)
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    reject_busy_files(session, select(FileDocument.id).where(FileDocument.kb_id == kb_id))
    kb.is_deleted = True
    session.add(kb)
    session.commit()
    
    return ApiResponse(message="已移入回收站")


@router.post("/{kb_id}/restore", response_model=ApiResponse)
async def restore_knowledge_base(
    kb_id: str,
    session: Session = Depends(get_session),
):
    """从回收站恢复知识库"""
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    kb.is_deleted = False
    session.add(kb)
    session.commit()
    
    return ApiResponse(message="恢复成功")


@router.delete("/{kb_id}/permanent", response_model=ApiResponse)
async def permanent_delete_knowledge_base(
    kb_id: str,
    session: Session = Depends(get_session),
):
    """永久删除知识库"""
    from app.api.files import begin_file_mutation, reject_busy_files
    from app.services.index_builds import retire_file_builds
    begin_file_mutation(session)
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    reject_busy_files(session, select(FileDocument.id).where(FileDocument.kb_id == kb_id))

    # 删除数据库记录（手动级联删除文件和 chunks）
    from app.models import DocumentChunk
    from app.services.bm25 import BM25Index
    BM25Index.clear_kb_index(session, kb_id)
    
    # 获取所有文件
    files = session.exec(select(FileDocument).where(FileDocument.kb_id == kb_id)).all()
    from app.services.file_retirement import retire_source, clean_source, retire_collection
    sources = [retire_source(session, doc) for doc in files]
    retire_collection(session, kb_id)
    for file_doc in files:
        retire_file_builds(session, file_doc.id)
        # 删除 chunks
        chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_doc.id)).all()
        for chunk in chunks:
            session.delete(chunk)
            
        session.delete(file_doc)

    session.delete(kb)
    session.commit()
    for retirement_id in sources:
        clean_source(session.get_bind(), retirement_id)
    
    return ApiResponse(message="永久删除成功")
