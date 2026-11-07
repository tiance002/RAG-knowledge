import pytest
from sqlmodel import Session, create_engine

from app.models import KnowledgeBase
from app.services import chat


def test_empty_selection_resolves_all_non_deleted_knowledge_bases(monkeypatch):
    engine = create_engine('sqlite://')
    KnowledgeBase.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            KnowledgeBase(id='one', name='one', embedding_model='e', vlm_model='v'),
            KnowledgeBase(id='two', name='two', embedding_model='e', vlm_model='v'),
            KnowledgeBase(id='deleted', name='deleted', embedding_model='e', vlm_model='v', is_deleted=True),
        ])
        session.commit()
    monkeypatch.setattr(chat, 'engine', engine)
    assert set(chat._resolve_knowledge_scope([])) == {'one', 'two'}
    assert chat._resolve_knowledge_scope(['one']) == ['one']


def test_scope_database_failure_is_not_empty_scope(monkeypatch):
    def unavailable(*args):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(chat, 'Session', unavailable)
    with pytest.raises(RuntimeError):
        chat._resolve_knowledge_scope([])
