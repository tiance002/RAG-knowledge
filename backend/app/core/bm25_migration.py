"""Populate the term lookup once, in the same transaction as table creation."""
import json

from sqlalchemy import select


def begin_posting_creation(table, connection, **kwargs):
    # Python sqlite3 in legacy mode does not start transactions for DDL.
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def backfill_new_posting_table(table, connection, **kwargs):
    chunks = table.metadata.tables["chunk_index"]
    batch = []
    with connection.execute(select(chunks.c.id, chunks.c.kb_id, chunks.c.terms)) as rows:
        for index_id, kb_id, encoded in rows:
            try:
                terms = json.loads(encoded)
                if not isinstance(terms, dict):
                    raise ValueError("expected a term-frequency dictionary")
                for term, tf in terms.items():
                    if type(tf) is not int or tf < 1:
                        raise ValueError("invalid term frequency")
                    batch.append(dict(index_id=index_id, kb_id=kb_id, term=term, tf=tf))
                    if len(batch) == 4096:
                        connection.execute(table.insert(), batch)
                        batch.clear()
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid BM25 frequencies in chunk_index row {index_id}") from exc
    if batch:
        connection.execute(table.insert(), batch)
