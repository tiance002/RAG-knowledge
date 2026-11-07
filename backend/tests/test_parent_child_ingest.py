"""Synthetic character-v1 corpus; no real source/index/provider writes."""
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.services.parent_child import ParentChildConfig, build_parent_child_chunks, remap_and_validate_graph
from app.services.parser import ParsedChunk
from app.models import DocumentChunk, FileDocument, KnowledgeBase, FileStatus, ChunkIndex


def generated():
    return build_parent_child_chunks([ParsedChunk("# A\n\nFirst sentence.\n\nSecond paragraph contains evidence. " * 8, 1, "text"),
                                      ParsedChunk("| product | revenue |", 1, "table")],
                                    ParentChildConfig(120, 10, 35, 5))


def payload(chunks):
    return [{**(c.metadata or {}), "content": c.content, "content_type": c.content_type,
             "page_number": c.page_number, "token_count": c.token_count} for c in chunks]


def test_generated_graph_uses_uuid_content_and_character_windows():
    chunks = generated()
    parents = {c.metadata["id"]: c for c in chunks if (c.metadata or {}).get("chunk_type") == "parent"}
    children = [c for c in chunks if (c.metadata or {}).get("chunk_type") == "child"]
    assert len(parents) > 1 and len(children) > len(parents)
    assert all(UUID(pid) and len(c.content) <= 120 for pid, c in parents.items())
    for child in children:
        assert UUID(child.metadata["id"])
        assert child.content in parents[child.metadata["parent_id"]].content
        assert len(child.content) <= 35
        assert child.metadata.get("start_offset") is None
    assert chunks[-1].content_type == "table"
    mapped = remap_and_validate_graph(payload(chunks), "f")
    mapped_parents = {r["id"] for r in mapped if r.get("chunk_type") == "parent"}
    assert all(r["parent_id"] in mapped_parents for r in mapped if r.get("chunk_type") == "child")
    assert not mapped_parents.intersection(parents)


@pytest.mark.parametrize("overrides", [{"parent_chunk_overlap": 120}, {"child_chunk_overlap": -1}, {"child_chunk_size": 121}])
def test_invalid_character_windows_rejected(overrides):
    config = dict(parent_chunk_size=120, parent_chunk_overlap=0, child_chunk_size=35, child_chunk_overlap=5)
    config.update(overrides)
    with pytest.raises(ValueError): ParentChildConfig(**config).validate()


@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "cross_file", "edited_child"])
def test_graph_rejects_unverifiable_parents(mutation):
    rows = payload(generated())
    child = next(r for r in rows if r.get("chunk_type") == "child")
    parent = next(r for r in rows if r.get("id") == child["parent_id"])
    if mutation == "missing": rows.remove(parent)
    if mutation == "wrong_type": parent["chunk_type"] = "content"
    if mutation == "cross_file": parent["file_id"] = "other"
    if mutation == "edited_child": child["content"] = "not in parent"
    with pytest.raises(ValueError): remap_and_validate_graph(rows, "f")


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    from app.tasks import parse_tasks as module
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "_preflight_embedding", lambda *a: None)
    calls, vectors = [], {"old": "old content"}
    def embed(session, file, model, contents):
        calls.append(contents)
        return [[1., 0.] for _ in contents]
    monkeypatch.setattr(module, "_prepare_embeddings", embed)
    monkeypatch.setattr("app.services.pdf_provenance.index_provenance", lambda *a: {})
    class Store:
        def add_documents(self, kb_id, documents, embeddings):
            assert len(documents) == len(embeddings)
            vectors.update({d["id"]: d["content"] for d in documents})
        def delete_ids(self, kb_id, ids):
            for cid in ids: vectors.pop(cid, None)
    monkeypatch.setattr(module, "VectorStoreService", Store)
    with Session(engine) as s:
        s.add(KnowledgeBase(id="kb", name="fixture", embedding_model="fixture", vlm_model=""))
        s.add(FileDocument(id="f", kb_id="kb", name="f", local_path="synthetic", size=1, status=FileStatus.READY))
        s.add(DocumentChunk(id="old", file_id="f", content="old content"))
        s.commit()
    yield module, engine, vectors, calls, Store
    engine.dispose()


def test_index_persists_real_parents_and_indexes_children_plus_specials(ingest):
    module, engine, vectors, calls, _ = ingest
    module.process_submitted_chunks("f", payload(generated()))
    with Session(engine) as s:
        chunks = s.exec(select(DocumentChunk).where(DocumentChunk.file_id == "f")).all()
        parents = {c.id: c for c in chunks if c.chunk_type == "parent"}
        children = [c for c in chunks if c.chunk_type == "child"]
        assert parents and children
        assert all(c.parent_id in parents and parents[c.parent_id].file_id == c.file_id for c in children)
        indexed = {r.chunk_id for r in s.exec(select(ChunkIndex)).all()}
        assert indexed == set(vectors)
        assert not set(parents).intersection(indexed)
        assert "old" not in vectors
        assert len(calls[0]) == len(chunks) - len(parents)
        assert all(c.start_offset is None and c.end_offset is None for c in chunks)


@pytest.mark.parametrize("stage", ["embedding", "vector", "sql"])
def test_failures_preserve_old_sql_and_vectors(ingest, monkeypatch, stage):
    module, engine, vectors, calls, Store = ingest
    def fail(*a, **kw): raise RuntimeError("synthetic " + stage + " failure")
    if stage == "embedding": monkeypatch.setattr(module, "_prepare_embeddings", fail)
    if stage == "vector": monkeypatch.setattr(Store, "add_documents", fail)
    if stage == "sql":
        original = module.get_bm25_index
        index = original()
        monkeypatch.setattr(index, "index_chunk", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        module.process_submitted_chunks("f", payload(generated()))
    with Session(engine) as s:
        assert [c.id for c in s.exec(select(DocumentChunk)).all()] == ["old"]
    assert vectors == {"old": "old content"}


def test_bm25_generic_call_excludes_parent(ingest):
    from app.services.bm25 import BM25Index
    _, engine, _, _, _ = ingest
    with Session(engine) as s:
        s.add(DocumentChunk(id="parent", file_id="f", content="parent context evidence", chunk_type="parent"))
        s.commit()
        BM25Index().index_chunk(s, "parent", "parent context evidence", "kb")
        s.commit()
        assert not s.exec(select(ChunkIndex).where(ChunkIndex.chunk_id == "parent")).all()


def test_preview_parent_ids_survive_confirmation_relationships(tmp_path, monkeypatch, ingest):
    module, engine, vectors, calls, Store = ingest
    source = tmp_path / "fixture.md"
    source.write_text("# Verified title\n\n" + "Evidence paragraph contains precise facts.\n\n" * 20, encoding="utf-8")
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(module, "_resolve_vlm_binding", lambda *a: None)
    monkeypatch.setattr(Store, "_existing_collection", lambda *a: None, raising=False)
    with Session(engine) as s:
        file = s.get(FileDocument, "f")
        file.local_path = str(source)
        file.status = FileStatus.PENDING
        s.add(file)
        s.delete(s.get(DocumentChunk, "old"))
        s.commit()
    vectors.clear()
    result = module.process_file_parsing("f", chunk_size=500, chunk_overlap=0,
        parent_child=True, parent_chunk_size=180, parent_chunk_overlap=0,
        child_chunk_size=45, child_chunk_overlap=5)
    assert result["status"] == "pending_confirm"
    with Session(engine) as s:
        rows = s.exec(select(DocumentChunk).where(DocumentChunk.file_id == "f")).all()
        parents = {c.id for c in rows if c.chunk_type == "parent"}
        children = [c for c in rows if c.chunk_type == "child"]
        assert parents and children
        assert all(c.parent_id in parents for c in children)
        submitted = [c.model_dump() for c in rows]
    module.process_submitted_chunks("f", submitted)
    with Session(engine) as s:
        rows = s.exec(select(DocumentChunk).where(DocumentChunk.file_id == "f")).all()
        final_parents = {c.id for c in rows if c.chunk_type == "parent"}
        assert final_parents and not final_parents.intersection(parents)
        assert all(c.parent_id in final_parents for c in rows if c.chunk_type == "child")
    assert not final_parents.intersection(vectors)


def test_parser_failure_retains_existing_file_chunks_and_vectors(ingest,monkeypatch):
    from types import SimpleNamespace
    from app.services import model_bindings
    module,engine,vectors,_,_=ingest
    class BrokenParser:
        def __init__(self,**kw): pass
        def parse(self,**kw): raise RuntimeError('synthetic parser failure')
    monkeypatch.setattr(module,'FileParser',BrokenParser)
    monkeypatch.setattr(model_bindings,'resolve_model',lambda *a,**kw:SimpleNamespace(
        context_length=8192,model_name='synthetic',base_url='synthetic',api_key='none'))
    with pytest.raises(RuntimeError,match='synthetic parser failure'):
        module.process_file_parsing('f',chunk_size=500,chunk_overlap=0,auto_vectorize=True,parent_child=True)
    with Session(engine) as s:
        assert s.get(DocumentChunk,'old').content=='old content'
        assert s.get(FileDocument,'f').status==FileStatus.READY
    assert vectors=={'old':'old content'}
