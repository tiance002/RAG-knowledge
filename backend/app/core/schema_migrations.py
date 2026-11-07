"""Additive compatibility migration; never rewrites source, chunks or index data.

Legacy parent IDs are intentionally left untouched. Nullable fields distinguish
unknown history from newly verified lineage. Apply against a maintenance-selected
database only; this module does not import application settings or open a DB.
"""
from sqlalchemy import inspect


ADDITIVE_COLUMNS = {
    "file_documents": {
        "original_name": "VARCHAR", "display_name": "VARCHAR",
        "sha256": "VARCHAR", "supersedes_file_id": "VARCHAR",
    },
    "document_chunks": {"start_offset": "INTEGER", "end_offset": "INTEGER"},
}


def apply_additive_schema_migrations(engine):
    """Idempotently add nullable columns without guessing historical values.

    SQLite is the deployed database. Other dialects must use an explicit reviewed
    migration when an old table is detected, instead of starting with a partial
    or silently incompatible schema.
    """
    existing = inspect(engine)
    missing = {}
    for table, columns in ADDITIVE_COLUMNS.items():
        if existing.has_table(table):
            present = {column["name"] for column in existing.get_columns(table)}
            missing[table] = {name: kind for name, kind in columns.items() if name not in present}
    if engine.dialect.name != "sqlite":
        if any(missing.values()):
            raise RuntimeError("Explicit additive schema migration required for this database dialect")
        return []
    changes = []
    with engine.begin() as connection:
        # Serialize concurrent startup migrations. Re-read schema after acquiring
        # the write reservation; another process may have migrated while waiting.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        for table, columns in ADDITIVE_COLUMNS.items():
            present = {row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')}
            if not present:
                continue
            for name, kind in columns.items():
                if name not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {kind}')
                    changes.append(f"{table}.{name}")
        if connection.exec_driver_sql("PRAGMA table_info(file_documents)").fetchall():
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_file_documents_sha256 ON file_documents (sha256)")
    return changes


migrate_schema = apply_additive_schema_migrations
