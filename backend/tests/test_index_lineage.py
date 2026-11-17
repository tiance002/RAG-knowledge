from types import SimpleNamespace
from app.services.index_lineage import file_lineage


def test_lineage_stable_and_changes_with_chunk_or_source(tmp_path):
    path = tmp_path / 'source'; path.write_bytes(b'source')
    a = SimpleNamespace(id='a', original_index=0, content='one')
    b = SimpleNamespace(id='b', original_index=1, content='two')
    result = file_lineage('file', path, [a, b])
    assert result == file_lineage('file', path, [b, a])
    b.content = 'changed'
    assert result['index_version'] != file_lineage('file', path, [a, b])['index_version']
    path.write_bytes(b'other')
    assert result['source_sha256'] != file_lineage('file', path, [a, b])['source_sha256']
