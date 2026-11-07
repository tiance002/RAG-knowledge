"""
向量化任务
"""

import asyncio
from datetime import datetime
from sqlmodel import Session, select
from sqlalchemy import update

from app.tasks.celery_app import celery_app
from app.core.database import engine
from app.models import FileDocument, DocumentChunk, KnowledgeBase, FileStatus, CustomModel
from app.services.embedding import EmbeddingService
from app.services.vector_store import VectorStoreService
from app.services.metrics import track_background_task


def run_async(coro):
    """在同步环境中运行异步函数"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=0)
@track_background_task("vectorize")
def vectorize_file_task(self, file_id: str, expected_revision: str = None):
    """
    向量化文件任务

    1. 读取 DocumentChunk
    2. 调用 Embedding API
    3. 存入 ChromaDB
    4. 更新知识库统计
    """
    with Session(engine) as session:
        # 获取文件记录
        file_doc = session.get(FileDocument, file_id)
        if not file_doc:
            return {"error": "文件不存在"}

        if file_doc.status != FileStatus.EMBEDDING:
            return {"error": "文件状态不正确"}
        # This legacy task currently has no producer. Any future producer must
        # capture the revision after marking its own EMBEDDING job. A queued
        # message without that generation cannot safely claim a newer job.
        if not expected_revision or file_doc.updated_at.isoformat() != expected_revision:
            return {"error": "任务已过期或缺少版本标识"}
        old_revision = file_doc.updated_at
        claimed_revision = datetime.utcnow()
        if claimed_revision <= old_revision:
            from datetime import timedelta
            claimed_revision = old_revision + timedelta(microseconds=1)
        claimed = session.execute(
            update(FileDocument)
            .where(FileDocument.id == file_id, FileDocument.status == FileStatus.EMBEDDING,
                   FileDocument.updated_at == old_revision)
            .values(updated_at=claimed_revision)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        if claimed.rowcount != 1:
            return {"error": "任务已被领取或已过期"}
        session.refresh(file_doc)
        expected_revision = claimed_revision.isoformat()

        def still_owned():
            session.refresh(file_doc)
            return file_doc.status == FileStatus.EMBEDDING and file_doc.updated_at == claimed_revision

        def update_if_owned(**values):
            result = session.execute(
                update(FileDocument)
                .where(FileDocument.id == file_id, FileDocument.status == FileStatus.EMBEDDING,
                       FileDocument.updated_at == claimed_revision)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

        try:
            # 获取所有 Chunks
            chunks = session.exec(
                select(DocumentChunk)
                .where(DocumentChunk.file_id == file_id, DocumentChunk.chunk_type != "parent")
                .order_by(DocumentChunk.original_index)
            ).all()

            if not chunks:
                if not update_if_owned(status=FileStatus.READY, progress=100, updated_at=datetime.utcnow()):
                    session.rollback()
                    return {"error": "任务已过期"}
                session.commit()
                return {"file_id": file_id, "chunk_count": 0}

            # 获取知识库信息 (为了获取 embedding_model)
            kb = session.get(KnowledgeBase, file_doc.kb_id)
            embedding_model_id = kb.embedding_model if kb else None

            from app.services.model_bindings import resolve_model
            if kb is None:
                raise ValueError("KNOWLEDGE_BASE_NOT_FOUND")
            binding = resolve_model(session, "embedding", embedding_model_id, use_default=False)
            embedding_service = EmbeddingService(base_url=binding.base_url,
                api_key=binding.api_key, model=binding.model_name)
            target_model_id = binding.model_name

            vector_service = VectorStoreService()

            # 准备文档数据
            documents = []
            contents = []

            for chunk in chunks:
                from app.services.pdf_provenance import index_provenance
                provenance = index_provenance(file_doc.local_path, chunk)
                documents.append({
                    "id": chunk.id,
                    "content": chunk.content,
                    "metadata": {
                        "file_id": file_id,
                        "file_name": file_doc.name,
                        "page_number": chunk.page_number,
                        "content_type": chunk.content_type.value,
                        "location_info": f"第 {chunk.page_number} 页" if chunk.page_number else "",
                        **provenance,
                    },
                })
                contents.append(chunk.content)

            # 批量生成向量
            total = len(contents)
            batch_size = 10
            all_embeddings = []

            for i in range(0, total, batch_size):
                batch = contents[i:i + batch_size]
                embeddings = run_async(embedding_service.embed_documents(batch, model_id=target_model_id))
                all_embeddings.extend(embeddings)


            # Do not write after a newer generation has replaced this claim.
            if not still_owned():
                return {"error": "任务已过期"}
            vector_service.add_documents(
                kb_id=file_doc.kb_id,
                documents=documents,
                embeddings=all_embeddings,
            )

            # 更新文件状态
            if not update_if_owned(status=FileStatus.READY, progress=100, updated_at=datetime.utcnow()):
                session.rollback()
                return {"error": "任务已过期"}

            # 更新知识库统计
            kb = session.get(KnowledgeBase, file_doc.kb_id)
            if kb:
                # 重新计算总 chunk 数
                kb.chunk_count = vector_service.get_count(kb.id)
                kb.updated_at = datetime.utcnow()
                session.add(kb)

            session.commit()

            return {
                "file_id": file_id,
                "chunk_count": len(documents),
                "status": "ready",
            }

        except Exception as e:
            if update_if_owned(status=FileStatus.FAILED, error_message=type(e).__name__, updated_at=datetime.utcnow()):
                session.commit()
            else:
                session.rollback()
            raise
