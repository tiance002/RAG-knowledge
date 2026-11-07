"""Real-byte offline shape/seed/citation check, not retrieval or answer-quality eval.

Inputs remain read-only. All SQL writes use an in-memory database; no provider is
called. Query-term matching selects deterministic seeds solely for inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def evaluate(root, temporary):
    os.environ.update(DATABASE_URL="sqlite:///:memory:", RAG_OBSERVABILITY_ENABLED="false",
                      UPLOAD_DIR=str(temporary / "uploads"), IMAGE_DIR=str(temporary / "images"),
                      CHROMA_DB_DIR=str(temporary / "chroma"))
    from sqlmodel import SQLModel, Session, create_engine
    from app.models import DocumentChunk, FileDocument, KnowledgeBase
    from app.services.parser import ParsedChunk, TextSplitter
    from app.services.parent_child import ParentChildConfig, build_parent_child_chunks
    from app.services.parent_context import expand_parent_context
    from app.services.context_builder import build_chat_context

    path = root / "README.md"
    source = path.read_bytes()
    text = source.decode("utf-8")
    splitter = TextSplitter(chunk_size=500, chunk_overlap=50)
    started = perf_counter()
    before = splitter.split(text)
    before_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    graph = build_parent_child_chunks([ParsedChunk(text, 1, "text")], ParentChildConfig())
    split_ms = (perf_counter() - started) * 1000
    parents = [piece for piece in graph if piece.metadata["chunk_type"] == "parent"]
    children = [piece for piece in graph if piece.metadata["chunk_type"] == "child"]
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    comparisons = []
    with Session(engine) as session:
        session.add(KnowledgeBase(id="offline-kb", name="offline", embedding_model="unused", vlm_model="unused"))
        session.add(FileDocument(id="offline-file", kb_id="offline-kb", name="README.md", local_path=str(path), size=len(source)))
        for i, piece in enumerate(graph):
            session.add(DocumentChunk(id=piece.metadata["id"], file_id="offline-file", content=piece.content,
                                      chunk_type=piece.metadata["chunk_type"], parent_id=piece.metadata.get("parent_id"), original_index=i))
        session.commit()
        for term in ["父子 Chunk", "BM25", "Celery", "模型配置"]:
            ordinary = sorted(enumerate(before), key=lambda pair: (-pair[1].count(term), pair[0]))
            ordinary = [(index, content) for index, content in ordinary if term in content][:2]
            selected = sorted(enumerate(children), key=lambda pair: (-pair[1].content.count(term), pair[0]))
            selected = [piece for _, piece in selected if term in piece.content][:2]
            rows = [{"id": piece.metadata["id"], "file_id": "offline-file", "kb_id": "offline-kb", "file_name": "README.md",
                     "content": piece.content, "score": 1.0-index*.1} for index, piece in enumerate(selected)]
            started = perf_counter()
            expanded = expand_parent_context(rows, session, window_size=0, max_parent_length=4096,
                                              max_context_chars=24000, allowed_kb_ids=["offline-kb"], include_source_members=True)
            citations, summary = build_chat_context(expanded, lambda row, body: {"id": row["id"], "fileId": row["file_id"]})
            expand_ms = (perf_counter()-started)*1000
            delivered = "\n\n".join(citation["content"] for citation in citations)
            raw_expanded = "\n\n".join(row["content"] for row in expanded)
            raw_retained = sum(piece.content in raw_expanded for piece in selected)
            retained = sum(piece.content.strip() in delivered for piece in selected)
            exact_citation_retained = sum(piece.content in delivered for piece in selected)
            assert raw_retained == len(selected), "selected seed bytes omitted from expansion"
            assert retained == len(selected), "selected seed text omitted from final citations"
            assert len({citation["id"] for citation in citations}) == len(citations), "duplicate final citation IDs"
            comparisons.append({"query_term": term, "selection": "substring_count_desc_then_original_order_top2",
                                "before_ordinary_seed_count": len(ordinary), "before_seed_only_chars": sum(len(content) for _, content in ordinary),
                                "after_child_seed_count": len(selected), "after_unique_parent_count": len(expanded),
                                "after_citation_count": len(citations), "after_context_chars": len(delivered),
                                "retained_seed_count": retained, "seed_coverage_denominator": len(selected),
                                "raw_expansion_exact_seed_count": raw_retained,
                                "citation_exact_seed_count": exact_citation_retained,
                                "citation_coverage_normalization": "strip boundary whitespace, matching existing evidence_text contract",
                                "extra_context_chars_over_seed_char_sum": len(delivered)-sum(len(piece.content) for piece in selected),
                                "expand_and_citation_ms": expand_ms, "mapping_status": summary.get("source_mapping_status"),
                                "degradation_reasons": [row["context_group"].get("degradation_reason") for row in expanded]})
    engine.dispose()
    # Reuse the active XLSX's already parsed table blocks without reparsing or
    # writing a new spreadsheet manifest; this tests standalone table semantics.
    database = root / "local.db"
    with sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        file = connection.execute("SELECT * FROM file_documents WHERE id='file-excel'").fetchone()
        table_rows = connection.execute("SELECT content,page_number FROM document_chunks WHERE file_id='file-excel' AND content_type IN ('TABLE','table') ORDER BY original_index").fetchall()
    table_result = {"status": "NOT_AVAILABLE"}
    if file:
        table_source = Path(file["local_path"])
        pieces = [ParsedChunk(row["content"], row["page_number"], "table") for row in table_rows]
        converted = build_parent_child_chunks(pieces, ParentChildConfig())
        assert [piece.content for piece in converted] == [piece.content for piece in pieces]
        table_result = {"file_id": file["id"], "source_sha256": hashlib.sha256(table_source.read_bytes()).hexdigest() if table_source.is_file() else None,
                        "table_blocks_before": len(pieces), "table_blocks_after": len(converted),
                        "exact_contents_preserved": True, "status": "CHECKED_EXISTING_SQL_TABLE_BLOCKS"}
    return {"schema": "parent-child-offline-comparison-v1", "source": str(path),
            "source_sha256": hashlib.sha256(source).hexdigest(), "source_chars": len(text),
            "baseline": {"method": "existing TextSplitter, 500 tokens / 50 overlap when tokenizer available; otherwise existing character fallback",
                         "chunks": len(before), "split_ms": before_ms, "context": "selected ordinary seeds only"},
            "candidate": {"config": ParentChildConfig().__dict__, "unit": "characters", "parents": len(parents),
                          "children": len(children), "split_ms": split_ms},
            "queries": comparisons, "active_xlsx_standalone": table_result,
            "limits": ["No actual vector/BM25 retrieval, reranker, provider call or answer generated",
                       "README is real project documentation outside the active knowledge base",
                       "No Gold/qrel correctness labels; recall, answer quality and semantic noise not evaluated",
                       "Extra context characters describe volume, not measured irrelevant noise",
                       "Timings are one local observation, not a latency benchmark"]}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="parent-child-offline-") as temporary:
        result = evaluate(args.root.resolve(), Path(temporary))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
