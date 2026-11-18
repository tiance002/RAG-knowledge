"""Real file-backed conversation persistence and evidence validation; no providers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from threading import Barrier

import pytest
from sqlalchemy import event, inspect
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.models.conversations import ChatEvidenceBody, ChatSession, ChatTurn, ChatTurnReference
from app.services.conversation_context import prepare_history
from app.services.conversation_store import ConversationError, ConversationStore


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@pytest.fixture
def store(tmp_path):
    database_path = tmp_path / 'conversation.sqlite'
    engine = create_engine('sqlite:///' + database_path.as_posix(),
                           connect_args={'check_same_thread': False, 'timeout': 10})
    @event.listens_for(engine, 'connect')
    def sqlite_setup(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
    tables = [row.__table__ for row in (KnowledgeBase, FileDocument, DocumentChunk,
              FileIndexBuild, ChatSession, ChatTurn, ChatEvidenceBody, ChatTurnReference)]
    SQLModel.metadata.create_all(engine, tables=tables)
    try:
        yield ConversationStore(engine)
    finally:
        engine.dispose()
        # Only the three exact files created by this fixture, never shared data.
        for suffix in ('', '-wal', '-shm'):
            database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)


def new_session(store, **overrides):
    return store.create_session({'title': 'test', 'kb_ids': ['kb'], **overrides})


def start(store, session_id, request_id='request', question='question'):
    session = store.get_session(session_id)
    return store.start_turn(session_id, question, request_id, session['revision'], {'kb_ids': ['kb']})


def finish(store, receipt, answer='answer [7]', status='COMPLETED', generation='COMPLETED', refs=None):
    return store.finish_turn(receipt['turn']['id'], receipt['lease_token'], answer, status,
                             generation, {'test': True}, refs or [])


def expect_error(code, callable_, *args, **kwargs):
    with pytest.raises(ConversationError) as captured:
        callable_(*args, **kwargs)
    assert captured.value.code == code
    return captured.value


def seed_source(store):
    manifest = json.dumps({'chunk_ids': ['chunk']})
    with Session(store.engine) as db:
        db.add(KnowledgeBase(id='kb', name='Knowledge', embedding_model='test', vlm_model=''))
        db.flush()
        db.add(FileDocument(id='file', kb_id='kb', name='source.pdf', local_path='unused.pdf',
                            size=10, sha256='source-hash', active_build_id='build'))
        db.flush()
        db.add(DocumentChunk(id='chunk', file_id='file', content='real source\nexact body',
                             page_number=3, source_metadata_json=json.dumps({'model_generated': True})))
        db.add(FileIndexBuild(build_id='build', file_id='file', kb_id='kb', generation=1,
                             task_id='test-task', attempt_no=1, state='ACTIVE', manifest=manifest,
                             manifest_hash=digest(manifest), chunk_count=1))
        db.commit()
    return {'chunk_id': 'chunk', 'file_id': 'file', 'kb_id': 'kb', 'index': 7,
            'content': 'real source\nexact body', 'source_sha256': 'source-hash',
            'index_build_id': 'build', 'verification_status': 'VERIFIED'}


def test_store_constructor_does_not_create_schema(tmp_path):
    path = tmp_path / 'empty.sqlite'
    engine = create_engine('sqlite:///' + path.as_posix())
    ConversationStore(engine)
    assert inspect(engine).get_table_names() == []
    engine.dispose()
    path.unlink(missing_ok=True)


def test_session_crud_revision_archive_and_scope_lock(store):
    session = new_session(store)
    patched = store.patch_session(session['id'], {'title': 'renamed', 'config_overrides': {'model_id': 'm'}}, 1)
    assert patched['revision'] == 2 and patched['title'] == 'renamed'
    expect_error('REVISION_CONFLICT', store.patch_session, session['id'], {'title': 'lost'}, 1)
    assert store.get_session(session['id'])['title'] == 'renamed'
    second = new_session(store, scope_mode='ALL_ENABLED', kb_ids=[])
    assert second['scope_mode'] == 'ALL_ENABLED'
    assert len(store.list_sessions(limit=1)) == 1
    assert len(store.list_sessions(limit=1, offset=1)) == 1
    receipt = start(store, session['id'])
    expect_error('TURN_ACTIVE', store.archive_session, session['id'])
    finish(store, receipt)
    current = store.get_session(session['id'])
    expect_error('SCOPE_LOCKED', store.patch_session, session['id'], {'kb_ids': ['other']}, current['revision'])
    archived = store.archive_session(session['id'])
    assert archived['archived_at']
    assert store.archive_session(session['id'])['revision'] == archived['revision']
    assert [r['id'] for r in store.list_sessions()] == [second['id']]
    assert len(store.list_turns(session['id'])) == 1
    expect_error('SESSION_ARCHIVED', start, store, session['id'], 'new-request')


def test_request_replay_freezes_configuration_and_identity(store):
    session = new_session(store)
    receipt = start(store, session['id'])
    replay = store.start_turn(session['id'], 'question', 'request', -1, {'kb_ids': ['other'], 'model_id': 'changed'})
    assert replay['created'] is False and replay['lease_token'] is None
    assert replay['turn']['id'] == receipt['turn']['id']
    assert replay['turn']['effective_config'] == {'kb_ids': ['kb']}
    expect_error('REQUEST_ID_CONFLICT', store.find_request, session['id'], 'request', 'different')
    finish(store, receipt)
    persisted = ConversationStore(store.engine).find_request(session['id'], 'request', 'question')
    assert persisted['turn']['status'] == 'COMPLETED'
    assert len(store.list_turns(session['id'])) == 1


@pytest.mark.parametrize('same_request', [False, True])
def test_file_sqlite_two_threads_have_one_owner(store, same_request):
    session = new_session(store)
    barrier = Barrier(2)
    def submit(index):
        barrier.wait(timeout=10)
        try:
            return store.start_turn(session['id'], 'question', 'same' if same_request else str(index), 1, {'kb_ids': ['kb']})
        except ConversationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    receipts = [r for r in results if isinstance(r, dict)]
    assert sum(r['created'] for r in receipts) == 1
    assert len(store.list_turns(session['id'])) == 1
    if same_request:
        assert len(receipts) == 2 and {r['turn']['id'] for r in receipts} == {receipts[0]['turn']['id']}
        assert sum(r['lease_token'] is not None for r in receipts) == 1
    else:
        assert len(receipts) == 1
        assert [r for r in results if isinstance(r, str)] == ['TURN_ACTIVE']


@pytest.mark.parametrize('trigger', ['start', 'heartbeat', 'finish'])
def test_expired_lease_recovery_commits_even_when_trigger_rejected(store, trigger):
    session = new_session(store)
    receipt = start(store, session['id'])
    with Session(store.engine) as db:
        row = db.get(ChatTurn, receipt['turn']['id'])
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.add(row)
        db.commit()
    if trigger == 'start':
        expect_error('REVISION_CONFLICT', store.start_turn, session['id'], 'next', 'next', 1, {'kb_ids': ['kb']})
    elif trigger == 'heartbeat':
        expect_error('LEASE_LOST', store.heartbeat_turn, receipt['turn']['id'], receipt['lease_token'])
    else:
        expect_error('LEASE_LOST', finish, store, receipt, answer='late')
    # Read directly, without a Store read that could itself perform recovery.
    with Session(store.engine) as db:
        row = db.get(ChatTurn, receipt['turn']['id'])
        assert (row.status, row.generation_status, row.lease_token) == ('UNKNOWN', 'UNKNOWN', None)
        assert json.loads(row.metadata_json)['recovery']['automatic_retry'] is False
        assert db.get(ChatSession, session['id']).active_turn_id is None
    successor = start(store, session['id'], 'successor')
    expect_error('LEASE_LOST', finish, store, receipt, answer='late overwriter')
    assert store.get_session(session['id'])['active_turn_id'] == successor['turn']['id']
    assert store.get_turn(receipt['turn']['id'])['assistant_content'] == ''


def test_cancel_preserves_partial_answer_and_blocks_assistant_history(store):
    session = new_session(store)
    receipt = start(store, session['id'])
    assert store.cancel_turn(receipt['turn']['id'])['cancel_requested'] is True
    assert store.read_turn_control(receipt['turn']['id'], receipt['lease_token'])['cancel_requested'] is True
    outcome = finish(store, receipt, answer='partial fact', generation='TRUNCATED')
    assert outcome['status'] == 'CANCELLED' and outcome['assistant_content'] == 'partial fact'
    messages, _, _ = prepare_history(store, session['id'], 2, 1, ['kb'])
    assert [m.content for m in messages] == ['question']
    expect_error('LEASE_LOST', store.read_turn_control, receipt['turn']['id'], receipt['lease_token'])


@pytest.mark.parametrize('status,generation,answer', [
    ('COMPLETED', 'NOT_RUN', 'fixed fallback'), ('FAILED', 'EMPTY', ''),
    ('FAILED', 'TRUNCATED', 'partial'), ('FAILED', 'FAILED', 'error'), ('UNKNOWN', 'UNKNOWN', 'uncertain')])
def test_terminal_generation_states_round_trip_without_promoting_answers(store, status, generation, answer):
    session = new_session(store)
    receipt = start(store, session['id'])
    finish(store, receipt, answer=answer, status=status, generation=generation)
    recovered = ConversationStore(store.engine).get_turn(receipt['turn']['id'])
    assert (recovered['status'], recovered['generation_status'], recovered['assistant_content']) == (status, generation, answer)
    messages, sources, _ = prepare_history(store, session['id'], 2, 5, ['kb'])
    assert [m.content for m in messages] == ['question'] and sources == []


@pytest.mark.parametrize('limit', [0, 1, 2, 5])
def test_history_counts_physical_turns_without_backfilling_successes(store, limit):
    session = new_session(store)
    ids = []
    for index in range(6):
        receipt = start(store, session['id'], str(index), 'question-' + str(index))
        ids.append(receipt['turn']['id'])
        finish(store, receipt, answer='answer-' + str(index),
               status='COMPLETED' if index < 3 else 'FAILED',
               generation='COMPLETED' if index < 3 else 'TRUNCATED')
    selected = ids[-limit:] if limit else []
    messages, refs, metadata = prepare_history(store, session['id'], 7, limit, ['kb'])
    assert metadata['history_turn_ids'] == selected
    expected = []
    for index in range(6 - limit, 6):
        expected.append('question-' + str(index))
        if index < 3:
            expected.append('answer-' + str(index))
    assert [m.content for m in messages] == expected and refs == []


def test_evidence_bodies_deduplicate_but_keep_source_identity_and_locator(store):
    source = seed_source(store)
    session = new_session(store)
    for i in range(2):
        finish(store, start(store, session['id'], str(i)), refs=[source])
    with Session(store.engine) as db:
        bodies = db.exec(select(ChatEvidenceBody)).all()
        refs = db.exec(select(ChatTurnReference)).all()
        assert len(bodies) == 1 and bodies[0].content == source['content']
        assert len(refs) == 2 and refs[0].turn_id != refs[1].turn_id
        assert all('content' not in json.loads(r.payload_json) for r in refs)
    messages, candidates, _ = prepare_history(store, session['id'], 3, 2, ['kb'])
    assert len(candidates) == 2 and all(c['page_number'] == 3 for c in candidates)
    assert all(c['source_metadata']['model_generated'] is True for c in candidates)
    assert [m.content for m in messages][1] == 'answer (historical source 1:7)'


@pytest.mark.parametrize('mutation', ['not_in_manifest', 'bad_manifest_hash', 'retired', 'new_build', 'source_hash',
                                    'deleted_file', 'deleted_kb', 'wrong_build_file', 'wrong_reference_file', 'scope'])
def test_historical_source_revalidation_rejects_invalid_active_identity(store, mutation):
    source = seed_source(store)
    if mutation == 'wrong_reference_file':
        source['file_id'] = 'different-file'
    session = new_session(store)
    receipt = start(store, session['id'])
    finish(store, receipt, refs=[source])
    with Session(store.engine) as db:
        build, file, kb = db.get(FileIndexBuild, 'build'), db.get(FileDocument, 'file'), db.get(KnowledgeBase, 'kb')
        if mutation == 'not_in_manifest':
            build.manifest = json.dumps({'chunk_ids': ['other']})
            build.manifest_hash = digest(build.manifest)
        elif mutation == 'bad_manifest_hash': build.manifest_hash = 'bad'
        elif mutation == 'retired': build.state = 'RETIRED'
        elif mutation == 'new_build': file.active_build_id = 'new-build'
        elif mutation == 'source_hash': file.sha256 = 'changed'
        elif mutation == 'deleted_file': file.deleted_at = datetime.utcnow()
        elif mutation == 'deleted_kb': kb.is_deleted = True
        elif mutation == 'wrong_build_file': build.file_id = 'other-file'
        for row in (build, file, kb): db.add(row)
        db.commit()
    messages, candidates, metadata = prepare_history(store, session['id'], 2, 1, ['other'] if mutation == 'scope' else ['kb'])
    assert candidates == []
    assert store.get_turn(receipt['turn']['id'])['citations'][0]['content'] == source['content']
    if mutation == 'scope':
        assert messages == [] and metadata['excluded_turns'][0]['reason'] == 'SCOPE_CHANGED'


def legacy_payload(refs=None):
    return {'id': 'browser-old', 'title': 'old', 'kbIds': ['kb'], 'unknown': {'nested': [1, '二']},
            'messages': [{'role': 'assistant', 'content': 'orphan'},
                         {'role': 'user', 'content': '  exact\r\n用户  ', 'extra': 3},
                         {'role': 'assistant', 'content': 'answer [7]', 'citations': refs or []},
                         {'role': 'user', 'content': 'unanswered'}]}


def test_import_partial_invalid_preserves_raw_unmatched_idempotence_and_changed_copy(store):
    raw = legacy_payload([{'chunk_id': 'unresolvable', 'content': ' old body ', 'index': 7}])
    payload = {'sessions': [raw, {'id': 'bad', 'messages': 'invalid'}, None]}
    preview = store.preview_import(payload)
    assert (preview['importable_count'], len(preview['errors'])) == (1, 2)
    assert store.list_sessions() == []
    imported = store.import_history(payload)
    assert (imported['imported_count'], len(imported['errors'])) == (1, 2)
    assert [e['index'] for e in imported['errors']] == [1, 2]
    sid = imported['sessions'][0]['id']
    with Session(store.engine) as db:
        stored = json.loads(db.get(ChatSession, sid).legacy_metadata_json)
        assert stored['original'] == raw
        assert stored['unmatched_messages'] == [raw['messages'][0], raw['messages'][-1]]
    turn = store.list_turns(sid)[0]
    assert turn['user_content'] == raw['messages'][1]['content']
    assert turn['status'] == 'LEGACY_IMPORTED' and turn['generation_status'] == 'UNKNOWN'
    assert turn['citations'][0]['verification_status'] == 'UNKNOWN'
    repeated = store.import_history(payload)
    assert (repeated['imported_count'], repeated['existing_count']) == (0, 1)
    assert repeated['sessions'][0]['id'] == sid
    changed = deepcopy(raw)
    changed['messages'][2]['content'] = 'changed'
    copied = store.import_history({'sessions': [changed]})
    assert copied['sessions'][0]['id'] != sid
    assert store.list_turns(sid)[0]['assistant_content'] == 'answer [7]'
    assert len(store.list_sessions()) == 2


def test_reference_outside_scope_excludes_turn_and_missing_import_scope_is_rejected(store):
    session = new_session(store)
    finish(store, start(store, session['id']), refs=[{'kb_id': 'outside', 'content': 'private'}])
    messages, candidates, metadata = prepare_history(store, session['id'], 2, 1, ['kb'])
    assert messages == [] and candidates == []
    assert metadata['excluded_turns'][0]['reason'] == 'REFERENCE_OUT_OF_SCOPE'
    raw = legacy_payload()
    del raw['kbIds']
    result = store.import_history({'sessions': [raw]})
    assert result['imported_count'] == 0
    assert result['errors'][0]['code'] == 'INVALID_IMPORT_SCOPE'


@pytest.mark.parametrize('file_hash,manifest_hash,claimed_hash,valid', [
    (None, 'source-hash', 'source-hash', True),
    (None, 'source-hash', 'wrong-source', False),
    (None, None, 'source-hash', False),
    ('source-hash', 'source-hash', 'source-hash', True),
    ('source-hash', 'conflicting-manifest', 'source-hash', False),
    ('source-hash', None, 'source-hash', True),
    (None, ['malformed'], 'source-hash', False),
])
def test_legacy_document_hash_uses_verified_active_manifest(store, file_hash, manifest_hash, claimed_hash, valid):
    source = seed_source(store)
    source['source_sha256'] = claimed_hash
    with Session(store.engine) as db:
        file, build = db.get(FileDocument, 'file'), db.get(FileIndexBuild, 'build')
        file.sha256 = file_hash
        manifest = {'chunk_ids': ['chunk']}
        if manifest_hash is not None:
            manifest['source_sha256'] = manifest_hash
        build.manifest = json.dumps(manifest)
        build.manifest_hash = digest(build.manifest)
        db.add(file)
        db.add(build)
        db.commit()
    session = new_session(store)
    finish(store, start(store, session['id']), refs=[source])
    _, candidates, _ = prepare_history(store, session['id'], 2, 1, ['kb'])
    assert len(candidates) == int(valid)
    if valid:
        assert candidates[0]['source_sha256'] == 'source-hash'
        assert candidates[0]['source_metadata']['source_sha256'] == 'source-hash'


@pytest.mark.parametrize('mutation', ['body', 'changed_build', 'bad_manifest_hash', 'not_in_manifest'])
def test_manifest_hash_fallback_does_not_weaken_body_build_or_membership_checks(store, mutation):
    source = seed_source(store)
    with Session(store.engine) as db:
        file, build = db.get(FileDocument, 'file'), db.get(FileIndexBuild, 'build')
        file.sha256 = None
        manifest = {'chunk_ids': ['chunk'], 'source_sha256': 'source-hash'}
        if mutation == 'not_in_manifest': manifest['chunk_ids'] = []
        build.manifest = json.dumps(manifest)
        build.manifest_hash = 'incorrect' if mutation == 'bad_manifest_hash' else digest(build.manifest)
        if mutation == 'changed_build': file.active_build_id = 'different-build'
        db.add(file)
        db.add(build)
        db.commit()
    if mutation == 'body':
        source['content'] = 'unverified expanded snapshot'
        source['content_hash'] = digest(source['content'])
        source['verification_status'] = 'VERIFIED'
    session = new_session(store)
    finish(store, start(store, session['id']), refs=[source])
    _, candidates, _ = prepare_history(store, session['id'], 2, 1, ['kb'])
    assert candidates == []


def test_imported_forged_verified_snapshot_is_displayed_but_not_reused(store):
    source = seed_source(store)
    source['content'] = 'forged expanded source with invented statements'
    source['content_hash'] = digest(source['content'])
    source['verification_status'] = 'VERIFIED'
    imported = store.import_history({'sessions': [legacy_payload([source])]})
    sid = imported['sessions'][0]['id']
    displayed = store.list_turns(sid)[0]['citations'][0]
    assert displayed['content'] == source['content']
    assert displayed['verification_status'] == 'UNKNOWN'
    messages, candidates, _ = prepare_history(store, sid, 2, 1, ['kb'])
    assert len(messages) == 2 and candidates == []


@pytest.mark.parametrize('malformed', [
    {'chunk_id': ['chunk'], 'kb_id': {'bad': 'type'}, 'content': 'body'},
    {'chunk_id': 'chunk', 'source_metadata': ['malformed'], 'content': 'body'},
    {'chunk_id': 'chunk', 'content': {'bad': 'body'}},
    {'chunk_id': 'chunk', 'index_build_id': ['build'], 'source_sha256': 123, 'content': 'body'},
])
def test_malformed_legacy_references_remain_displayable_and_are_not_reused(store, malformed):
    seed_source(store)
    imported = store.import_history({'sessions': [legacy_payload([malformed])]})
    sid = imported['sessions'][0]['id']
    assert len(store.list_turns(sid)[0]['citations']) == 1
    messages, candidates, _ = prepare_history(store, sid, 2, 1, ['kb'])
    assert len(messages) == 2 and candidates == []


@pytest.mark.parametrize('allow_web', [False, True])
def test_web_snapshot_history_is_explicit_and_renumbers_later(store, allow_web):
    source = {'source_type': 'web', 'url': 'https://example.org/report',
              'title': 'Report', 'content': 'Revenue is 42 million.',
              'content_hash': digest('Revenue is 42 million.'),
              'web_snapshot_status': 'CAPTURED_SEARCH_RESULT',
              'source_id': 7, 'citation_index': 7, 'fetched_at': '2026-11-15T00:00:00+00:00'}
    session = new_session(store)
    receipt = start(store, session['id'])
    finish(store, receipt, answer='Revenue 42 [7]', refs=[source])
    stored = store.get_turn(receipt['turn']['id'])['citations'][0]
    assert stored['url'] == source['url'] and stored['title'] == 'Report'
    assert stored['trust_status'] == 'CAPTURED_SEARCH_RESULT'
    assert stored['verification_status'] == 'SNAPSHOT_ONLY'
    messages, candidates, _ = prepare_history(store, session['id'], 2, 5, ['kb'], allow_web=allow_web)
    assert '[7]' not in messages[-1].content
    assert len(candidates) == int(allow_web)
    if allow_web:
        assert candidates[0]['origin'] == 'HISTORY_WEB_SNAPSHOT'
        assert candidates[0]['content'] == source['content']
        assert candidates[0]['history_citation_index'] == 7


@pytest.mark.parametrize('change', ['bad_hash', 'missing_capture', 'invalid_url'])
def test_unverifiable_web_history_is_not_reused(store, change):
    source = {'source_type': 'web', 'url': 'https://example.org/report',
              'title': 'Report', 'content': '42 million', 'content_hash': digest('42 million'),
              'web_snapshot_status': 'CAPTURED_SEARCH_RESULT'}
    if change == 'missing_capture':
        source.pop('web_snapshot_status')
    if change == 'invalid_url':
        source['url'] = 'javascript:alert(1)'
    session = new_session(store)
    receipt = start(store, session['id'])
    finish(store, receipt, refs=[source])
    # Body store computes hashes from actual bytes; corrupt the normalized
    # loaded reference to exercise the history read boundary, not fake trust.
    if change == 'bad_hash':
        original = store.history_turns
        def corrupt(*args):
            turns = original(*args)
            turns[0]['citations'][0]['content_hash'] = 'bad'
            return turns
        store.history_turns = corrupt
    _, candidates, _ = prepare_history(store, session['id'], 2, 5, ['kb'], allow_web=True)
    assert candidates == []


def test_historical_unresolved_alias_cannot_collide_with_new_registry(store):
    session = new_session(store)
    finish(store, start(store, session['id']), answer='Earlier [7] and unresolved [s99]')
    messages, _, _ = prepare_history(store, session['id'], 2, 5, ['kb'])
    assert messages[-1].content == 'Earlier (historical source 1:7) and unresolved (historical source 1:99)'
