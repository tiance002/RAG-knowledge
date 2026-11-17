"""Observational fingerprints for persisted file chunks; not a database index version."""
import hashlib
import json


def file_lineage(file_id, source_path, chunks):
    source_sha256 = None
    try:
        with open(source_path, 'rb') as source:
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
            source_sha256 = digest.hexdigest()
    except (OSError, TypeError):
        pass
    ordered = sorted(chunks, key=lambda c: (c.original_index if c.original_index is not None else -1, str(c.id)))
    payload = [[str(c.id), hashlib.sha256((c.content or '').encode('utf-8')).hexdigest()] for c in ordered]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'file_id': str(file_id), 'source_sha256': source_sha256,
            'index_version': hashlib.sha256(encoded).hexdigest(), 'chunk_count': len(payload)}
