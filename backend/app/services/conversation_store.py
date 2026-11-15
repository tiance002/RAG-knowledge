"""Transactional conversation persistence; no provider calls or schema creation.

Leases describe ownership, not whether a remote model was called. Expired work
is UNKNOWN and is never retried here. Evidence is a historical snapshot and
must be revalidated by the context service before being used in a new request.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import math
from uuid import uuid4

from sqlalchemy import func, text, update
from sqlmodel import Session, select

from app.models.conversations import ChatEvidenceBody, ChatSession, ChatTurn, ChatTurnReference


class ConversationError(ValueError):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


LEASE_SECONDS = 300
_BOOL_CONFIG = {"use_rewrite", "rerank_enabled", "web_search_enabled"}
_STR_CONFIG = {"model_id", "rerank_model_id"}
_INT_CONFIG = {"top_k": (1, 200), "rerank_top_k": (1, 200),
               "context_window": (0, 5), "max_chat_history_rounds": (1, 100)}
_FLOAT_CONFIG = {"score_threshold": (0, 1), "rerank_score_threshold": (0, 1),
                 "vector_weight": (0, 1), "bm25_weight": (0, 1)}
_ENUM_CONFIG = {"mode": {"chat", "qa", "agent", "normal"},
                "search_mode": {"vector", "fulltext", "hybrid"}}
_CONFIG_KEYS = _BOOL_CONFIG | _STR_CONFIG | _INT_CONFIG.keys() | _FLOAT_CONFIG.keys() | _ENUM_CONFIG.keys()
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "UNKNOWN", "LEGACY_IMPORTED"}
_SUCCESS = {"COMPLETED"}
_SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "access_token", "accesstoken",
                   "refresh_token", "refreshtoken", "credentials", "credential", "private_key", "privatekey"}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConversationError("INVALID_JSON", "Value must contain only finite JSON data", 422) from exc


def _load(value, fallback=None):
    return json.loads(value) if value is not None else fallback


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value):
    return value.isoformat() + "Z" if value is not None else None


def _no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith(("_api_key", "_password", "_secret")):
                raise ConversationError("INVALID_CONFIG", "Credentials cannot be stored in conversation configuration", 422)
            _no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)


def validate_config_overrides(value):
    if not isinstance(value, dict):
        raise ConversationError("INVALID_CONFIG", "config_overrides must be an object", 422)
    _no_credentials(value)
    if set(value) - _CONFIG_KEYS:
        raise ConversationError("INVALID_CONFIG", "Unsupported conversation configuration field", 422)
    for key, item in value.items():
        if key in _BOOL_CONFIG and type(item) is not bool:
            raise ConversationError("INVALID_CONFIG", key + " must be boolean", 422)
        if key in _STR_CONFIG and item is not None and (not isinstance(item, str) or len(item) > 200):
            raise ConversationError("INVALID_CONFIG", key + " must be a model identifier", 422)
        if key in _INT_CONFIG:
            lo, hi = _INT_CONFIG[key]
            if type(item) is not int or not lo <= item <= hi:
                raise ConversationError("INVALID_CONFIG", key + " is out of range", 422)
        if key in _FLOAT_CONFIG:
            lo, hi = _FLOAT_CONFIG[key]
            if type(item) not in (int, float) or not math.isfinite(item) or not lo <= item <= hi:
                raise ConversationError("INVALID_CONFIG", key + " is out of range", 422)
        if key in _ENUM_CONFIG and (not isinstance(item, str) or item not in _ENUM_CONFIG[key]):
            raise ConversationError("INVALID_CONFIG", key + " has an unsupported value", 422)
    _json(value)
    return dict(value)


def _scope(mode, ids):
    if mode not in ("SELECTED", "ALL_ENABLED"):
        raise ConversationError("INVALID_SCOPE", "scope_mode must be SELECTED or ALL_ENABLED", 422)
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in ids):
        raise ConversationError("INVALID_SCOPE", "kb_ids must be a list of nonempty identifiers", 422)
    ids = list(dict.fromkeys(ids))
    if mode == "SELECTED" and not ids:
        raise ConversationError("INVALID_SCOPE", "SELECTED requires at least one explicit knowledge base", 422)
    if mode == "ALL_ENABLED" and ids:
        raise ConversationError("INVALID_SCOPE", "ALL_ENABLED resolves its scope on the server; kb_ids must be empty", 422)
    return mode, ids


def _limit(value, maximum=200):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConversationError("INVALID_PAGINATION", "limit is out of range", 422)
    return value


class ConversationStore:
    def __init__(self, engine, *, source_engine=None):
        self.engine = engine
        self.source_engine = source_engine if source_engine is not None else engine

    @contextmanager
    def _write(self):
        # The database transaction ends before the caller starts any provider.
        with Session(self.engine, expire_on_commit=False) as db:
            try:
                if self.engine.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _require_session(db, session_id):
        row = db.get(ChatSession, session_id)
        if row is None:
            raise ConversationError("SESSION_NOT_FOUND", "Conversation does not exist", 404)
        return row

    @staticmethod
    def _require_turn(db, turn_id):
        row = db.get(ChatTurn, turn_id)
        if row is None:
            raise ConversationError("TURN_NOT_FOUND", "Turn does not exist", 404)
        return row

    @staticmethod
    def _cas(db, row, expected_revision, **changes):
        if type(expected_revision) is not int or row.revision != expected_revision:
            raise ConversationError("REVISION_CONFLICT", "Conversation changed; refresh its revision", 409)
        changes.update(revision=expected_revision + 1, updated_at=datetime.utcnow())
        result = db.execute(update(ChatSession).where(
            ChatSession.id == row.id, ChatSession.revision == expected_revision
        ).values(**changes).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ConversationError("REVISION_CONFLICT", "Conversation changed; refresh its revision", 409)
        db.refresh(row)

    def _recover(self, db, session, now=None):
        if not session.active_turn_id:
            return
        now = now or datetime.utcnow()
        turn = db.get(ChatTurn, session.active_turn_id)
        if turn is not None and turn.status == "RUNNING" and turn.lease_expires_at is not None and turn.lease_expires_at > now:
            return
        if turn is not None and turn.status == "RUNNING":
            turn.status = "UNKNOWN"
            turn.generation_status = "UNKNOWN"
            turn.lease_token = None
            turn.lease_expires_at = None
            turn.updated_at = turn.finished_at = now
            metadata = _load(turn.metadata_json, {})
            metadata["recovery"] = {"reason": "lease_expired", "recovered_at": _iso(now), "automatic_retry": False}
            turn.metadata_json = _json(metadata)
            db.add(turn)
        self._cas(db, session, session.revision, active_turn_id=None)

    @staticmethod
    def _session_dict(row):
        return {"id": row.id, "title": row.title, "scope_mode": row.scope_mode,
                "kb_ids": _load(row.kb_ids_json, []), "config_overrides": _load(row.config_overrides_json, {}),
                "revision": row.revision, "active_turn_id": row.active_turn_id,
                "legacy_origin": _load(row.legacy_origin_json),
                "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
                "archived_at": _iso(row.archived_at)}

    @staticmethod
    def _references(db, turn_id):
        rows = db.exec(select(ChatTurnReference).where(ChatTurnReference.turn_id == turn_id)
                       .order_by(ChatTurnReference.position)).all()
        output = []
        for row in rows:
            item = _load(row.payload_json, {})
            body = db.get(ChatEvidenceBody, row.body_hash) if row.body_hash else None
            item.update({"id": row.id, "citation_index": row.citation_index,
                         "body_hash": row.body_hash, "content_hash": row.body_hash,
                         "kb_id": row.kb_id, "file_id": row.file_id, "chunk_id": row.chunk_id,
                         "build_id": row.build_id, "index_build_id": row.build_id,
                         "file_hash": row.file_hash, "source_sha256": row.file_hash,
                         "source_type": row.source_type, "locator": _load(row.locator_json, {}),
                         "delivery_status": row.delivery_status, "trust_status": row.trust_status,
                         "verification_status": ("SNAPSHOT_ONLY" if row.trust_status == "CAPTURED_SEARCH_RESULT" else row.trust_status)})
            if body is not None:
                item["content"] = body.content
            output.append(item)
        return output

    def _turn_dict(self, db, row):
        return {"id": row.id, "session_id": row.session_id, "sequence_no": row.sequence_no,
                "user_content": row.user_content, "assistant_content": row.assistant_content,
                "client_request_id": row.client_request_id, "status": row.status,
                "generation_status": row.generation_status, "cancel_requested": row.cancel_requested,
                "effective_config": _load(row.effective_config_json, {}),
                "metadata": _load(row.metadata_json, {}), "understanding": _load(row.understanding_json),
                "legacy_metadata": _load(row.legacy_metadata_json), "citations": self._references(db, row.id),
                "created_at": _iso(row.created_at), "started_at": _iso(row.started_at),
                "updated_at": _iso(row.updated_at), "finished_at": _iso(row.finished_at),
                "lease_expires_at": _iso(row.lease_expires_at)}

    def create_session(self, data):
        if not isinstance(data, dict) or set(data) - {"title", "scope_mode", "kb_ids", "config_overrides"}:
            raise ConversationError("INVALID_SESSION", "Unsupported session field", 422)
        title = data.get("title", "新对话")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ConversationError("INVALID_SESSION", "title must contain 1 to 500 characters", 422)
        mode, ids = _scope(data.get("scope_mode", "SELECTED"), data.get("kb_ids", []))
        config = validate_config_overrides(data.get("config_overrides", {}))
        with self._write() as db:
            row = ChatSession(title=title, scope_mode=mode, kb_ids_json=_json(ids), config_overrides_json=_json(config))
            db.add(row)
            db.flush()
            return self._session_dict(row)

    def list_sessions(self, limit=50, offset=0):
        _limit(limit)
        if type(offset) is not int or offset < 0:
            raise ConversationError("INVALID_PAGINATION", "offset must be nonnegative", 422)
        with self._write() as db:
            # Recover only requested rows. No startup sweep or implicit retry.
            rows = db.exec(select(ChatSession).where(ChatSession.archived_at == None)
                           .order_by(ChatSession.updated_at.desc(), ChatSession.id).offset(offset).limit(limit)).all()
            for row in rows:
                self._recover(db, row)
            return [self._session_dict(row) for row in rows]

    def get_session(self, session_id):
        with self._write() as db:
            row = self._require_session(db, session_id)
            self._recover(db, row)
            return self._session_dict(row)

    def patch_session(self, session_id, data, expected_revision):
        if not isinstance(data, dict) or set(data) - {"title", "scope_mode", "kb_ids", "config_overrides"}:
            raise ConversationError("INVALID_SESSION", "Unsupported session field", 422)
        with self._write() as db:
            row = self._require_session(db, session_id)
            if row.archived_at is not None:
                raise ConversationError("SESSION_ARCHIVED", "Conversation is archived", 409)
            if row.active_turn_id is not None:
                raise ConversationError("TURN_ACTIVE", "Finish or cancel the active turn before editing", 409)
            changes = {}
            if "title" in data:
                title = data["title"]
                if not isinstance(title, str) or not title.strip() or len(title) > 500:
                    raise ConversationError("INVALID_SESSION", "title must contain 1 to 500 characters", 422)
                changes["title"] = title
            mode, ids = _scope(data.get("scope_mode", row.scope_mode), data.get("kb_ids", _load(row.kb_ids_json, [])))
            if mode != row.scope_mode or set(ids) != set(_load(row.kb_ids_json, [])):
                has_turn = db.exec(select(ChatTurn.id).where(ChatTurn.session_id == row.id).limit(1)).first()
                if has_turn is not None:
                    raise ConversationError("SCOPE_LOCKED", "Fork a conversation to change scope after its first turn", 409)
            if "scope_mode" in data or "kb_ids" in data:
                changes.update(scope_mode=mode, kb_ids_json=_json(ids))
            if "config_overrides" in data:
                changes["config_overrides_json"] = _json(validate_config_overrides(data["config_overrides"]))
            self._cas(db, row, expected_revision, **changes)
            return self._session_dict(row)

    def archive_session(self, session_id):
        with self._write() as db:
            row = self._require_session(db, session_id)
            if row.active_turn_id:
                raise ConversationError("TURN_ACTIVE", "An active conversation cannot be archived", 409)
            if row.archived_at is None:
                self._cas(db, row, row.revision, archived_at=datetime.utcnow())
            return self._session_dict(row)

    def find_request(self, session_id, client_request_id, question):
        """Replay before model binding or current-scope resolution can fail.

        A replay never starts work, returns a lease token, or changes the frozen
        configuration of the original request.
        """
        if not isinstance(question, str) or not question.strip():
            raise ConversationError("INVALID_QUESTION", "question must be nonempty text", 422)
        if not isinstance(client_request_id, str) or not client_request_id.strip() or len(client_request_id) > 200:
            raise ConversationError("INVALID_REQUEST_ID", "client_request_id is required", 422)
        digest = _hash(_json({"user_content": question}))
        with self._write() as db:
            session = self._require_session(db, session_id)
            turn = db.exec(select(ChatTurn).where(ChatTurn.session_id == session_id,
                                                 ChatTurn.client_request_id == client_request_id)).first()
            if turn is None:
                return None
            if turn.request_hash != digest:
                raise ConversationError("REQUEST_ID_CONFLICT", "client_request_id already identifies different content", 409)
            self._recover(db, session)
            db.refresh(turn)
            return {"turn": self._turn_dict(db, turn), "session": self._session_dict(session),
                    "created": False, "lease_token": None}

    def start_turn(self, session_id, question, client_request_id, expected_revision, effective_config):
        if not isinstance(question, str) or not question.strip():
            raise ConversationError("INVALID_QUESTION", "question must be nonempty text", 422)
        if not isinstance(client_request_id, str) or not client_request_id.strip() or len(client_request_id) > 200:
            raise ConversationError("INVALID_REQUEST_ID", "client_request_id is required", 422)
        if not isinstance(effective_config, dict):
            raise ConversationError("INVALID_CONFIG", "effective_config must be an object", 422)
        _no_credentials(effective_config)
        effective_json = _json(effective_config)
        # Server defaults can change between an original call and HTTP replay.
        # The client sends the question; server-derived config is not its identity.
        request_hash = _hash(_json({"user_content": question}))
        # Recovery must survive a later revision conflict or validation error.
        # No result that arrives after this commit can reacquire the old lease.
        with self._write() as db:
            self._recover(db, self._require_session(db, session_id))
        with self._write() as db:
            row = self._require_session(db, session_id)
            existing = db.exec(select(ChatTurn).where(ChatTurn.session_id == session_id,
                                                     ChatTurn.client_request_id == client_request_id)).first()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ConversationError("REQUEST_ID_CONFLICT", "client_request_id already identifies different content", 409)
                self._recover(db, row)
                db.refresh(existing)
                return {"turn": self._turn_dict(db, existing), "session": self._session_dict(row),
                        "created": False, "lease_token": None}
            self._recover(db, row)
            if row.archived_at is not None:
                raise ConversationError("SESSION_ARCHIVED", "Conversation is archived", 409)
            if row.active_turn_id is not None:
                raise ConversationError("TURN_ACTIVE", "Only one turn may run in a conversation", 409)
            if type(expected_revision) is not int or row.revision != expected_revision:
                raise ConversationError("REVISION_CONFLICT", "Conversation changed; refresh its revision", 409)
            sequence = db.exec(select(func.max(ChatTurn.sequence_no)).where(ChatTurn.session_id == session_id)).one() or 0
            now = datetime.utcnow()
            token = str(uuid4())
            turn = ChatTurn(session_id=session_id, sequence_no=sequence + 1, user_content=question,
                            client_request_id=client_request_id, request_hash=request_hash,
                            effective_config_json=effective_json, lease_token=token,
                            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), started_at=now,
                            created_at=now, updated_at=now)
            db.add(turn)
            db.flush()
            self._cas(db, row, expected_revision, active_turn_id=turn.id)
            return {"turn": self._turn_dict(db, turn), "session": self._session_dict(row),
                    "created": True, "lease_token": token}

    def list_turns(self, session_id, limit=50, before_seq=None):
        _limit(limit)
        if before_seq is not None and (type(before_seq) is not int or before_seq < 1):
            raise ConversationError("INVALID_PAGINATION", "before_seq must be a positive sequence number", 422)
        with self._write() as db:
            session = self._require_session(db, session_id)
            self._recover(db, session)
            statement = select(ChatTurn).where(ChatTurn.session_id == session_id)
            if before_seq is not None:
                statement = statement.where(ChatTurn.sequence_no < before_seq)
            rows = db.exec(statement.order_by(ChatTurn.sequence_no.desc()).limit(limit)).all()
            return [self._turn_dict(db, row) for row in reversed(rows)]

    def get_turn(self, turn_id):
        with self._write() as db:
            row = self._require_turn(db, turn_id)
            self._recover(db, self._require_session(db, row.session_id))
            db.refresh(row)
            return self._turn_dict(db, row)

    @staticmethod
    def _lease_valid(turn, session, token, now):
        return (turn.status == "RUNNING" and session.active_turn_id == turn.id and token is not None
                and turn.lease_token == token and turn.lease_expires_at is not None and turn.lease_expires_at > now)

    def heartbeat_turn(self, turn_id, lease_token):
        valid = False
        with self._write() as db:
            turn = self._require_turn(db, turn_id)
            session = self._require_session(db, turn.session_id)
            now = datetime.utcnow()
            self._recover(db, session, now)
            valid = self._lease_valid(turn, session, lease_token, now)
            if valid:
                turn.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
                turn.updated_at = now
                db.add(turn)
                result = self._turn_dict(db, turn)
        if not valid:
            raise ConversationError("LEASE_LOST", "Turn lease expired or ownership changed", 409)
        return result

    def read_turn_control(self, turn_id, lease_token):
        """Cheap cancellation polling without a write or evidence-body reads."""
        with Session(self.engine) as db:
            control = db.exec(select(
                ChatTurn.status, ChatTurn.cancel_requested, ChatTurn.lease_token,
                ChatTurn.lease_expires_at, ChatSession.active_turn_id
            ).join(ChatSession, ChatSession.id == ChatTurn.session_id).where(ChatTurn.id == turn_id)).first()
            if control is None:
                raise ConversationError("TURN_NOT_FOUND", "Turn does not exist", 404)
            status, cancelled, token, expiry, active = control
            if (status != "RUNNING" or active != turn_id or lease_token is None or token != lease_token
                    or expiry is None or expiry <= datetime.utcnow()):
                raise ConversationError("LEASE_LOST", "Turn lease expired or ownership changed", 409)
            return {"status": status, "cancel_requested": cancelled}

    def cancel_turn(self, turn_id):
        with self._write() as db:
            turn = self._require_turn(db, turn_id)
            session = self._require_session(db, turn.session_id)
            self._recover(db, session)
            if turn.status == "RUNNING":
                turn.cancel_requested = True
                turn.updated_at = datetime.utcnow()
                db.add(turn)
            return self._turn_dict(db, turn)

    def _store_references(self, db, turn_id, citations, legacy=False):
        if not isinstance(citations, list) or any(not isinstance(item, dict) for item in citations):
            raise ConversationError("INVALID_CITATIONS", "citations must be a list of objects", 422)
        for position, original in enumerate(citations):
            item = dict(original)
            source = item.get("source_metadata") if isinstance(item.get("source_metadata"), dict) else {}
            locator = item.get("locator", source.get("locator", {}))
            if not isinstance(locator, dict):
                locator = {"original": locator}
            for key in ("page_number", "start_offset", "end_offset", "start", "end", "location", "url", "image_path",
                        "sheet", "sheet_name", "row", "row_start", "row_end", "col", "column", "column_start",
                        "column_end", "table_bbox", "bbox"):
                if key in item and key not in locator:
                    locator[key] = item[key]
                elif key in source and key not in locator:
                    locator[key] = source[key]
            body = item.get("content")
            body_hash = _hash(body) if isinstance(body, str) else None
            if body_hash and db.get(ChatEvidenceBody, body_hash) is None:
                db.add(ChatEvidenceBody(content_hash=body_hash, content=body))
                db.flush()
            index = item.get("citation_index", item.get("index"))
            if type(index) is not int:
                # A missing emitted index stays unknown. Never renumber history.
                index = None
            delivery = item.get("delivery_status", "UNKNOWN")
            if delivery not in ("DELIVERED", "NOT_DELIVERED", "PARTIAL", "UNKNOWN", "PROVIDER_REQUEST_PREPARED", "CANDIDATE"):
                delivery = "UNKNOWN"
            def identity(key, *aliases):
                for name in (key,) + aliases:
                    value = item.get(name, source.get(name))
                    if isinstance(value, str) and value:
                        return value
                return None
            payload = dict(item)
            if body_hash:
                payload.pop("content", None)
            db.add(ChatTurnReference(
                turn_id=turn_id, position=position, citation_index=index, body_hash=body_hash,
                kb_id=identity("kb_id", "kbId"), file_id=identity("file_id", "fileId"),
                chunk_id=identity("chunk_id", "chunkId"), build_id=identity("build_id", "index_build_id", "active_build_id"),
                file_hash=identity("file_hash", "source_sha256", "file_sha256", "sha256"), source_type=identity("source_type"),
                locator_json=_json(locator), delivery_status="UNKNOWN" if legacy else delivery,
                # Only the server-owned runner can supply new references. Its
                # explicit source validation survives; browser imports cannot.
                trust_status=("CAPTURED_SEARCH_RESULT" if not legacy and item.get("source_type") == "web"
                              and item.get("web_snapshot_status") == "CAPTURED_SEARCH_RESULT"
                              else "VERIFIED" if not legacy and item.get("verification_status") == "VERIFIED" else "UNKNOWN"),
                payload_json=_json(payload)))
        db.flush()

    def finish_turn(self, turn_id, lease_token, answer, status, generation_status, metadata, citations, understanding=None):
        if not isinstance(answer, str) or status not in _TERMINAL or status == "LEGACY_IMPORTED":
            raise ConversationError("INVALID_OUTCOME", "Invalid turn outcome", 422)
        # Keep the actual provider/pipeline outcome (e.g. NOT_RUN, TRUNCATED),
        # rather than mapping different outcomes into a fabricated success.
        if not isinstance(generation_status, str) or not generation_status or len(generation_status) > 100:
            raise ConversationError("INVALID_OUTCOME", "Invalid generation status", 422)
        if not isinstance(metadata, dict):
            raise ConversationError("INVALID_OUTCOME", "metadata must be an object", 422)
        _no_credentials(metadata)
        metadata_json = _json(metadata)
        understanding_json = _json(understanding) if understanding is not None else None
        valid = False
        with self._write() as db:
            turn = self._require_turn(db, turn_id)
            session = self._require_session(db, turn.session_id)
            now = datetime.utcnow()
            self._recover(db, session, now)
            valid = self._lease_valid(turn, session, lease_token, now)
            if valid:
                self._store_references(db, turn.id, citations)
                turn.assistant_content = answer
                turn.status = "CANCELLED" if turn.cancel_requested and status in _SUCCESS else status
                turn.generation_status = generation_status
                turn.metadata_json = metadata_json
                turn.understanding_json = understanding_json
                turn.lease_token = None
                turn.lease_expires_at = None
                turn.updated_at = turn.finished_at = now
                db.add(turn)
                self._cas(db, session, session.revision, active_turn_id=None)
                db.flush()
                result = self._turn_dict(db, turn)
        if not valid:
            raise ConversationError("LEASE_LOST", "Late outcome cannot replace an expired or completed turn", 409)
        return result

    def history_turns(self, session_id, before_sequence, limit):
        if type(limit) is not int or limit < 0:
            raise ConversationError("INVALID_PAGINATION", "limit must be a nonnegative integer", 422)
        if limit != 0:
            _limit(limit)
        if type(before_sequence) is not int or before_sequence < 1:
            raise ConversationError("INVALID_PAGINATION", "before_sequence must be positive", 422)
        with Session(self.engine) as db:
            self._require_session(db, session_id)
            if limit == 0:
                return []
            rows = db.exec(select(ChatTurn).where(
                ChatTurn.session_id == session_id, ChatTurn.sequence_no < before_sequence
            ).order_by(ChatTurn.sequence_no.desc()).limit(limit)).all()
            # Bound by physical user turns first, so failed attempts consume the
            # same history window. The caller projects safe assistant outcomes
            # and revalidates every reference/scope before building context.
            return [self._turn_dict(db, row) for row in reversed(rows)]

    @staticmethod
    def _prepare_import(data):
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            raise ConversationError("INVALID_IMPORT", "Import must contain a sessions array", 422)
        prepared = []
        for index, raw in enumerate(data["sessions"]):
            original_id = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
            try:
                if not isinstance(raw, dict) or not original_id:
                    raise ConversationError("INVALID_IMPORT", "Every imported conversation requires its original id", 422)
                if not isinstance(raw.get("messages", []), list) or any(not isinstance(m, dict) for m in raw.get("messages", [])):
                    raise ConversationError("INVALID_IMPORT", "messages must be a list of objects", 422)
                # Unknown fields survive, but credential-bearing configuration
                # must not become durable. Body strings are left unchanged.
                _no_credentials(raw)
                raw_scope = raw.get('kbIds', raw.get('kb_ids'))
                if (not isinstance(raw_scope, list)
                        or any(not isinstance(k, str) or not k.strip() for k in raw_scope)):
                    raise ConversationError('INVALID_IMPORT_SCOPE',
                        'Legacy knowledge scope is missing or invalid; it cannot be guessed as all knowledge bases', 422)
                digest = _hash(_json(raw))
                pairs, unmatched = [], []
                pending = None
                for message in raw.get("messages", []):
                    role = message.get("role")
                    content = message.get("content")
                    if role == "user" and isinstance(content, str):
                        if pending is not None:
                            unmatched.append(pending)
                        pending = message
                    elif role == "assistant" and isinstance(content, str) and pending is not None:
                        pairs.append((pending, message))
                        pending = None
                    else:
                        unmatched.append(message)
                if pending is not None:
                    unmatched.append(pending)
                prepared.append({"index": index, "raw": raw, "content_hash": digest,
                                 "pairs": pairs, "unmatched": unmatched})
            except ConversationError as exc:
                prepared.append({"index": index, "error": {"index": index, "original_id": original_id,
                                                            "code": exc.code, "message": exc.message}})
        return prepared

    @staticmethod
    def _import_existing(db, original_id, digest):
        return db.exec(select(ChatSession).where(ChatSession.legacy_source_id == original_id,
                                                ChatSession.legacy_content_hash == digest)).first()

    def preview_import(self, data):
        prepared = self._prepare_import(data)
        output, seen = [], set()
        errors = [entry["error"] for entry in prepared if "error" in entry]
        with Session(self.engine) as db:
            for entry in prepared:
                if "error" in entry:
                    error = entry["error"]
                    # Keep one descriptor per original array index. Consumers
                    # must never map a compacted preview onto the wrong export.
                    output.append({"index": entry["index"], "valid": False,
                                   "original_id": error["original_id"], "error": error,
                                   "legacy_origin": {"original_id": error["original_id"], "content_hash": None},
                                   "title": None, "turn_count": 0, "unmatched_message_count": 0,
                                   "already_imported": False, "session_id": None})
                    continue
                raw, digest = entry["raw"], entry["content_hash"]
                key = (raw["id"], digest)
                existing = self._import_existing(db, *key)
                title = raw.get("title")
                if not isinstance(title, str) or not title:
                    title = "导入的对话"
                output.append({"index": entry["index"], "valid": True, "original_id": raw["id"],
                               "legacy_origin": {"original_id": raw["id"], "content_hash": digest},
                               "title": title[:500], "turn_count": len(entry["pairs"]),
                               "unmatched_message_count": len(entry["unmatched"]),
                               "already_imported": existing is not None or key in seen,
                               "session_id": existing.id if existing is not None else None})
                seen.add(key)
        importable = sum(item["valid"] and not item["already_imported"] for item in output)
        existing_count = sum(item["already_imported"] for item in output)
        return {"sessions": output, "errors": errors, "importable_count": importable, "existing_count": existing_count,
                "counts": {"total_sessions": len(output), "importable_sessions": importable,
                           "existing_sessions": existing_count, "invalid_sessions": len(errors),
                           "total_turns": sum(item["turn_count"] for item in output),
                           "unmatched_messages": sum(item["unmatched_message_count"] for item in output)}}

    def import_history(self, data):
        prepared = self._prepare_import(data)
        errors = [entry["error"] for entry in prepared if "error" in entry]
        output, imported, existing_count = [], 0, 0
        with self._write() as db:
            for entry in prepared:
                if "error" in entry:
                    continue
                raw, digest = entry["raw"], entry["content_hash"]
                existing = self._import_existing(db, raw["id"], digest)
                if existing is not None:
                    existing_count += 1
                    output.append(self._session_dict(existing))
                    continue
                raw_ids = raw.get("kbIds", raw.get("kb_ids", []))
                ids = list(dict.fromkeys(item for item in raw_ids if isinstance(item, str) and item.strip())) if isinstance(raw_ids, list) else []
                config = {}
                if isinstance(raw.get("model"), str):
                    config["model_id"] = raw["model"]
                mode = raw.get("mode")
                if isinstance(mode, str) and mode in _ENUM_CONFIG["mode"]:
                    config["mode"] = mode
                title = raw.get("title")
                if not isinstance(title, str) or not title:
                    title = "导入的对话"
                session = ChatSession(title=title[:500], scope_mode="SELECTED" if ids else "ALL_ENABLED",
                                      kb_ids_json=_json(ids), config_overrides_json=_json(config),
                                      legacy_source_id=raw["id"], legacy_content_hash=digest,
                                      legacy_origin_json=_json({"original_id": raw["id"], "content_hash": digest,
                                                                "unmatched_message_count": len(entry["unmatched"]),
                                                                "imported_turn_count": len(entry["pairs"])}),
                                      legacy_metadata_json=_json({"original": raw, "unmatched_messages": entry["unmatched"]}))
                db.add(session)
                db.flush()
                for sequence, (user, assistant) in enumerate(entry["pairs"], 1):
                    turn = ChatTurn(session_id=session.id, sequence_no=sequence, user_content=user["content"],
                                    assistant_content=assistant["content"], client_request_id="legacy:" + str(sequence),
                                    request_hash=_hash(_json({"user_content": user["content"]})),
                                    status="LEGACY_IMPORTED", generation_status="UNKNOWN",
                                    effective_config_json=_json({**config, "kb_ids": ids,
                                                                "scope_mode": session.scope_mode}),
                                    metadata_json=_json({"imported": True, "verified_model_success": False}),
                                    legacy_metadata_json=_json({"user": user, "assistant": assistant}),
                                    finished_at=datetime.utcnow())
                    db.add(turn)
                    db.flush()
                    references = assistant.get("citations", assistant.get("references", []))
                    if isinstance(references, list):
                        self._store_references(db, turn.id, [r for r in references if isinstance(r, dict)], legacy=True)
                imported += 1
                output.append(self._session_dict(session))
        return {"sessions": output, "errors": errors, "imported_count": imported, "existing_count": existing_count,
                "counts": {"total_sessions": len(prepared), "imported_sessions": imported,
                           "existing_sessions": existing_count, "invalid_sessions": len(errors),
                           "total_turns": sum(len(entry["pairs"]) for entry in prepared if "error" not in entry),
                           "unmatched_messages": sum(len(entry["unmatched"]) for entry in prepared if "error" not in entry)}}
