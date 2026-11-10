"""Knowledge-base task monitoring and explicit resolution of uncertain attempts."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case
from sqlmodel import Session, func, select

from app.core.database import get_session
from app.models import FileDocument, KnowledgeBase
from app.schemas import ApiResponse
from app.tasks.dispatch import FileTaskConflict, FileTaskReceipt, receipt_view, resolve_unknown

router = APIRouter(tags=["File"])


class Resolution(BaseModel):
    action: Literal["resume", "retry", "terminate"]
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/files/{file_id}/tasks/{task_id}/resolve", response_model=ApiResponse)
def resolve_file_task(file_id: str, task_id: str, decision: Resolution):
    try:
        receipt = resolve_unknown(file_id, task_id, decision.action, decision.reason.strip())
    except FileTaskConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return ApiResponse(data=receipt)


@router.get("/knowledge-bases/{kb_id}/tasks", response_model=ApiResponse)
def list_kb_tasks(kb_id: str, limit: int = Query(50, ge=1, le=200),
                  offset: int = Query(0, ge=0), session: Session = Depends(get_session)):
    kb = session.get(KnowledgeBase, kb_id)
    if not kb or kb.is_deleted:
        raise HTTPException(404, detail="知识库不存在")
    receipt = FileTaskReceipt
    queue = case((receipt.stage == "index", "document.index"), else_="document.parse")
    summary = {name: {"waiting": 0, "running": 0, "failed": 0, "unknown": 0,
                      "oldest_wait_seconds": None} for name in ("document.parse", "document.index")}
    counts = session.exec(select(queue, receipt.status, func.count(), func.min(receipt.updated_at))
                          .join(FileDocument, receipt.file_id == FileDocument.id)
                          .where(FileDocument.kb_id == kb_id).group_by(queue, receipt.status)).all()
    now = datetime.utcnow()
    waiting = {"QUEUED", "RETRY_WAIT", "PUBLISH_UNKNOWN"}
    for name, status, count, oldest in counts:
        group = summary[name]
        if status in waiting:
            group["waiting"] += count
            age = max(0, int((now - oldest).total_seconds()))
            group["oldest_wait_seconds"] = max(age, group["oldest_wait_seconds"] or 0)
        elif status in {"RUNNING", "FAILED", "UNKNOWN"}:
            group[{"RUNNING": "running", "FAILED": "failed", "UNKNOWN": "unknown"}[status]] += count
    entries = session.exec(select(receipt, FileDocument)
        .join(FileDocument, receipt.file_id == FileDocument.id).where(FileDocument.kb_id == kb_id)
        .order_by(case((receipt.active_file.is_not(None), 0), else_=1),
                  receipt.updated_at.desc(), receipt.task_id).offset(offset).limit(limit + 1)).all()
    rows = [{**receipt_view(row), "file_name": doc.display_name or doc.name,
             "active_build_id": doc.active_build_id} for row, doc in entries[:limit]]
    return ApiResponse(data={"queues": summary, "tasks": rows, "has_more": len(entries) > limit})
