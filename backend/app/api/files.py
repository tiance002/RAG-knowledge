from app.services.observability import observe, event
"""
文件管理 API 路由
"""

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, status, Body, Query
from fastapi.responses import Response
from typing import Annotated, Optional
from sqlmodel import Session, select, func
from typing import List, Dict
import os
import aiofiles
from datetime import datetime

from app.core.database import get_session
from app.core.config import get_settings
from app.models import KnowledgeBase, FileDocument, FileStatus, generate_uuid, DocumentChunk
from app.schemas import FileUploadResponse, FileStatusResponse, ApiResponse
from app.services.file_retirement import retire_source, clean_source
from app.tasks.dispatch import (dispatch_file_task, FileTaskBusy, FileTaskConflict,
                                FileTaskReceipt, lookup_file_request, receipt_view, cancel_in_session)

router = APIRouter(tags=["File"])
settings = get_settings()


RequestKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=256)]


def _dispatch(file_id, operation, payload, **options):
    try:
        return dispatch_file_task(file_id, operation, payload, **options)
    except FileTaskBusy as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "task_id": exc.task_id}) from exc
    except FileTaskConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404 if str(exc) == 'FILE_NOT_FOUND' else 400, detail=str(exc)) from exc


def _replay(file_id, operation, key, raw):
    if key is None:
        return None
    try:
        return lookup_file_request(file_id, operation, key, raw)
    except FileTaskConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc


def begin_file_mutation(session):
    # Reserve the SQLite writer before checking active_file or reading live chunks.
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def reject_busy_files(session, file_ids):
    row = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.active_file.in_(file_ids)).limit(1)).first()
    if row:
        raise HTTPException(409, detail={"message": "文件正在处理，请查看任务状态", "task_id": row.task_id})


@router.get("/files/{file_id}/tasks", response_model=ApiResponse)
def list_file_tasks(file_id: str, session: Session = Depends(get_session)):
    if session.get(FileDocument, file_id) is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    rows = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.file_id == file_id)
                        .order_by(FileTaskReceipt.created_at.desc()).limit(20)).all()
    return ApiResponse(data=[receipt_view(row) for row in rows])


@router.get("/files/{file_id}/tasks/{task_id}", response_model=ApiResponse)
def get_file_task(file_id: str, task_id: str, session: Session = Depends(get_session)):
    row = session.get(FileTaskReceipt, task_id)
    if row is None or row.file_id != file_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    from app.tasks.dispatch import FileTaskAttempt, FileTaskEffect, FileTaskEvent
    attempts = session.exec(select(FileTaskAttempt).where(FileTaskAttempt.task_id == task_id)
        .order_by(FileTaskAttempt.attempt_no)).all()
    effects = session.exec(select(FileTaskEffect).where(FileTaskEffect.task_id == task_id)
        .order_by(FileTaskEffect.updated_at, FileTaskEffect.effect_key)).all()
    events = session.exec(select(FileTaskEvent).where(FileTaskEvent.task_id == task_id)
        .order_by(FileTaskEvent.created_at)).all()
    data = receipt_view(row)
    data["attempts"] = [dict(attempt_no=a.attempt_no, stage=a.stage, worker=a.worker_id, outcome=a.outcome,
        started_at=a.started_at, finished_at=a.finished_at, error=a.error_code) for a in attempts]
    data["effects"] = [dict(effect_key=e.effect_key, stage=e.stage, state=e.state,
        provider_request_id=e.provider_request_id, stored_result=bool(e.result_ref)) for e in effects]
    data["decisions"] = [dict(action=e.action, reason=e.reason, created_at=e.created_at) for e in events]
    return ApiResponse(data=data)


def get_file_extension(filename: str) -> str:
    """获取文件扩展名"""
    return os.path.splitext(filename)[1].lower()


@router.get('/files/{file_id}/source')
def read_pdf_source(file_id: str, kb_id: str, sha256: str = Query(pattern=r'^[0-9a-f]{64}$'),
                    session: Session = Depends(get_session)):
    """Read the exact source version referenced by a same-file table expansion."""
    import hashlib
    from pathlib import Path
    from urllib.parse import quote
    doc = session.get(FileDocument, file_id)
    kb = session.get(KnowledgeBase, kb_id)
    if not doc or not kb or kb.is_deleted or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail='资料不存在')
    path = Path(doc.local_path)
    try:
        if path.suffix.lower() != '.pdf' or path.stat().st_size > 50_000_000:
            raise HTTPException(status_code=404, detail='原始 PDF 不可用')
        data = path.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail='原始 PDF 不可用')
    if hashlib.sha256(data).hexdigest() != sha256:
        raise HTTPException(status_code=409, detail='原始文件版本已变化，无法打开该引用')
    if not data.startswith(b'%PDF-'):
        raise HTTPException(status_code=404, detail='原始 PDF 不可用')
    return Response(content=data, media_type='application/pdf', headers={
        'Content-Disposition': "inline; filename*=UTF-8''" + quote(doc.name, safe=''),
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@router.post(
    "/knowledge-bases/{kb_id}/upload",
    response_model=ApiResponse[FileUploadResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
@observe("ingest.upload")
async def upload_file(
    kb_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    action: str = Query(default="auto"),
    target_file_id: Optional[str] = Query(default=None),
):
    """上传文件；同名操作显式选择，替换保留历史版本。"""
    # 检查知识库是否存在
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    # 检查文件格式
    ext = get_file_extension(file.filename)
    if ext not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40001, "message": f"不支持的文件格式: {ext}"},
        )

    # Stream the exact saved bytes; UUID storage keys never contain user paths.
    import hashlib
    from pathlib import Path
    from app.services.file_uploads import register_upload
    file_id = generate_uuid()
    directory = Path(settings.UPLOAD_DIR) / kb_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{file_id}_source{ext}"
    digest = hashlib.sha256()
    size = 0
    retained = False
    try:
        async with aiofiles.open(path, "xb") as handle:
            while part := await file.read(1024 * 1024):
                size += len(part)
                if size > settings.MAX_FILE_SIZE:
                    raise HTTPException(400, detail={"code": 40002, "message": "文件过大，最大支持 100MB"})
                digest.update(part)
                await handle.write(part)
        result = register_upload(session, kb_id=kb_id, file_id=file_id,
                                 name=file.filename, path=path, size=size,
                                 sha256=digest.hexdigest(), action=action,
                                 target_file_id=target_file_id)
        retained = not result["duplicate"]
        event("ingest.uploaded" if retained else "ingest.upload_duplicate",
              file_id=result["file_id"], kb_id=kb_id, status=result["status"],
              outcome=result["outcome"])
        return ApiResponse(data=FileUploadResponse(**result))
    finally:
        if not retained:
            session.rollback()
            path.unlink(missing_ok=True)



@router.get(
    "/knowledge-bases/{kb_id}/files",
    response_model=ApiResponse[List[FileStatusResponse]],
)
async def list_files(
    kb_id: str,
    session: Session = Depends(get_session),
):
    """获取文件列表及状态"""
    # 检查知识库是否存在
    kb = session.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "知识库不存在"},
        )

    statement = select(FileDocument).where(
        FileDocument.kb_id == kb_id
    ).order_by(FileDocument.created_at.desc())

    files = session.exec(statement).all()

    # 批量查询 chunk 数量
    chunk_counts_map = {}
    if files:
        file_ids = [f.id for f in files]
        chunk_counts = session.exec(
            select(DocumentChunk.file_id, func.count(DocumentChunk.id))
            .where(DocumentChunk.file_id.in_(file_ids))
            .group_by(DocumentChunk.file_id)
        ).all()
        chunk_counts_map = {file_id: count for file_id, count in chunk_counts}

    file_list = [
        FileStatusResponse(
            id=f.id,
            name=f.name,
            original_name=f.original_name or f.name,
            display_name=f.display_name or f.name,
            sha256=f.sha256,
            supersedes_file_id=f.supersedes_file_id,
            size=f.size,
            status=f.status.value,
            progress=f.progress,
            error_message=f.error_message,
            chunk_count=chunk_counts_map.get(f.id, 0),
            created_at=f.created_at,
        )
        for f in files
    ]
    
    return ApiResponse(data=file_list)


@router.get("/files/{file_id}", response_model=ApiResponse[FileStatusResponse])
async def get_file_detail(
    file_id: str,
    session: Session = Depends(get_session),
):
    """获取文件详情"""
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )
        
    # 查询 chunk 数量
    chunk_count = session.exec(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.file_id == file_id)
    ).one()

    return ApiResponse(
        data=FileStatusResponse(
            id=file_doc.id,
            name=file_doc.name,
            original_name=file_doc.original_name or file_doc.name,
            display_name=file_doc.display_name or file_doc.name,
            sha256=file_doc.sha256,
            supersedes_file_id=file_doc.supersedes_file_id,
            size=file_doc.size,
            status=file_doc.status.value,
            progress=file_doc.progress,
            error_message=file_doc.error_message,
            chunk_count=chunk_count,
            created_at=file_doc.created_at,
        )
    )


@router.post("/files/{file_id}/parse", response_model=ApiResponse)
def parse_file_manually(
    file_id: str,
    session: Session = Depends(get_session),
    # 使用 Body() 正确解析请求体参数
    config: Optional[dict] = Body(default=None),
    request_key: RequestKey = None,
):
    """手动触发文件解析"""
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )


    # 提取 config 中的参数
    config = config or {}
    prior = _replay(file_id, "parse", request_key, config)
    if prior:
        return ApiResponse(data=prior)
    chunk_mode = config.get('chunk_mode')
    chunk_size = config.get('chunk_size')
    chunk_overlap = config.get('chunk_overlap')
    heading_level = config.get('heading_level')
    auto_vectorize = config.get('auto_vectorize', False)
    embedding_model = config.get('embedding_model')
    vector_model = config.get('vector_model')

    if not auto_vectorize:
        from app.models import ChunkIndex
        published = file_doc.active_build_id or file_doc.status in (FileStatus.READY, FileStatus.PARSED)
        if not published:
            published = session.exec(select(ChunkIndex.id).join(DocumentChunk, DocumentChunk.id == ChunkIndex.chunk_id)
                .where(ChunkIndex.kb_id == file_doc.kb_id, DocumentChunk.file_id == file_id).limit(1)).first()
        if not published:
            # A historical vector-only index may lack postings and a build pointer.
            ids = session.exec(select(DocumentChunk.id).where(DocumentChunk.file_id == file_id)).all()
            if ids:
                from app.services.vector_store import VectorStoreService
                collection = VectorStoreService()._existing_collection(file_doc.kb_id)
                published = collection and collection.get(ids=list(ids), limit=1, include=[])["ids"]
        if published:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="文件已有有效向量，暂不支持保留索引的手动重解析；请选择自动入库")

    task = _dispatch(file_id, 'parse', dict(
        chunk_mode=chunk_mode, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        separator=config.get('separator', "\n\n"), heading_level=heading_level,
        auto_vectorize=auto_vectorize, embedding_model=embedding_model,
        vector_model=vector_model,
        **{key: config[key] for key in ("parent_child", "parent_chunk_size", "parent_chunk_overlap", "child_chunk_size", "child_chunk_overlap") if key in config},
    ), request_key=request_key, expected_generation=file_doc.desired_generation, request_input=config)
    return ApiResponse(message="解析任务已入队", data=task)


@router.post("/files/{file_id}/chunks/submit", response_model=ApiResponse)
def submit_file_chunks(
    file_id: str,
    data: dict, # { "chunks": [...] }
    session: Session = Depends(get_session),
    request_key: RequestKey = None,
):
    """提交确认的分块结果，触发入库"""
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )
        
    raw = {"action": "submit", "data": data}
    prior = _replay(file_id, "index", request_key, raw)
    if prior:
        return ApiResponse(data=prior)
    chunks = data.get("chunks", [])
    vector_model = data.get("vector_model")
    if not chunks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40003, "message": "分块数据为空"},
        )

    # Existing IDs are resolved only within this file; the client may edit content
    # but cannot substitute another file's structure or provenance.
    existing_ids = [item.get("id") for item in chunks if isinstance(item, dict) and item.get("id")]
    originals = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id, DocumentChunk.id.in_(existing_ids))).all() if existing_ids else []
    by_id = {chunk.id: chunk for chunk in originals}
    normalized = []
    for item in chunks:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="无效的分块数据")
        chunk_id = item.get("id")
        if chunk_id:
            original = by_id.get(chunk_id)
            if original is None:
                raise HTTPException(status_code=400, detail="分块不属于当前文件或已失效")
            item = {**item, "page_number": original.page_number, "content_type": original.content_type.value,
                    "image_path": original.image_path, "parent_id": original.parent_id,
                    "heading_level": original.heading_level, "heading_text": original.heading_text,
                    "id": original.id, "chunk_type": original.chunk_type, "token_count": original.token_count,
                    "start_offset": original.start_offset, "end_offset": original.end_offset}
        normalized.append(item)

    task = _dispatch(file_id, "index", {"chunks_data": normalized, "vector_model": vector_model},
                     request_key=request_key, request_input=raw, expected_generation=file_doc.desired_generation)
    return ApiResponse(message="分块入库任务已入队", data=task)


@router.post("/files/{file_id}/vectorize", response_model=ApiResponse)
def vectorize_file_manually(
    file_id: str,
    session: Session = Depends(get_session),
    request_key: RequestKey = None,
):
    """手动触发向量化入库 (用于正常模式确认)"""
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )
        
    raw = {"action": "vectorize"}
    prior = _replay(file_id, "index", request_key, raw)
    if prior:
        return ApiResponse(data=prior)
    chunks_data = _chunk_snapshot(session, file_id)
    
    if not chunks_data:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40004, "message": "文件尚未解析，无法入库"},
        )
        
    task = _dispatch(file_id, "index", {"chunks_data": chunks_data}, request_key=request_key,
                     request_input=raw, expected_generation=file_doc.desired_generation)
    return ApiResponse(message="入库任务已入队", data=task)


def _chunk_snapshot(session, file_id):
    chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id)
                          .order_by(DocumentChunk.original_index, DocumentChunk.id)).all()
    return [
        {
            "id": c.id, "start_offset": c.start_offset, "end_offset": c.end_offset,
            "content": c.content,
            "page_number": c.page_number,
            "content_type": c.content_type.value,
            "image_path": c.image_path,
            "vlm_description": c.vlm_description if hasattr(c, "vlm_description") else None,
            "parent_id": c.parent_id,
            "heading_level": c.heading_level,
            "heading_text": c.heading_text,
            "chunk_type": c.chunk_type,
            "token_count": c.token_count,
        }
        for c in chunks
    ]

@router.get("/files/{file_id}/chunks", response_model=ApiResponse)
async def get_file_chunks(
    file_id: str,
    session: Session = Depends(get_session),
):
    """获取文件的 Chunks"""
    from app.models import DocumentChunk
    
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )
        
    chunks = session.exec(
        select(DocumentChunk)
        .where(DocumentChunk.file_id == file_id)
        .order_by(DocumentChunk.original_index)
    ).all()
    
    return ApiResponse(data=chunks)


@router.put("/chunks/{chunk_id}", response_model=ApiResponse)
def update_chunk(
    chunk_id: str,
    data: dict, # { "content": "..." }
    session: Session = Depends(get_session),
    request_key: RequestKey = None,
):
    """更新 Chunk 内容"""
    from app.models import ChunkIndex
    raw = {"action": "edit", "chunk_id": chunk_id, "content": data.get("content")}
    if data.get("file_id"):
        prior = _replay(data["file_id"], "index", request_key, raw)
        if prior:
            return ApiResponse(data=prior)
    begin_file_mutation(session)
    chunk = session.get(DocumentChunk, chunk_id)
    if not chunk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "Chunk 不存在"},
        )
        
    file_id = chunk.file_id
    if data.get("file_id", file_id) != file_id:
        raise HTTPException(409, detail="分块不属于当前文件")
    if "content" not in data or data["content"] == chunk.content:
        return ApiResponse(data=chunk)
    if not isinstance(data["content"], str) or not data["content"].strip():
        raise HTTPException(400, detail="分块内容不能为空")
    doc = session.get(FileDocument, file_id)
    if doc is None:
        raise HTTPException(409, detail="分块缺少文件归属")
    prior = _replay(file_id, "index", request_key, raw)
    if prior:
        return ApiResponse(data=prior)
    reject_busy_files(session, [file_id])
    published = doc.active_build_id or doc.status in (FileStatus.READY, FileStatus.PARSED) or session.exec(
        select(ChunkIndex.id).where(ChunkIndex.kb_id == doc.kb_id, ChunkIndex.chunk_id == chunk_id)
        .limit(1)).first()
    if published:
        generation = doc.desired_generation
        chunks = _chunk_snapshot(session, file_id)
        for item in chunks:
            if item["id"] == chunk_id:
                item["content"] = data["content"]
        session.rollback()
        task = _dispatch(file_id, "index", {"chunks_data": chunks}, request_input=raw,
                         expected_generation=generation, request_key=request_key)
        return ApiResponse(message="修改已入队，发布后生效", data=task)
    chunk.content = data["content"]
    chunk.source_metadata_json = None
    doc.desired_generation += 1
    session.add(doc)
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    
    return ApiResponse(data=chunk)


@router.delete("/files/{file_id}", response_model=ApiResponse)
async def delete_file(
    file_id: str,
    session: Session = Depends(get_session),
    cancel_active: bool = False,
):
    """删除文件"""
    begin_file_mutation(session)
    file_doc = session.get(FileDocument, file_id)
    if not file_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": 40401, "message": "文件不存在"},
        )

    if cancel_active:
        cancel_in_session(session, file_id, "用户取消任务并删除文件")
    else:
        reject_busy_files(session, [file_id])
    source_retirement = retire_source(session, file_doc)
    from app.services.index_builds import retire_file_builds
    retire_file_builds(session, file_id)

    # 删除数据库记录
    # 级联删除 chunks
    from app.models import DocumentChunk
    chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id)).all()
    from app.services.bm25 import get_bm25_index
    bm25_index = get_bm25_index()
    for chunk in chunks:
        bm25_index.delete_chunk_index(session, chunk.id, file_doc.kb_id)
        session.delete(chunk)

    kb = session.get(KnowledgeBase, file_doc.kb_id)
    session.delete(file_doc)
    session.flush()
    if kb:
        kb.chunk_count = session.exec(select(func.count(DocumentChunk.id)).join(FileDocument).where(FileDocument.kb_id == kb.id)).one()
    session.commit()
    clean_source(session.get_bind(), source_retirement)
    return ApiResponse(message="删除成功")
