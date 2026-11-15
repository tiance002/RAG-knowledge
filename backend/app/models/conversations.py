"""Conversation records are independent of the lifetime of indexed sources.

Only conversation-owned identifiers have foreign keys. In particular deleting a
KB, document or index build must never delete a conversation or its evidence.
Schema registration is explicit; importing this module does not create tables.
"""
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _id() -> str:
    return str(uuid4())


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("legacy_source_id", "legacy_content_hash", name="uq_chat_session_legacy"),
        Index("ix_chat_sessions_archive_updated", "archived_at", "updated_at"),
    )

    id: str = Field(default_factory=_id, primary_key=True)
    title: str = "新对话"
    scope_mode: str = "SELECTED"
    kb_ids_json: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    config_overrides_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    revision: int = 1
    active_turn_id: Optional[str] = None
    legacy_source_id: Optional[str] = None
    legacy_content_hash: Optional[str] = None
    legacy_origin_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    legacy_metadata_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    archived_at: Optional[datetime] = None


class ChatTurn(SQLModel, table=True):
    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence_no", name="uq_chat_turn_sequence"),
        UniqueConstraint("session_id", "client_request_id", name="uq_chat_turn_request"),
        Index("ix_chat_turn_session_status_sequence", "session_id", "status", "sequence_no"),
    )

    id: str = Field(default_factory=_id, primary_key=True)
    session_id: str = Field(foreign_key="chat_sessions.id", index=True)
    sequence_no: int
    user_content: str = Field(sa_column=Column(Text, nullable=False))
    assistant_content: str = Field(default="", sa_column=Column(Text, nullable=False))
    client_request_id: str
    request_hash: str
    status: str = "RUNNING"
    generation_status: str = "PENDING"
    effective_config_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    metadata_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    understanding_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    legacy_metadata_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    cancel_requested: bool = False
    lease_token: Optional[str] = None
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


class ChatEvidenceBody(SQLModel, table=True):
    __tablename__ = "chat_evidence_bodies"

    # Hash the exact UTF-8 bytes, without whitespace or Unicode normalization.
    content_hash: str = Field(primary_key=True)
    content: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatTurnReference(SQLModel, table=True):
    __tablename__ = "chat_turn_references"
    __table_args__ = (
        UniqueConstraint("turn_id", "position", name="uq_chat_reference_position"),
    )

    id: str = Field(default_factory=_id, primary_key=True)
    turn_id: str = Field(foreign_key="chat_turns.id", index=True)
    position: int
    citation_index: Optional[int] = None
    body_hash: Optional[str] = Field(default=None, foreign_key="chat_evidence_bodies.content_hash", index=True)
    kb_id: Optional[str] = None
    file_id: Optional[str] = None
    chunk_id: Optional[str] = None
    build_id: Optional[str] = None
    file_hash: Optional[str] = None
    source_type: Optional[str] = None
    locator_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    delivery_status: str = "UNKNOWN"
    trust_status: str = "UNKNOWN"
    payload_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=datetime.utcnow)
