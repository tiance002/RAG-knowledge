import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from app.services.agent_tool_results import AgentToolResultStore, ToolResultStoreError
from app.tools.read_tool_result import read_tool_result


def sources():
    return [{'source_id': 1, 'chunk_id': 'internal-uuid-1', 'fileId': 'private-file',
             'fileName': 'C:\\private\\合同.txt', 'content': '原文😀\n金额42元。',
             'url': 'https://example.test/doc', 'page_number': 7,
             'source_metadata': {'page': 7, 'source_path': '/secret/original.pdf'}},
            {'source_id': 2, 'chunk_id': 'internal-uuid-2', 'fileName': 'second.txt',
             'content': 'Second original body.'}]


def payload(rows):
    return json.dumps({'context': '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}" for c in rows),
                       'source_ids': [c['source_id'] for c in rows], 'citations': rows,
                       'retrieval_status': 'SUCCESS', 'attempt_id': 'private-attempt'},
                      ensure_ascii=False, indent=2)


def test_exact_utf8_content_hash_dedup_private_index_and_original_read(tmp_path):
    rows = sources()
    raw = payload(rows)
    with AgentToolResultStore(rows, root=tmp_path) as store:
        index = store.save(raw)
        handle = index['result_handle']
        assert store.resolve(handle) == raw
        assert store._entries[handle]['path'].read_bytes() == raw.encode('utf-8')
        assert index['sha256'] == hashlib.sha256(raw.encode('utf-8')).hexdigest()
        assert store.save(raw) == index
        assert len(store._entries) == 1
        wire = json.dumps(index, ensure_ascii=False)
        for secret in ('internal-uuid', 'private-file', 'private-attempt', 'private\\', 'secret/original'):
            assert secret not in wire
        assert index['sources'][0]['title'] == '合同.txt'
        assert index['sources'][0]['source_metadata']['page'] == 7
        assert index['sources'][0]['url'] == 'https://example.test/doc'
        assert index['delivered_source_ids'] == []
        assert 'INCOMPLETE' in index['sources'][0]['preview_status']
        read = store.read(handle, ['s2'])
        assert read['citations'] == [rows[1]]
        assert read['delivered_source_ids'] == [2]
        assert read['tool_result_read'] is True
        assert read['result_handle'] == handle
        assert store.metadata(handle)['attempt_id'] == 'private-attempt'
        if os.name != 'nt':
            assert stat.S_IMODE(store._directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(store._entries[handle]['path'].stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == []


def test_populated_request_stores_do_not_accept_each_others_handles(tmp_path):
    rows = sources()
    with AgentToolResultStore(rows, root=tmp_path) as first, AgentToolResultStore(rows, root=tmp_path) as second:
        one = first.save(payload(rows))['result_handle']
        two = second.save(payload(rows))['result_handle']
        assert one != two
        for store, foreign in ((first, two), (second, one)):
            with pytest.raises(ToolResultStoreError):
                store.read(foreign)
        for handle in ('../r1.json', str(first._directory), '/etc/passwd', 'r0', True, None):
            with pytest.raises(ToolResultStoreError):
                first.resolve(handle)


def test_hash_corruption_and_unregistered_source_rejected(tmp_path):
    rows = sources()
    with AgentToolResultStore(rows, root=tmp_path) as store:
        handle = store.save(payload(rows))['result_handle']
        path = store._entries[handle]['path']
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'42', b'99'))
        with pytest.raises(ToolResultStoreError, match='hash mismatch'):
            store.read(handle)
        with pytest.raises(ToolResultStoreError):
            store.save(payload(rows))
    with AgentToolResultStore(rows, root=tmp_path) as store:
        altered = [dict(rows[0], content='invented')]
        with pytest.raises(ToolResultStoreError, match='not registered'):
            store.save(payload(altered))
        with pytest.raises(ToolResultStoreError):
            store.save('{invalid')


def test_source_selection_and_paging_use_only_saved_result(tmp_path):
    rows = sources()
    with AgentToolResultStore(rows, root=tmp_path) as store:
        handle = store.save(payload(rows))['result_handle']
        read = store.read(handle, offset=0, limit=1)
        assert read['source_ids'] == [1] and read['next_offset'] == 1
        assert store.read(handle, offset=1, limit=1)['source_ids'] == [2]
        assert store.read(handle, offset=2)['citations'] == []
        smaller = store.save(payload(rows[:1]))['result_handle']
        for selection in (['s2'], ['s999'], ['internal-uuid-1'], [True], [], 's1'):
            with pytest.raises(ToolResultStoreError):
                store.read(smaller, selection)
        for kwargs in ({'offset': -1}, {'limit': 0}, {'limit': 21}, {'max_chars': 16001}, {'offset': True}):
            with pytest.raises(ToolResultStoreError):
                store.read(handle, **kwargs)


def test_whole_body_limit_never_claims_truncated_evidence(tmp_path):
    rows = sources()
    rows[0]['content'] = '完整原文' * 5000
    with AgentToolResultStore(rows, root=tmp_path) as store:
        handle = store.save(payload(rows))['result_handle']
        read = store.read(handle)
        assert read['source_ids'] == [2]
        assert read['citations'] == [rows[1]]
        assert read['read_status'] == 'CANNOT_FIT_WHOLE_SOURCE'
        assert read['cannot_fit'][0]['source_id'] == 's1'
        assert read['cannot_fit'][0]['body_chars'] == len(rows[0]['content'])
        assert rows[0]['content'] not in read['context']


def test_disk_entry_limits_cleanup_exception_and_no_foreign_deletion(tmp_path):
    rows = sources()
    raw = payload(rows)
    with AgentToolResultStore(rows, root=tmp_path, max_bytes=len(raw.encode('utf-8')) - 1) as store:
        with pytest.raises(ToolResultStoreError, match='disk budget'):
            store.save(raw)
        assert list(store._directory.iterdir()) == []
    with AgentToolResultStore(rows, root=tmp_path, max_entries=1) as store:
        store.save(raw)
        with pytest.raises(ToolResultStoreError, match='exhausted'):
            store.save(payload(rows[:1]))
    store = AgentToolResultStore(rows, root=tmp_path)
    directory = store._directory
    with pytest.raises(RuntimeError):
        with store:
            store.save(raw)
            raise RuntimeError('cancelled')
    assert not directory.exists()
    with pytest.raises(ToolResultStoreError):
        store.save(raw)
    store.close()
    foreign = tmp_path / 'foreign.json'
    foreign.write_text('belongs to somebody else', encoding='utf-8')
    with AgentToolResultStore(rows, root=tmp_path) as store:
        store.save(raw)
    assert foreign.read_text(encoding='utf-8') == 'belongs to somebody else'


def test_refresh_copied_registry_and_schema_requires_server_dispatch(tmp_path):
    rows = sources()
    with AgentToolResultStore([], root=tmp_path) as store:
        store.update_registry(rows)
        handle = store.save(payload(rows))['result_handle']
        assert store.read(handle, [1])['citations'] == [rows[0]]
    schema = read_tool_result.args_schema.model_json_schema()
    assert set(schema['properties']) == {'result_handle', 'source_ids', 'offset', 'limit'}
    with pytest.raises(ValueError, match='server-side'):
        asyncio.run(read_tool_result.ainvoke({'result_handle': 'r1'}))


def test_aggregate_byte_quota_and_cleanup_failure_preserve_business_error(tmp_path, monkeypatch):
    rows = sources()
    raw = payload(rows)
    with AgentToolResultStore(rows, root=tmp_path, max_bytes=len(raw.encode('utf-8'))) as store:
        store.save(raw)
        with pytest.raises(ToolResultStoreError, match='exhausted'):
            store.save(payload(rows[:1]))
    store = AgentToolResultStore(rows, root=tmp_path)
    store.save(raw)
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.parent == store._directory:
            raise PermissionError('file temporarily open')
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', denied)
        with pytest.raises(RuntimeError, match='original cancellation'):
            with store:
                raise RuntimeError('original cancellation')
        assert store.close()['cleanup_status'] == 'INCOMPLETE'
    assert store.close()['cleanup_status'] == 'COMPLETE'
    assert not store._directory.exists()


def test_large_index_has_bounded_first_page_and_later_sources_remain_readable(tmp_path):
    rows = [{'source_id': number, 'chunk_id': f'internal-{number}',
             'fileName': 'x' * 5000, 'content': f'原文 source {number} ' + 'z' * 300,
             'source_metadata': {'page': number, 'large_locator': 'a' * 20000}}
            for number in range(1, 51)]
    with AgentToolResultStore(rows, root=tmp_path) as store:
        raw = payload(rows)
        index = store.save(raw)
        assert len(index['sources']) == 10
        assert index['total_sources'] == index['available_source_count'] == 50
        assert index['next_source_offset'] == 10
        assert all(card['locator_metadata_omitted'] for card in index['sources'])
        assert all(len(json.dumps(card, ensure_ascii=False)) <= 1100 for card in index['sources'])
        assert len(json.dumps(index, ensure_ascii=False)) < 12000
        assert store.resolve(index['result_handle']) == raw
        # Large metadata is preserved on disk; use a later ordinary source to
        # establish source paging independent of the initial ten aliases.
        rows[-1]['source_metadata'] = {'page': 50}
        handle = store.save(payload(rows))['result_handle']
        read = store.read(handle, offset=49, limit=1)
        assert read['source_ids'] == [50]
        assert read['citations'][0] == rows[-1]
        assert read['next_offset'] is None
