import hashlib
import json

from app.services.source_registry import trusted_source_hash


def test_pinned_source_only_manifest_rejects_tampering_and_ambiguity(tmp_path, monkeypatch):
    source = tmp_path / 'book.xlsx'
    source.write_bytes(b'original')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = tmp_path / 'sources.json'
    payload = {'schema': 'verified-source-files/v1', 'sources': [{'path': str(source), 'sha256': digest}]}
    def freeze():
        manifest.write_text(json.dumps(payload), encoding='utf8')
        monkeypatch.setenv('RAG_SOURCE_MANIFEST_SHA256', hashlib.sha256(manifest.read_bytes()).hexdigest())
    monkeypatch.setenv('RAG_SOURCE_MANIFEST', str(manifest))
    freeze()
    assert trusted_source_hash(source) == digest
    assert trusted_source_hash(tmp_path / 'other.xlsx') is None
    manifest.write_bytes(manifest.read_bytes() + b' ')
    assert trusted_source_hash(source) is None
    payload['sources'].append(payload['sources'][0])
    freeze()
    assert trusted_source_hash(source) is None
    payload['sources'] = payload['sources'][:1]
    payload['question'] = 'must not consume QA manifests'
    freeze()
    assert trusted_source_hash(source) is None
