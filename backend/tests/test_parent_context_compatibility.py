"""Legacy Settings compatibility, using real isolated SQL parent expansion."""
from types import SimpleNamespace

import pytest
from sqlmodel import SQLModel,Session,create_engine
from app.models import DocumentChunk,FileDocument,KnowledgeBase
from app.services import parent_context
from app.services.context_builder import build_chat_context


@pytest.fixture
def records(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    engine=create_engine('sqlite:///'+str(tmp_path/'compat.sqlite'))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb',name='test',embedding_model='none',vlm_model='none'))
        session.add(FileDocument(id='file',kb_id='kb',name='source.txt',local_path='synthetic',size=0))
        session.add_all([DocumentChunk(id='parent',file_id='file',content='alpha beta',chunk_type='parent'),
            DocumentChunk(id='child',file_id='file',content='alpha',parent_id='parent',chunk_type='child')])
        session.commit()
        yield session
    engine.dispose()


def hit(chunk_id='child'):
    return dict(id=chunk_id,file_id='file',file_name='source.txt',kb_id='kb',score=.8)


def expand(session,**kwargs):
    return parent_context.expand_parent_context([hit()],session,window_size=0,
        include_source_members=True,allowed_kb_ids=['kb'],**kwargs)


def test_actual_legacy_settings_without_aggregate_field_expands_parent(records,monkeypatch):
    settings=SimpleNamespace(CONTEXT_WINDOW_SIZE=1)
    assert not hasattr(settings,'CONTEXT_MAX_CHARS')
    monkeypatch.setattr(parent_context,'get_settings',lambda:settings)
    result=expand(records)
    assert result[0]['content']=='alpha beta' and result[0]['parent_chunk_id']=='parent'
    citations,_=build_chat_context(result,lambda row,text:dict(chunk_id=row['id'],content=text))
    assert citations[0]['chunk_id']=='parent' and citations[0]['content']=='alpha beta'


@pytest.mark.parametrize('configured,explicit,expected',[(8,None,'alpha'),(8,40,'alpha beta'),
    (40,0,'alpha'),(0,None,'alpha'),(40,None,'alpha beta')])
def test_configured_budget_and_explicit_zero_precedence(records,monkeypatch,configured,explicit,expected):
    monkeypatch.setattr(parent_context,'get_settings',lambda:SimpleNamespace(
        CONTEXT_WINDOW_SIZE=1,CONTEXT_MAX_CHARS=configured))
    result=expand(records,max_context_chars=explicit)
    assert result[0]['content']==expected
    if configured == 0 or explicit == 0:
        assert result[0]['context_group']['omitted_seed_ids']==[]
        assert result[0]['delivery_budget']['mandatory_overrun'] is True


def test_legacy_fallback_is_finite_and_keeps_true_seeds(records,monkeypatch):
    monkeypatch.setattr(parent_context,'get_settings',lambda:SimpleNamespace(CONTEXT_WINDOW_SIZE=1))
    seeds=[]
    for index in range(8):
        text='hit-'+str(index)
        records.add(DocumentChunk(id='p'+str(index),file_id='file',content=text+'x'*3200,chunk_type='parent'))
        records.add(DocumentChunk(id='c'+str(index),file_id='file',content=text,chunk_type='child',parent_id='p'+str(index)))
        seeds.append(hit('c'+str(index)))
    records.commit()
    result=parent_context.expand_parent_context(seeds,records,window_size=0,
        include_source_members=True,allowed_kb_ids=['kb'],max_parent_contexts=8)
    assert sum(len(row['content'])+2 for row in result)<=24000
    assert all('hit-'+str(index) in row['content'] for index,row in enumerate(result))
    assert any(row['context_group']['degradation_reason']=='PARENT_OVER_BUDGET' for row in result)
