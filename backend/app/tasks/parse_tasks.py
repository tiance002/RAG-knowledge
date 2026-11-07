from app.services.observability import event, observe, enabled
"""
文件解析任务
"""

import asyncio
from datetime import datetime
from sqlmodel import Session, select

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
_UNSET_VLM = object()

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
    if kb is None:
        raise ValueError("KNOWLEDGE_BASE_NOT_FOUND")
    from app.services.model_bindings import resolve_model
    binding = resolve_model(session, "embedding", vector_model or kb.embedding_model, use_default=False)
    embedding = EmbeddingService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
    model_id = binding.model_name
    vectors = run_async(embedding.embed_documents(contents, model_id=model_id))
    if len(vectors) != len(contents) or any(not v for v in vectors):
        raise ValueError("Embedding returned incomplete vectors")
    if vectors:
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("Embedding returned vectors with inconsistent dimensions")
        VectorStoreService().validate_dimension(file_doc.kb_id, vectors[0])
    return vectors


def _resolve_vlm_binding(session, kb, identity=_UNSET_VLM):
    from app.services.model_bindings import resolve_model
    if identity is _UNSET_VLM:
        # Direct calls may choose defaults once. A queued explicit None/empty
        # identity is a frozen no-selection and must never read a new default.
        from app.services.runtime_settings import get_runtime_settings
        identity = kb.vlm_model if kb.vlm_model else get_runtime_settings(session).default_vlm_model
    return resolve_model(session, "vlm", identity, allow_unset=True, use_default=False)


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
            from app.services.parent_child import remap_and_validate_graph
            chunks_data = remap_and_validate_graph(chunks_data, file_id)
            index_data = [c for c in chunks_data if c.get("chunk_type") != "parent"]
            embeddings = _prepare_embeddings(session, file_doc, vector_model, [c.get("content", "") for c in index_data])
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
                    id=data["id"], file_id=file_id, content=data.get("content", ""),
                    page_number=data.get("page_number", 0), content_type=content_type,
                    image_path=data.get("image_path", ""), original_index=idx,
                    parent_id=data.get("parent_id"), heading_level=data.get("heading_level"),
                    heading_text=data.get("heading_text"), chunk_type=data.get("chunk_type", "content"),
                    token_count=data.get("token_count", 0),
                    start_offset=data.get("start_offset"), end_offset=data.get("end_offset"),
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
                if chunk.chunk_type != "parent":
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
            for chunk in sorted(new_chunks, key=lambda c: c.chunk_type != "parent"):
                session.add(chunk)
                if chunk.chunk_type != "parent":
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
    chunk_size: int = None,
    chunk_overlap: int = None,
    separator: str = "\n\n",
    heading_level: int = 2,
    embedding_model: str = None,
    vector_model: str = None,
    auto_vectorize: bool = False,
    vlm_model_id=_UNSET_VLM,
    parent_child: bool = False,
    parent_chunk_size: int = 4096,
    parent_chunk_overlap: int = 0,
    child_chunk_size: int = 384,
    child_chunk_overlap: int = 48,
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
    
    from app.services.runtime_settings import get_runtime_settings
    defaults = get_runtime_settings() if chunk_size is None or chunk_overlap is None else None
    chunk_size = defaults.chunk_size if chunk_size is None else int(chunk_size)
    chunk_overlap = defaults.chunk_overlap if chunk_overlap is None else int(chunk_overlap)
    heading_level = 2 if heading_level is None else int(heading_level)
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("CHUNK_CONFIGURATION_INVALID")

    from app.services.parent_child import ParentChildConfig, build_parent_child_chunks
    parent_child_config = ParentChildConfig(parent_chunk_size, parent_chunk_overlap,
                                           child_chunk_size, child_chunk_overlap).validate() if parent_child else None

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

            from app.services.model_bindings import resolve_model
            kb = session.get(KnowledgeBase, file_doc.kb_id)
            vector_max_tokens = settings.EMBEDDING_MAX_CONTEXT_LENGTH
            if vector_model or auto_vectorize or chunk_mode in ('semantic', 'structure'):
                vector_binding = resolve_model(session, "embedding", vector_model or kb.embedding_model,
                                               use_default=False)
                vector_max_tokens = vector_binding.context_length
            semantic_max_tokens = None
            embedding_base_url = embedding_api_key = None
            if embedding_model or chunk_mode in ('semantic', 'structure'):
                semantic = resolve_model(session, "embedding", embedding_model or kb.embedding_model,
                                         use_default=False)
                embedding_model = semantic.model_name
                embedding_base_url, embedding_api_key = semantic.base_url, semantic.api_key
                semantic_max_tokens = semantic.context_length

            # 解析文件
            file_doc.progress = 10
            session.add(file_doc)
            safe_commit(session)
            print(f"✅ [ParseTask] 开始调用 parser.parse()... Mode: {chunk_mode}")

            # 同步调用解析
            try:
                parsed_chunks = parser.parse(
                    file_path=file_doc.local_path,
                    # Retain parser special-block semantics; use larger upstream
                    # pieces, then enforce exact character windows below.
                    chunk_mode=chunk_mode,
                    chunk_size=(parent_chunk_size if parent_child else chunk_size),
                    chunk_overlap=(0 if parent_child else chunk_overlap),
                    separator=separator,
                    heading_level=heading_level,  # 新增：传递 heading_level
                    embedding_model=embedding_model,  # 传递 embedding_model
                    embedding_base_url=embedding_base_url, # 传递 base_url
                    embedding_api_key=embedding_api_key, # 传递 api_key
                    use_semantic=bool(embedding_model) or chunk_mode == "semantic", # 显式启用语义切分
                    embedding_max_tokens=vector_max_tokens, # 传递向量模型上下文长度
                    semantic_max_tokens=semantic_max_tokens # 传递语义模型上下文长度
                )

                # Caption actual image chunks once, using the KB-bound VLM. Failure
                # leaves extracted text/OCR and image provenance available.
                from app.services.vlm import VLMService, describe_parsed_images
                from app.services.model_bindings import ModelBindingError
                vlm = None
                vlm_binding_error = None
                try:
                    bound_vlm = _resolve_vlm_binding(session, kb, vlm_model_id)
                    if bound_vlm:
                        vlm = VLMService(base_url=bound_vlm.base_url, api_key=bound_vlm.api_key,
                                         model=bound_vlm.model_name)
                except ModelBindingError as exc:
                    vlm_binding_error = type(exc).__name__
                description_summary = describe_parsed_images(parsed_chunks, file_doc.kb_id, vlm, run_async)
                for c in parsed_chunks:
                    if (c.metadata or {}).get('content_origin') == 'model_generated_image_description':
                        c.token_count = parser.splitter.count_tokens(c.content)
                if vlm_binding_error:
                    description_summary['binding_error'] = vlm_binding_error
                    description_summary['binding_status'] = 'INVALID'
                    for c in parsed_chunks:
                        if c.content_type == 'image':
                            c.metadata = {**(c.metadata or {}), 'image_description': {'status':'MODEL_BINDING_INVALID'}}
                if any((c.metadata or {}).get('image_description') for c in parsed_chunks):
                    from pathlib import Path
                    if Path(file_doc.local_path).suffix.lower() == '.pdf':
                        from app.services.pdf_provenance import digest_bytes, write_manifest
                        write_manifest(settings.PDF_AUDIT_DIR,
                            digest_bytes(Path(file_doc.local_path).read_bytes()),
                            getattr(parser, 'pdf_page_records', []), parsed_chunks)
                if parent_child:
                    parsed_chunks = build_parent_child_chunks(parsed_chunks, parent_child_config,
                        count_tokens=parser.splitter.count_tokens)
                    event("ingest.parent_child", file_id=file_id, unit="characters",
                          parent_count=sum((c.metadata or {}).get("chunk_type") == "parent" for c in parsed_chunks),
                          child_count=sum((c.metadata or {}).get("chunk_type") == "child" for c in parsed_chunks),
                          parent_chunk_size=parent_chunk_size, parent_chunk_overlap=parent_chunk_overlap,
                          child_chunk_size=child_chunk_size, child_chunk_overlap=child_chunk_overlap)
                event("ingest.parsed", file_id=file_id, chunk_count=len(parsed_chunks),
                      image_descriptions=description_summary)
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
                        "id": metadata.get("id"), "content": chunk.content, "page_number": chunk.page_number,
                        "content_type": chunk.content_type, "image_path": chunk.image_path,
                        "parent_id": metadata.get("parent_id"), "heading_level": metadata.get("heading_level"),
                        "heading_text": metadata.get("heading_text"), "chunk_type": metadata.get("chunk_type", "content"),
                        "token_count": chunk.token_count,
                        "start_offset": metadata.get("start_offset"), "end_offset": metadata.get("end_offset"),
                    })
                process_submitted_chunks(file_id, auto_data, vector_model=vector_model)
                return {"file_id": file_id, "chunk_count": len(parsed_chunks), "status": "parsed", "image_descriptions": description_summary}

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
                    id=metadata.get("id") or str(__import__("uuid").uuid4()),
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
                    start_offset=metadata.get("start_offset"), end_offset=metadata.get("end_offset"),
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
                "image_descriptions": description_summary,
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
