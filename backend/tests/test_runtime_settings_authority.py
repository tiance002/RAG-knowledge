"""Focused, offline configuration contracts; no provider or production data."""
import json
from types import SimpleNamespace

import pytest
from sqlmodel import SQLModel, Session, create_engine

from app.services import runtime_settings as runtime
from app.services import model_bindings as bindings
from app.models import CustomModel, ModelType, KnowledgeBase


@pytest.fixture
def configured(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'settings.db'))
    SQLModel.metadata.create_all(engine)
    settings = SimpleNamespace(DEFAULT_LLM_MODEL_ID='', DEFAULT_EMBEDDING_MODEL_ID='',
        DEFAULT_VLM_MODEL_ID='', LLM_MODEL='synthetic-chat', LLM_BASE_URL='http://localhost/v1',
        LLM_API_KEY='', EMBEDDING_MODEL='synthetic-embedding', EMBEDDING_BASE_URL='http://localhost/v1',
        EMBEDDING_API_KEY='', EMBEDDING_MAX_CONTEXT_LENGTH=8192, VLM_MODEL='',
        VLM_BASE_URL='', VLM_API_KEY='', CHUNK_SIZE=900, CHUNK_OVERLAP=0,
        MAX_PARSING_WORKERS=10, CELERY_WORKER_CONCURRENCY=1, MAX_CHAT_HISTORY_ROUNDS=8)
    monkeypatch.setattr(runtime, 'get_settings', lambda: settings)
    monkeypatch.setattr(bindings, 'get_settings', lambda: settings)
    yield engine, settings
    runtime._last_good.pop(engine, None)
    engine.dispose()


def test_initial_values_migrate_once_not_environment_override(configured):
    engine, settings = configured
    with Session(engine) as s:
        first = runtime.get_runtime_settings(s)
        assert (first.chunk_size, first.chunk_overlap, first.default_llm_model) == (900, 0, 'sys_llm')
        settings.CHUNK_SIZE = 1200
        assert runtime.get_runtime_settings(s).chunk_size == 900


def test_legacy_partial_values_are_preserved_and_only_missing_are_seeded(configured):
    engine, _ = configured
    with Session(engine) as s:
        s.add(runtime.PersistedSystemSettings(data=json.dumps({'chunk_size': 777, 'chunk_overlap': 0,
                                                               'default_llm_model': None})))
        s.commit()
        loaded = runtime.get_runtime_settings(s)
        assert loaded.chunk_size == 777 and loaded.chunk_overlap == 0
        assert loaded.default_llm_model is None
        assert loaded.default_embedding_model == 'sys_embedding'


def test_stale_write_rejected_and_null_has_explicit_meaning(configured):
    engine, _ = configured
    with Session(engine) as s:
        first = runtime.get_runtime_settings(s)
        updated = runtime.update_runtime_settings(s, {'chunk_size': None, 'default_llm_model': None}, first.revision)
        assert updated.chunk_size == 500 and updated.default_llm_model is None
        with pytest.raises(runtime.SettingsConflict):
            runtime.update_runtime_settings(s, {'chunk_size': 700}, first.revision)
        assert runtime.get_runtime_settings(s).revision == updated.revision


def test_bad_record_is_not_overwritten_and_last_good_is_flagged(configured):
    engine, _ = configured
    with Session(engine) as s:
        first = runtime.get_runtime_settings(s)
        row = s.get(runtime.PersistedSystemSettings, 'system')
        row.data = '{broken'; s.add(row); s.commit()
        fallback = runtime.get_runtime_settings(s)
        assert fallback.degraded and fallback.chunk_size == first.chunk_size
        assert s.get(runtime.PersistedSystemSettings, 'system').data == '{broken'
        runtime._last_good.pop(engine)
        with pytest.raises(runtime.RuntimeSettingsUnavailable):
            runtime.get_runtime_settings(s)


def test_missing_explicit_embedding_never_uses_system(configured):
    engine, _ = configured
    with Session(engine) as s:
        with pytest.raises(bindings.ModelBindingError, match='MODEL_REFERENCE_MISSING'):
            bindings.resolve_model(s, 'embedding', 'deleted-id')
        assert bindings.resolve_model(s, 'embedding', 'sys_embedding').binding_id == 'sys_embedding'


def test_reference_check_includes_kb_and_saved_defaults(configured):
    engine, _ = configured
    with Session(engine) as s:
        s.add(CustomModel(id='embed', name='embed', model_type=ModelType.EMBEDDING,
                          base_url='http://localhost/v1', api_key='', model_name='synthetic'))
        s.add(KnowledgeBase(id='kb', name='kb', embedding_model='embed', vlm_model=''))
        s.commit()
        first = runtime.get_runtime_settings(s)
        runtime.update_runtime_settings(s, {'default_embedding_model': 'embed'}, first.revision)
        assert set(bindings.model_references(s, 'embed')) == {'knowledge_base:kb', 'settings:default_embedding_model'}


def test_invalid_unused_role_is_warning_not_global_outage(configured):
    engine, _ = configured
    with Session(engine) as s:
        s.add(runtime.PersistedSystemSettings(data=json.dumps({
            'default_vlm_model': 'missing-vlm', 'default_llm_model': 'sys_llm',
        })))
        s.commit()
        loaded = runtime.get_runtime_settings(s)
        assert not loaded.degraded
        assert loaded.default_vlm_model == 'missing-vlm'
        assert 'MODEL_REFERENCE_MISSING: vlm' in loaded.config_error
        healthy = bindings.resolve_model(s, 'chat')
        assert healthy.binding_id == 'sys_llm' and healthy.source == 'deployment'
        with pytest.raises(bindings.ModelBindingError, match='MODEL_REFERENCE_MISSING: vlm'):
            bindings.resolve_model(s, 'vlm')
        updated = runtime.update_runtime_settings(s, {'chunk_size': 800}, loaded.revision)
        assert updated.chunk_size == 800 and updated.default_vlm_model == 'missing-vlm'
        cleared = runtime.update_runtime_settings(s, {'default_vlm_model': None}, updated.revision)
        assert cleared.default_vlm_model is None and cleared.config_error is None


def test_invalid_new_role_selection_is_rejected(configured):
    engine, _ = configured
    with Session(engine) as s:
        current = runtime.get_runtime_settings(s)
        with pytest.raises(bindings.ModelBindingError, match='MODEL_REFERENCE_MISSING: vlm'):
            runtime.update_runtime_settings(s, {'default_vlm_model': 'missing-vlm'}, current.revision)
        assert runtime.get_runtime_settings(s).revision == current.revision


def test_active_task_model_reference_survives_cleared_default(configured):
    from datetime import datetime
    from sqlalchemy import MetaData, Table, inspect, text
    engine, _ = configured
    with Session(engine) as s:
        current = runtime.get_runtime_settings(s)
        runtime.update_runtime_settings(s, {'default_embedding_model': None}, current.revision)
        if not inspect(s.connection()).has_table('file_task_receipts'):
            s.execute(text('CREATE TABLE file_task_receipts ('
                'task_id TEXT PRIMARY KEY, file_id TEXT, operation TEXT, '
                'active_file TEXT, status TEXT, payload TEXT)'))
        table = Table('file_task_receipts', MetaData(), autoload_with=s.connection())
        rows = [
            {'task_id':'queued-vector', 'status':'QUEUED', 'active_file':None,
             'payload':json.dumps({'vector_model':'protected-model'})},
            {'task_id':'unknown-vlm', 'status':'UNKNOWN', 'active_file':None,
             'payload':json.dumps({'vlm_model_id':'protected-model'})},
            {'task_id':'active-semantic', 'status':'COMPLETED', 'active_file':'held-file',
             'payload':json.dumps({'embedding_model':'protected-model'})},
            {'task_id':'finished', 'status':'COMPLETED', 'active_file':None,
             'payload':json.dumps({'vector_model':'protected-model'})},
        ]
        for row in rows:
            row.update(file_id='file-' + row['task_id'], operation='parse')
            for stamp in ('created_at', 'updated_at'):
                if stamp in table.c:
                    row[stamp] = datetime.utcnow()
        s.execute(table.insert(), rows); s.commit()
        assert set(bindings.model_references(s, 'protected-model')) == {
            'task:queued-vector', 'task:unknown-vlm', 'task:active-semantic'}
        assert bindings.model_references(s, 'another-model') == []


def test_full_form_unchanged_bad_role_does_not_block_numeric_edit(configured):
    engine, _ = configured
    with Session(engine) as session:
        session.add(runtime.PersistedSystemSettings(data=json.dumps({
            'chunk_size': 700, 'chunk_overlap': 0, 'default_vlm_model': 'missing-vlm'})))
        session.commit()
        before = runtime.get_runtime_settings(session)
        after = runtime.update_runtime_settings(session, {
            'chunk_size': 800, 'default_vlm_model': 'missing-vlm'}, before.revision)
        assert after.chunk_size == 800
        assert after.default_vlm_model == 'missing-vlm'
        assert after.config_error


def test_bootstrap_reuses_exact_existing_user_vlm_record(configured):
    engine, settings = configured
    settings.VLM_MODEL = 'synthetic-vision'
    settings.VLM_BASE_URL = 'http://localhost/vision/v1'
    settings.VLM_API_KEY = 'synthetic-not-a-secret'
    with Session(engine) as session:
        session.add(CustomModel(id='user-vision', name='User vision', model_type=ModelType.VLM,
            model_name=settings.VLM_MODEL, base_url=settings.VLM_BASE_URL, api_key=settings.VLM_API_KEY))
        session.commit()
        assert runtime.get_runtime_settings(session).default_vlm_model == 'user-vision'
