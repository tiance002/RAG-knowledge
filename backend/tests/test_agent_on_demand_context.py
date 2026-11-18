"""Agent search/read boundaries, using real SQL publication records and no models."""
import asyncio
import hashlib
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.tools import retrieval_tool

reader = importlib.import_module('app.tools.read_knowledge_context')


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id='kb', name='Known', embedding_model='none', vlm_model='none'))
        db.add(FileDocument(id='file', kb_id='kb', name='source.pdf', local_path='synthetic', size=0))
        db.add_all([
            DocumentChunk(id='parent', file_id='file', chunk_type='parent', content='before seed after'),
            DocumentChunk(id='seed', file_id='file', chunk_type='child', parent_id='parent', content='seed', original_index=1),
            DocumentChunk(id='neighbor', file_id='file', chunk_type='content', content='nearby', original_index=2),
        ])
        db.commit()
    monkeypatch.setattr(reader, 'engine', engine)
    monkeypatch.setattr(retrieval_tool, 'engine', engine)
    yield engine
    engine.dispose()


def read(**kwargs):
    chunk_id = kwargs.pop('chunk_id')
    return json.loads(asyncio.run(reader.read_registered_context({'chunk_id': chunk_id}, **kwargs)))


def test_agent_search_expansion_is_opt_in_compatible(source, monkeypatch):
    service = SimpleNamespace(search=AsyncMock(return_value=([
        dict(id='seed', file_id='file', kb_id='kb', file_name='source.pdf',
             chunk_type='child', parent_id='parent', content='seed', score=.8)],
        {'retrieval_status': 'SUCCESS'})))
    monkeypatch.setattr(retrieval_tool, 'get_retrieval_service', lambda: service)
    result = json.loads(asyncio.run(retrieval_tool.search_knowledge_base.coroutine(
        query='q', kb_ids=['kb'], context_window=2, expand_evidence=False)))
    assert [(c['chunk_id'], c['content']) for c in result['citations']] == [('seed', 'seed')]
    assert result['citations'][0]['parent_id'] == 'parent'
    assert result['expansion_delivery_status'] == 'NOT_REQUESTED'
    default = json.loads(asyncio.run(retrieval_tool.search_knowledge_base.coroutine(
        query='q', kb_ids=['kb'])))
    assert default['citations'][0]['chunk_id'] == 'parent'


def test_read_delivers_verified_parent(source):
    result = read(chunk_id='seed', kb_ids=['kb'], window=1)
    assert result['retrieval_status'] == 'SUCCESS'
    assert result['requested_chunk_id'] == 'seed'
    assert [(c['chunk_id'], c['content']) for c in result['citations']] == [('parent', 'before seed after')]
    assert result['citations'][0]['seed_child_ids'] == ['seed']


@pytest.mark.parametrize('scope,window', [(None, 1), ([], 1), (['elsewhere'], 1), (['kb'], -1), (['kb'], 3)])
def test_read_never_broadens_scope_or_window(source, scope, window):
    assert read(chunk_id='seed', kb_ids=scope, window=window)['citations'] == []


def test_read_rejects_superseded_file(source):
    with Session(source) as db:
        db.add(FileDocument(id='new', kb_id='kb', name='new.pdf', local_path='x', size=0,
            supersedes_file_id='file', status='ready'))
        db.commit()
    assert read(chunk_id='seed', kb_ids=['kb'])['citations'] == []


@pytest.mark.parametrize('manifest_ids,expected', [(['seed'], []), (['seed', 'parent'], ['parent'])])
def test_read_requires_active_manifest_for_all_delivered_members(source, manifest_ids, expected):
    manifest = json.dumps({'chunk_ids': manifest_ids})
    with Session(source) as db:
        doc = db.get(FileDocument, 'file')
        doc.active_build_id = 'build'
        db.add(doc)
        db.add(FileIndexBuild(build_id='build', file_id='file', kb_id='kb', generation=1,
            task_id='task', attempt_no=1, state='ACTIVE', manifest=manifest,
            manifest_hash=hashlib.sha256(manifest.encode()).hexdigest(), chunk_count=len(manifest_ids)))
        db.commit()
    assert [c['chunk_id'] for c in read(chunk_id='seed', kb_ids=['kb'])['citations']] == expected


def test_public_read_schema_uses_only_source_number_and_window():
    assert set(reader.read_knowledge_context.args) == {'source_id', 'window'}


def test_registered_parent_resolves_only_recorded_seed(source):
    result = json.loads(asyncio.run(reader.read_registered_context(
        {'chunk_id': 'parent', 'fileId': 'file', 'kb_id': 'kb', 'seed_child_ids': ['seed']}, ['kb'])))
    assert [c['chunk_id'] for c in result['citations']] == ['parent']


def test_registered_source_owner_must_match_sql(source):
    result = json.loads(asyncio.run(reader.read_registered_context(
        {'chunk_id': 'seed', 'fileId': 'different-file', 'kb_id': 'kb'}, ['kb'])))
    assert result['citations'] == []


def test_public_read_schema_accepts_exact_alias_and_legacy_number():
    schema = reader.read_knowledge_context.args_schema
    assert schema.model_validate({'source_id': 's1'}).source_id == 's1'
    assert schema.model_validate({'source_id': 1}).source_id == 1
    assert set(reader.read_knowledge_context.args) == {'source_id', 'window'}
