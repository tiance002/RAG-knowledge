from app.services.observability import event, observe, enabled
"""
文件解析任务
"""

import asyncio
from datetime import datetime
from sqlmodel import Session, select

from app.tasks.celery_app import celery_app
from app.core.database import engine
from app.models import FileDocument, DocumentChunk, FileStatus, ContentType, KnowledgeBase, CustomModel
from app.services.parser import FileParser
from app.services.vector_store import VectorStoreService
from app.services.embedding import EmbeddingService
from app.services.bm25 import get_bm25_index  # 新增：BM25 索引
from app.core.config import get_settings # Fix: import get_settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sqlalchemy.exc import OperationalError

settings = get_settings() # Fix: Initialize settings

# 定义重试策略：捕获 OperationalError (通常包含 database is locked)，最多重试 5 次，指数退避
db_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(OperationalError)
)

@db_retry
def safe_commit(session: Session):
    """带重试机制的数据库提交"""
    session.commit()


def run_async(coro):
    """在同步环境中运行异步函数"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _preflight_embedding(session, file_doc, vector_model):
    """Reject incompatible models before replacing any persisted file data."""
    kb = session.get(KnowledgeBase, file_doc.kb_id)
    if not kb:
        raise ValueError(f"知识库不存在: {file_doc.kb_id}")
    service = VectorStoreService()
    collection = service._existing_collection(file_doc.kb_id)
    if collection is None or not collection.count():
        return
    if vector_model and vector_model != kb.embedding_model:
        raise ValueError("知识库已有向量，不能通过单文件入库更换 Embedding 模型；需要显式迁移")


def _prepare_embeddings(session, file_doc, vector_model, contents):
    kb = session.get(KnowledgeBase, file_doc.kb_id)
    model_id = vector_model or kb.embedding_model
    custom = session.get(CustomModel, model_id) if model_id else None
    if custom:
        embedding = EmbeddingService(base_url=custom.base_url, api_key=custom.api_key, model=custom.model_name)
        model_id = custom.model_name
    else:
        embedding = EmbeddingService()
    vectors = run_async(embedding.embed_documents(contents, model_id=model_id))
    if len(vectors) != len(contents) or any(not v for v in vectors):
        raise ValueError("Embedding returned incomplete vectors")
    if vectors:
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("Embedding returned vectors with inconsistent dimensions")
        VectorStoreService().validate_dimension(file_doc.kb_id, vectors[0])
    return vectors


@celery_app.task(bind=True, max_retries=3)
def parse_file_task(self, file_id: str, **kwargs):
    """Celery 任务包装器"""
    try:
        return process_file_parsing(file_id, **kwargs)
    except Exception as e:
        raise self.retry(exc=e, countdown=60)


@celery_app.task(bind=True, max_retries=3)
def submit_chunks_task(self, file_id: str, chunks_data: list, vector_model: str = None):
    """处理提交的 Chunks 任务"""
    try:
        return process_submitted_chunks(file_id, chunks_data, vector_model=vector_model)
    except Exception as e:
        raise self.retry(exc=e, countdown=60)


class IndexRetirementError(RuntimeError):
    """SQL has committed, but retiring the superseded vectors failed."""


@observe("ingest.index")
def process_submitted_chunks(file_id: str, chunks_data: list, vector_model: str = None):
    """Stage new vectors first, then replace SQL chunks and retire exact old IDs."""
    with Session(engine, expire_on_commit=False) as session:
        file_doc = session.get(FileDocument, file_id)
        if not file_doc:
            return {"error": "文件不存在"}
        started_without_index = file_doc.status in (FileStatus.PENDING, FileStatus.PENDING_CONFIRM)
        try:
            _preflight_embedding(session, file_doc, vector_model)
            if started_without_index:
                file_doc.status = FileStatus.EMBEDDING
                file_doc.progress = 10
                file_doc.updated_at = datetime.utcnow()
                session.add(file_doc)
                safe_commit(session)
            embeddings = _prepare_embeddings(session, file_doc, vector_model, [c.get("content", "") for c in chunks_data])
            kb = session.get(KnowledgeBase, file_doc.kb_id)
            old_chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id)).all()
            old_ids = [c.id for c in old_chunks]
            new_chunks = []
            documents = []
            from app.services.pdf_provenance import index_provenance
            for idx, data in enumerate(chunks_data):
                try:
                    content_type = ContentType(data.get("content_type", "text"))
                except ValueError:
                    content_type = ContentType.TEXT
                chunk = DocumentChunk(
                    file_id=file_id, content=data.get("content", ""),
                    page_number=data.get("page_number", 0), content_type=content_type,
                    image_path=data.get("image_path", ""), original_index=idx,
                    parent_id=data.get("parent_id"), heading_level=data.get("heading_level"),
                    heading_text=data.get("heading_text"), chunk_type=data.get("chunk_type", "content"),
                    token_count=data.get("token_count", 0),
                )
                metadata = {
                    "file_id": file_id, "file_name": file_doc.name, "chunk_index": idx,
                    "page_number": chunk.page_number or 0, "content_type": chunk.content_type.value,
                    "image_path": chunk.image_path or "",
                    "location_info": f"Page {chunk.page_number or 1} | Chunk #{idx + 1}",
                    "parent_id": chunk.parent_id or "", "heading_level": chunk.heading_level or 0,
                    "heading_text": chunk.heading_text or "", "chunk_type": chunk.chunk_type or "content",
                }
                metadata.update(index_provenance(file_doc.local_path, chunk))
                new_chunks.append(chunk)
                documents.append({"id": chunk.id, "content": chunk.content, "metadata": metadata})
            service = VectorStoreService()
            # A rejected write must leave the old SQL and vectors untouched.
            try:
                service.add_documents(kb_id=file_doc.kb_id, documents=documents, embeddings=embeddings)
            except Exception as write_error:
                try:
                    service.delete_ids(file_doc.kb_id, [c.id for c in new_chunks])
                except Exception as cleanup_error:
                    raise RuntimeError(f"向量写入失败: {write_error}; 新向量清理失败: {cleanup_error}") from write_error
                raise
        except Exception as exc:
            file_doc.error_message = str(exc)
            if started_without_index:
                file_doc.status = FileStatus.FAILED
            session.add(file_doc)
            safe_commit(session)
            raise

        new_ids = [c.id for c in new_chunks]
        try:
            bm25 = get_bm25_index()
            for chunk in old_chunks:
                bm25.delete_chunk_index(session, chunk.id, file_doc.kb_id)
                session.delete(chunk)
            session.flush()
            for chunk in new_chunks:
                session.add(chunk)
                bm25.index_chunk(session, chunk.id, chunk.content, file_doc.kb_id)
            file_doc.status = FileStatus.PARSED
            file_doc.progress = 100
            file_doc.error_message = None
            file_doc.updated_at = datetime.utcnow()
            selected_model_id = vector_model or kb.embedding_model
            if selected_model_id:
                kb.embedding_model = selected_model_id
            from sqlalchemy import func
            session.flush()
            kb.chunk_count = session.exec(select(func.count(DocumentChunk.id)).join(FileDocument).where(FileDocument.kb_id == kb.id)).one()
            kb.updated_at = datetime.utcnow()
            session.add(file_doc)
            session.add(kb)
            safe_commit(session)
        except Exception as sql_error:
            session.rollback()
            cleanup_error = None
            try:
                service.delete_ids(file_doc.kb_id, new_ids)
            except Exception as exc:
                cleanup_error = exc
            file_doc = session.get(FileDocument, file_id)
            file_doc.error_message = f"数据库提交失败: {sql_error}"
            if started_without_index:
                file_doc.status = FileStatus.FAILED
            if cleanup_error:
                file_doc.error_message += f"; 新向量清理失败: {cleanup_error}"
            session.add(file_doc)
            safe_commit(session)
            if cleanup_error:
                raise RuntimeError(file_doc.error_message) from sql_error
            raise

        lineage = {}
        if enabled():
            try:
                from app.services.index_lineage import file_lineage
                lineage = file_lineage(file_id, file_doc.local_path, new_chunks)
                event("ingest.file_lineage", **lineage)
            except Exception:
                pass
        event("ingest.index_chunks", file_id=file_id, kb_id=file_doc.kb_id,
              chunk_count=len(new_chunks), chunk_ids=[str(c.id) for c in new_chunks])
        event("ingest.bm25_indexed", kb_id=file_doc.kb_id,
              **(lineage or {"file_id": file_id, "chunk_count": len(new_chunks)}))
        custom = session.get(CustomModel, selected_model_id) if selected_model_id else None
        actual_model = custom.model_name if custom else selected_model_id
        event("ingest.vector_indexed", kb_id=file_doc.kb_id, model=actual_model,
              **(lineage or {"file_id": file_id, "chunk_count": len(new_chunks)}))

        try:
            service.delete_ids(file_doc.kb_id, old_ids)
        except Exception as exc:
            # New index is committed; expose stale-vector cleanup failure explicitly.
            file_doc.error_message = f"旧向量清理失败: {exc}"
            session.add(file_doc)
            safe_commit(session)
            raise IndexRetirementError(file_doc.error_message) from exc



@observe("ingest.parse")
def process_file_parsing(
    file_id: str, 
    chunk_mode: str = "custom", 
    chunk_size: int = 500, 
    chunk_overlap: int = 50,
    separator: str = "\n\n",
    heading_level: int = 2,
    embedding_model: str = None,
    vector_model: str = None,
    auto_vectorize: bool = False
):
    """
    处理文件解析任务
    
    Args:
        file_id: 文件ID
        chunk_mode: 切分模式 (custom, no_split, structure, semantic)
        chunk_size: 块大小
        chunk_overlap: 重叠大小
        separator: 分隔符
        heading_level: 标题切分级别 (1-3)
        embedding_model: 语义切分用的 Embedding 模型 ID (Optional)
        vector_model: 向量化用的 Embedding 模型 ID (Optional)
        auto_vectorize: 解析完成后是否自动入库
    """
    # 立即打印入参，确保前端传值正确
    print(f"🚀 [ParseTask] 接收到任务: file_id={file_id}, chunk_mode={chunk_mode}, embedding_model={embedding_model}, vector_model={vector_model}", flush=True)
    
    # 类型安全转换
    try:
        if chunk_size is not None: chunk_size = int(chunk_size)
        if chunk_overlap is not None: chunk_overlap = int(chunk_overlap)
        if heading_level is not None: heading_level = int(heading_level)
    except (ValueError, TypeError) as e:
        print(f"⚠️ [ParseTask] 参数类型转换失败: {e}")
        # 使用默认值
        chunk_size = chunk_size or 500
        chunk_overlap = chunk_overlap or 50
        heading_level = heading_level or 2

    with Session(engine) as session:
        # 获取文件记录
        file_doc = session.get(FileDocument, file_id)
        if not file_doc:
            print(f"❌ [ParseTask] 文件不存在: {file_id}")
            return {"error": "文件不存在"}

        old_chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id)).first()
        try:
            _preflight_embedding(session, file_doc, vector_model)
            if not auto_vectorize:
                collection = VectorStoreService()._existing_collection(file_doc.kb_id)
                if collection is not None and collection.get(where={"file_id": file_id}, limit=1)["ids"]:
                    raise ValueError("文件已有有效向量，暂不支持保留索引的手动重解析；请选择自动入库")
        except Exception as exc:
            file_doc.error_message = str(exc)
            if file_doc.status in (FileStatus.PENDING, FileStatus.PENDING_CONFIRM):
                file_doc.status = FileStatus.FAILED
            session.add(file_doc)
            safe_commit(session)
            raise
        previous_status, previous_progress = file_doc.status, file_doc.progress


        try:
            print(f"📄 [ParseTask] 文件信息: {file_doc.name}, 路径: {file_doc.local_path}")
            
            # 更新状态为解析中
            file_doc.status = FileStatus.PARSING
            file_doc.progress = 0
            file_doc.updated_at = datetime.utcnow()
            session.add(file_doc)
            safe_commit(session)
            
            # 初始化解析器
            parser = FileParser(kb_id=file_doc.kb_id)

            # --- 1. 获取向量模型配置 (用于决定 Chunk 是否超过向量模型限制) ---
            vector_max_tokens = 8192 # 默认安全值
            if vector_model:
                if vector_model == "sys_embedding":
                    vector_max_tokens = settings.EMBEDDING_MAX_CONTEXT_LENGTH
                else:
                    try:
                        custom_vec = session.get(CustomModel, vector_model)
                        if custom_vec:
                            vector_max_tokens = custom_vec.context_length
                            print(f"ℹ️ [ParseTask] 使用向量模型 {custom_vec.name} 配置: Max Tokens={vector_max_tokens}")
                    except Exception as e:
                        print(f"⚠️ [ParseTask] 获取向量模型 {vector_model} 配置失败: {e}")
            elif hasattr(settings, 'EMBEDDING_MAX_CONTEXT_LENGTH'):
                 vector_max_tokens = settings.EMBEDDING_MAX_CONTEXT_LENGTH

            # --- 2. 获取语义切分模型配置 ---
            semantic_max_tokens = None
            embedding_base_url = None
            embedding_api_key = None
            
            if embedding_model:
                if embedding_model == "sys_embedding":
                    # 处理系统预设模型
                    embedding_model = settings.EMBEDDING_MODEL
                    embedding_base_url = settings.EMBEDDING_BASE_URL
                    embedding_api_key = settings.EMBEDDING_API_KEY
                    semantic_max_tokens = settings.EMBEDDING_MAX_CONTEXT_LENGTH
                    print(f"ℹ️ [ParseTask] 语义切分使用系统预设模型: {embedding_model}")
                else:
                    try:
                        # 尝试从 CustomModel 获取配置
                        print(f"ℹ️ [ParseTask] 正在查询语义切分模型配置, ID: {embedding_model}")
                        custom_model_obj = session.get(CustomModel, embedding_model)
                        if custom_model_obj:
                             semantic_max_tokens = custom_model_obj.context_length
                             embedding_base_url = custom_model_obj.base_url
                             embedding_api_key = custom_model_obj.api_key
                             # 关键修复：将 embedding_model 更新为实际的模型名称 (model_name)，而非数据库 ID
                             embedding_model = custom_model_obj.model_name
                             print(f"ℹ️ [ParseTask] 语义切分使用自定义模型 {custom_model_obj.name} (ID: {custom_model_obj.id}, Name: {embedding_model}) - Base URL: {embedding_base_url}, Max Tokens: {semantic_max_tokens}")
                        else:
                            print(f"⚠️ [ParseTask] 未找到 ID 为 {embedding_model} 的自定义模型！将使用默认配置。")
                    except Exception as e:
                        print(f"⚠️ [ParseTask] 获取自定义模型信息失败: {e}")
                        import traceback
                        traceback.print_exc()



            # 解析文件
            file_doc.progress = 10
            session.add(file_doc)
            safe_commit(session)
            print(f"✅ [ParseTask] 开始调用 parser.parse()... Mode: {chunk_mode}")

            # 同步调用解析
            try:
                parsed_chunks = parser.parse(
                    file_path=file_doc.local_path,
                    chunk_mode=chunk_mode,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    separator=separator,
                    heading_level=heading_level,  # 新增：传递 heading_level
                    embedding_model=embedding_model,  # 传递 embedding_model
                    embedding_base_url=embedding_base_url, # 传递 base_url
                    embedding_api_key=embedding_api_key, # 传递 api_key
                    use_semantic=bool(embedding_model) or chunk_mode == "semantic", # 显式启用语义切分
                    embedding_max_tokens=vector_max_tokens, # 传递向量模型上下文长度
                    semantic_max_tokens=semantic_max_tokens # 传递语义模型上下文长度
                )

                event("ingest.parsed", file_id=file_id, chunk_count=len(parsed_chunks))
                print(f"✅ [ParseTask] 解析完成，获得 {len(parsed_chunks)} 个块")
            except Exception as parse_err:
                print(f"❌ [ParseTask] parser.parse() 内部抛出异常: {parse_err}")
                import traceback
                traceback.print_exc()
                raise parse_err

            if auto_vectorize:
                auto_data = []
                for chunk in parsed_chunks:
                    metadata = chunk.metadata if hasattr(chunk, "metadata") and chunk.metadata else {}
                    auto_data.append({
                        "content": chunk.content, "page_number": chunk.page_number,
                        "content_type": chunk.content_type, "image_path": chunk.image_path,
                        "parent_id": metadata.get("parent_id"), "heading_level": metadata.get("heading_level"),
                        "heading_text": metadata.get("heading_text"), "chunk_type": metadata.get("chunk_type", "content"),
                        "token_count": chunk.token_count,
                    })
                process_submitted_chunks(file_id, auto_data, vector_model=vector_model)
                return {"file_id": file_id, "chunk_count": len(parsed_chunks), "status": "pending_confirm"}

            # 清理旧的 Chunks (支持重解析)
            print("🧹 [ParseTask] 清理旧数据...")
            existing_chunks = session.exec(
                select(DocumentChunk).where(DocumentChunk.file_id == file_id)
            ).all()
            bm25_index = get_bm25_index()
            for c in existing_chunks:
                bm25_index.delete_chunk_index(session, c.id, file_doc.kb_id)
                session.delete(c)
            session.flush()

            # This preview path has no active vectors. Only the indexing path replaces vectors.
            # 保存 Chunks
            db_chunks = []
            print("🔄 [ParseTask] 开始保存分块...")

            for idx, chunk in enumerate(parsed_chunks):
                # 映射 ContentType
                try:
                    c_type = ContentType(chunk.content_type)
                except ValueError:
                    c_type = ContentType.TEXT

                # 获取 metadata 中的新字段（结构化模式会有这些信息）
                metadata = chunk.metadata if hasattr(chunk, 'metadata') and chunk.metadata else {}

                db_chunk = DocumentChunk(
                    file_id=file_id,
                    content=chunk.content,
                    page_number=chunk.page_number,
                    content_type=c_type,
                    image_path=chunk.image_path,
                    original_index=idx,
                    # 新增字段
                    parent_id=metadata.get("parent_id"),
                    heading_level=metadata.get("heading_level"),
                    heading_text=metadata.get("heading_text"),
                    chunk_type=metadata.get("chunk_type", "content"),
                    token_count=chunk.token_count, # Add token_count
                )
                db_chunks.append(db_chunk)
                session.add(db_chunk)

            event("ingest.chunks", file_id=file_id, chunk_count=len(db_chunks))
            print(f"✅ [ParseTask] 分块处理完成，共生成 {len(db_chunks)} 个数据库记录块")

            # 更新文件状态为解析完成 (PENDING_CONFIRM)
            file_doc.status = FileStatus.PENDING_CONFIRM
            file_doc.progress = 50
            file_doc.updated_at = datetime.utcnow()
            session.add(file_doc)
            
            # 暂时不更新知识库分块计数，因为还未入库
            # ...

            safe_commit(session)
            print("🎉 [ParseTask] 解析完成，等待用户确认入库。")
            
            return {
                "file_id": file_id,
                "chunk_count": len(db_chunks) if 'db_chunks' in locals() else 0,
                "status": "pending_confirm",
            }

        except IndexRetirementError:
            # The new SQL/index state is already committed; do not restore stale state.
            raise
        except Exception as e:
            # 更新状态为失败
            print(f"❌ [ParseTask] 全局异常捕获: {e}")
            import traceback
            traceback.print_exc()
            
            # 重新获取 session 中的对象以防过期
            session.rollback()
            file_doc = session.get(FileDocument, file_id)
            if file_doc:
                file_doc.status = previous_status if old_chunk else FileStatus.FAILED
                if old_chunk:
                    file_doc.progress = previous_progress
                file_doc.error_message = str(e)
                file_doc.updated_at = datetime.utcnow()
                session.add(file_doc)
                safe_commit(session)
            
            raise e
