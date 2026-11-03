"""Synthetic only: ownership preparation and the real SQL expansion path."""
import copy
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from replay_source_preparation import prepare_replay_sql, expand_replay_context
from app.models import DocumentChunk
from app.services import rerank


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    db = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    yield db
    db.dispose()


def chunks():
    return [{'id':'seed', 'file_id':'file', 'content':'SYNTHETIC_SEED', 'original_index':0,
             'parent_id':'parent', 'page_number':2, 'heading_text':'Synthetic heading', 'heading_level':1},
            {'id':'neighbor', 'file_id':'file', 'content':'SYNTHETIC_NEIGHBOR', 'original_index':1,
             'parent_id':'parent', 'page_number':3}]


def seed():
    return {'id':'seed', 'file_id':'file', 'kb_id':'kb', 'content':'SYNTHETIC_SEED'}


def prepare(engine, **kwargs):
    return prepare_replay_sql(engine, chunks=chunks(), files=[{'id':'file','kb_id':'kb','name':'synthetic.pdf'}],
                              allowed_kb_ids=['kb'], ownership_provenance='Synthetic fixture; explicit test ownership', **kwargs)


def test_old_documentchunk_only_preparation_yields_actual_empty_expansion(engine):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([DocumentChunk(**row) for row in chunks()])
        session.commit()
        assert rerank.expand_context([seed()], session, include_source_members=True) == []
        with pytest.raises(ValueError, match='verified source ownership'):
            expand_replay_context(session, [seed()], allowed_kb_ids=['kb'], expected_seed_count=1)


def test_verified_owners_restore_actual_nonempty_expansion_and_metadata(engine):
    result = prepare(engine)
    assert result['files'] == 1 and result['chunks'] == 2
    with Session(engine) as session:
        expanded = expand_replay_context(session, [seed()], allowed_kb_ids=['kb'], expected_seed_count=1, window_size=1)
    assert [row['id'] for row in expanded] == ['seed']
    members = {row['id']: row for row in expanded[0]['context_group']['source_members']}
    assert set(members) == {'seed', 'neighbor'}
    assert members['seed']['page_number'] == 2 and members['neighbor']['page_number'] == 3


def test_missing_file_ownership_is_rejected_without_inference(engine):
    with pytest.raises(ValueError, match='no verified FileDocument ownership'):
        prepare_replay_sql(engine, chunks=chunks(), files=[], allowed_kb_ids=['kb'], ownership_provenance='synthetic')


def test_missing_expected_hits_and_undeclared_zero_rejected(engine):
    prepare(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match='expected seed count mismatch'):
            expand_replay_context(session, [], allowed_kb_ids=['kb'], expected_seed_count=1)
        with pytest.raises(ValueError, match='explicit clean_zero'):
            expand_replay_context(session, [], allowed_kb_ids=['kb'], expected_seed_count=0)
        assert expand_replay_context(session, [], allowed_kb_ids=['kb'], expected_seed_count=0, clean_zero=True) == []


def test_cross_kb_seed_and_missing_actual_expansion_rejected(engine, monkeypatch):
    prepare(engine)
    with Session(engine) as session:
        wrong = copy.deepcopy(seed()); wrong['kb_id'] = 'other'
        with pytest.raises(ValueError, match='verified source ownership'):
            expand_replay_context(session, [wrong], allowed_kb_ids=['kb'], expected_seed_count=1)
        monkeypatch.setattr(rerank, 'expand_context', lambda *args, **kwargs: [])
        with pytest.raises(ValueError, match='actual expansion lost expected seed hits'):
            expand_replay_context(session, [seed()], allowed_kb_ids=['kb'], expected_seed_count=1)


def test_verified_catalog_keeps_other_kb_collision_but_expansion_is_scoped(engine):
    rows = chunks() + [{'id':'foreign', 'file_id':'foreign-file', 'content':'FOREIGN_SECRET',
                        'original_index':1, 'parent_id':'parent'}]
    prepare_replay_sql(engine, chunks=rows,
        files=[{'id':'file','kb_id':'kb','name':'synthetic.pdf'},
               {'id':'foreign-file','kb_id':'foreign-kb','name':'foreign.pdf'}],
        allowed_kb_ids=['kb'], ownership_provenance='Synthetic collision fixture explicit ownership')
    with Session(engine) as session:
        assert session.get(DocumentChunk, 'foreign') is not None
        expanded = expand_replay_context(session, [seed()], allowed_kb_ids=['kb'], expected_seed_count=1)
        assert 'FOREIGN_SECRET' not in expanded[0]['content']
        assert {m['id'] for m in expanded[0]['context_group']['source_members']} == {'seed','neighbor'}
        foreign_seed = {'id':'foreign','file_id':'foreign-file','kb_id':'foreign-kb','content':'FOREIGN_SECRET'}
        with pytest.raises(ValueError, match='verified source ownership'):
            expand_replay_context(session, [foreign_seed], allowed_kb_ids=['kb'], expected_seed_count=1)
