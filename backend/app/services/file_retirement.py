"""Durable deletion of owned source files and permanently removed KB collections."""
import json
from pathlib import Path
import stat

from sqlmodel import Field, Session, SQLModel, select

from app.core.database import engine
from app.models import FileDocument, KnowledgeBase


class FileStorageRetirement(SQLModel, table=True):
    __tablename__ = "file_storage_retirements"
    id: str = Field(primary_key=True)
    kind: str
    target: str
    identity: str | None = None
    error: str | None = None


def _identity(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("SOURCE_NOT_REGULAR")
    return json.dumps([s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns])


def retire_source(session, doc):
    path = Path(doc.local_path).absolute()
    retirement = FileStorageRetirement(id="source:" + doc.id, kind="source", target=str(path))
    try:
        retirement.identity = _identity(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        retirement.error = type(exc).__name__
    session.add(retirement)
    return retirement.id


def retire_collection(session, kb_id):
    session.add(FileStorageRetirement(id="collection:" + kb_id, kind="collection", target=kb_id))


def clean_source(bind, retirement_id):
    if retirement_id is None:
        return
    with Session(bind) as session:
        if bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        record = session.get(FileStorageRetirement, retirement_id)
        if record is None or record.kind != "source":
            return
        path = Path(record.target)
        try:
            if not record.identity or _identity(path) != record.identity:
                record.error = "SOURCE_IDENTITY_UNVERIFIED"
            elif any(Path(p).absolute() == path for p in session.exec(select(FileDocument.local_path))):
                # The last referencing document's deletion will register its own cleanup.
                session.delete(record)
            else:
                path.unlink()
                session.delete(record)
        except FileNotFoundError:
            session.delete(record)
        except (OSError, ValueError) as exc:
            record.error = type(exc).__name__
        session.commit()


def sweep_storage_retirements():
    from app.services.vector_store import VectorStoreService
    with Session(engine) as session:
        sources = session.exec(select(FileStorageRetirement.id).where(
            FileStorageRetirement.kind == "source")).all()
        collections = session.exec(select(FileStorageRetirement.id).where(
            FileStorageRetirement.kind == "collection")).all()
    for retirement_id in sources:
        clean_source(engine, retirement_id)
    for retirement_id in collections:
        with Session(engine) as session:
            record = session.get(FileStorageRetirement, retirement_id)
            if not record or session.get(KnowledgeBase, record.target) is not None:
                continue
            try:
                store = VectorStoreService()
                if store._existing_collection(record.target) is not None:
                    store.client.delete_collection(name=store._get_collection_name(record.target))
                session.delete(record)
            except Exception as exc:
                record.error = type(exc).__name__
            session.commit()
