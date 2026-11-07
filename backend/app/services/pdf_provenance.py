"""Immutable, source-hashed PDF manifests without a database schema migration."""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
import tempfile


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def chunk_key(content, page_number, content_type):
    return digest_bytes(json.dumps([content, page_number, content_type], ensure_ascii=False).encode())


def write_manifest(root, source_sha256, pages, chunks, *, schema="pdf-parse-v1"):
    payload = {"schema": schema, "source_sha256": source_sha256,
               "pages": pages, "chunks": [
                   {"key": chunk_key(c.content, c.page_number, c.content_type),
                    "metadata": c.metadata or {}} for c in chunks]}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    directory = Path(root) / source_sha256
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest_bytes(data) + '.json')
    # Exclusive creation preserves historical manifests. Readers only see complete files.
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        handle.write(data)
        temporary = handle.name
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError('PDF_MANIFEST_IDENTITY_CONFLICT')
    finally:
        os.unlink(temporary)
    return str(path)


@lru_cache(maxsize=128)
def _source_hash(path, size, mtime_ns):
    return digest_bytes(Path(path).read_bytes())


@lru_cache(maxsize=64)
def _manifest_index(path, size, mtime_ns):
    manifest = Path(path)
    data = manifest.read_bytes()
    if digest_bytes(data) != manifest.stem:
        raise ValueError('PDF_MANIFEST_HASH_MISMATCH')
    value = json.loads(data)
    index = {}
    for row in value['chunks']:
        index.setdefault(row['key'], []).append(row['metadata'])
    return value['source_sha256'], index


def source_metadata(path, content, page_number, content_type, *, root=None):
    """Only exact persisted text can recover geometry; edited previews cannot forge it."""
    from app.core.config import get_settings
    root = root or get_settings().PDF_AUDIT_DIR
    source = Path(path)
    if source.suffix.lower() not in {'.pdf', '.xlsx', '.csv'} or not source.is_file():
        return {}
    stat = source.stat()
    sha = _source_hash(str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    key = chunk_key(content, page_number, getattr(content_type, 'value', content_type))
    matches = []
    for manifest in sorted((Path(root) / sha).glob('*.json')):
        stat = manifest.stat()
        manifest_sha, index = _manifest_index(str(manifest), stat.st_size, stat.st_mtime_ns)
        if manifest_sha != sha:
            raise ValueError('PDF_SOURCE_IDENTITY_CONFLICT')
        matches.extend(index.get(key, ()))
    if not matches:
        return {}
    # Repeated identical lines on one page may have several real locations.
    locators = []
    for metadata in matches:
        locator = metadata.get('source_locator')
        if locator and locator not in locators:
            locators.append(locator)
    result = {"source_sha256": sha, "source_locators": locators}
    origins = sorted({m['content_origin'] for m in matches if m.get('content_origin')})
    if origins:
        result['content_origins'] = origins
    descriptions = [m['image_description'] for m in matches if m.get('image_description')]
    if descriptions:
        result['image_descriptions'] = descriptions
    if source.suffix.lower() in {'.xlsx', '.csv'} and len(locators) == 1:
        result.update(locators[0])
    return result


def index_provenance(path, chunk):
    metadata = source_metadata(path, chunk.content, chunk.page_number, chunk.content_type)
    return {"source_locator_json": json.dumps(metadata, ensure_ascii=False)} if metadata else {}
