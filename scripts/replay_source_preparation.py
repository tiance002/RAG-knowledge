"""Fail-closed source preparation for isolated, offline replay databases.

Ownership must be supplied from a verified source manifest (or an explicitly
declared synthetic fixture), never reconstructed from retrieval candidates.
"""
import hashlib

from sqlmodel import Session, SQLModel, select

from app.models import DocumentChunk, FileDocument, KnowledgeBase


def _unique(rows, label):
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError(f'{label}: explicit nonempty id required')
        if row['id'] in result:
            raise ValueError(f'{label}: duplicate id {row["id"]}')
        result[row['id']] = row
    return result


def prepare_replay_sql(engine, *, chunks, files, allowed_kb_ids, ownership_provenance):
    """Populate only a new in-memory DB using explicit file-to-KB assignments."""
    if engine.url.get_backend_name() != 'sqlite' or engine.url.database not in (None, '', ':memory:'):
        raise ValueError('replay preparation requires an in-memory SQLite database')
    if not isinstance(ownership_provenance, str) or not ownership_provenance.strip():
        raise ValueError('verified ownership provenance required')
    scope = set(allowed_kb_ids or [])
    if not scope or any(not isinstance(k, str) or not k for k in scope):
        raise ValueError('explicit allowed_kb_ids required')
    file_rows = _unique(files, 'files')
    chunk_rows = _unique(chunks, 'chunks')
    for row in file_rows.values():
        if not isinstance(row.get('kb_id'), str) or not row['kb_id']:
            raise ValueError(f'explicit file KB ownership missing: {row["id"]}')
        if not isinstance(row.get('name'), str) or not row['name']:
            raise ValueError(f'explicit file name required: {row["id"]}')
    for row in chunk_rows.values():
        if row.get('file_id') not in file_rows:
            raise ValueError(f'chunk has no verified FileDocument ownership: {row["id"]}; file_id={row.get("file_id")}')
        if not isinstance(row.get('content'), str):
            raise ValueError(f'chunk content missing: {row["id"]}')
        if row.get('content_sha256') and hashlib.sha256(row['content'].encode('utf8')).hexdigest() != row['content_sha256']:
            raise ValueError(f'chunk hash mismatch: {row["id"]}')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if any(session.exec(select(model)).first() is not None for model in (KnowledgeBase, FileDocument, DocumentChunk)):
            raise ValueError('replay preparation requires an empty database')
        # These names/model labels describe this offline DB, not a historical model.
        catalog_kbs = scope | {row['kb_id'] for row in file_rows.values()}
        session.add_all([KnowledgeBase(id=k, name=k, embedding_model='NOT_RUN', vlm_model='NOT_RUN') for k in sorted(catalog_kbs)])
        session.flush()
        session.add_all([FileDocument(**{k: v for k, v in row.items() if k in FileDocument.model_fields},
                                     **({} if 'local_path' in row else {'local_path': 'UNKNOWN'}),
                                     **({} if 'size' in row else {'size': 0})) for row in file_rows.values()])
        session.flush()
        session.add_all([DocumentChunk(**{k: v for k, v in row.items() if k in DocumentChunk.model_fields}) for row in chunk_rows.values()])
        session.commit()
    return {'files': len(file_rows), 'chunks': len(chunk_rows), 'allowed_kb_ids': sorted(scope), 'catalog_kb_ids': sorted(catalog_kbs),
            'ownership_provenance': ownership_provenance}


def expand_replay_context(session, seeds, *, allowed_kb_ids, expected_seed_count, clean_zero=False, **expansion_kwargs):
    """Run actual expansion and reject missing expected hits before either arm."""
    if type(expected_seed_count) is not int or expected_seed_count < 0:
        raise ValueError('explicit nonnegative expected_seed_count required')
    if len(seeds) != expected_seed_count:
        raise ValueError('expected seed count mismatch')
    scope = set(allowed_kb_ids or [])
    if not scope:
        raise ValueError('explicit allowed_kb_ids required')
    if not seeds:
        if clean_zero is not True:
            raise ValueError('empty retrieval requires an explicit clean_zero declaration')
        return []
    if clean_zero:
        raise ValueError('clean_zero cannot be declared for nonempty retrieval')
    ids = set()
    verified_seeds = []
    for seed in seeds:
        chunk = session.get(DocumentChunk, seed.get('id'))
        owner = session.get(FileDocument, chunk.file_id) if chunk else None
        kb = session.get(KnowledgeBase, owner.kb_id) if owner else None
        if (chunk is None or owner is None or kb is None or owner.kb_id not in scope
                or seed.get('file_id') != chunk.file_id or seed.get('kb_id') != owner.kb_id):
            raise ValueError(f'expected seed lacks verified source ownership: {seed.get("id")}; file_id={chunk.file_id if chunk else seed.get("file_id")}')
        if seed.get('content') != chunk.content:
            raise ValueError(f'expected seed content differs from SQL: {seed.get("id")}')
        if chunk.id in ids:
            raise ValueError(f'duplicate expected seed: {chunk.id}')
        ids.add(chunk.id)
        # Source format controls locator validity; use the verified document
        # name, rather than an absent or untrusted candidate display label.
        verified_seeds.append({**seed, 'file_name': owner.name, 'kb_name': kb.name})
    from app.services import rerank
    expansion_kwargs.pop('include_source_members', None)
    expanded = rerank.expand_context(verified_seeds, session, allowed_kb_ids=sorted(scope),
                                     include_source_members=True, **expansion_kwargs)
    actual = {row['id'] for row in expanded}
    members = {member['id'] for row in expanded for member in row.get('context_group', {}).get('source_members', [])}
    if not expanded or not ids <= actual or not ids <= members:
        raise ValueError('actual expansion lost expected seed hits')
    for row in expanded:
        for member in row.get('context_group', {}).get('source_members', []):
            chunk = session.get(DocumentChunk, member['id'])
            owner = session.get(FileDocument, chunk.file_id) if chunk else None
            if (owner is None or owner.kb_id not in scope or member.get('file_id') != owner.id
                    or member.get('kb_id') != owner.kb_id or chunk.content != member.get('content')):
                raise ValueError(f'actual expansion returned unverified or out-of-scope member: {member["id"]}')
    return expanded
