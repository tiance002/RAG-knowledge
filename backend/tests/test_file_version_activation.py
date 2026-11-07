from sqlmodel import SQLModel, Session, create_engine
from app.models import FileDocument, KnowledgeBase, FileStatus, DocumentChunk
from app.services.file_versions import inactive_file_ids


def test_replacement_activates_only_after_index_success_preserves_history():
    engine=create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb',name='kb',embedding_model='',vlm_model=''))
        s.add_all([
            FileDocument(id='old',kb_id='kb',name='same',local_path='old',size=1,status=FileStatus.READY),
            FileDocument(id='new',kb_id='kb',name='same',local_path='new',size=1,supersedes_file_id='old'),
            FileDocument(id='independent',kb_id='kb',name='same',local_path='other',size=1,status=FileStatus.READY)])
        s.commit()
        s.add(DocumentChunk(id='old-chunk',file_id='old',content='historical fact'))
        s.add(DocumentChunk(id='new-chunk',file_id='new',content='updated fact'))
        s.commit()
        assert inactive_file_ids(s,['kb'])==set()
        new=s.get(FileDocument,'new'); new.status=FileStatus.FAILED; s.commit()
        assert inactive_file_ids(s,['kb'])==set()
        new.status=FileStatus.PARSED; s.commit()
        assert inactive_file_ids(s,['kb'])=={'old'}
        from app.services.retrieval import RetrievalService
        service=RetrievalService.__new__(RetrievalService)
        results=service._enrich_results(s,[{'id':'old-chunk','kb_id':'kb'},{'id':'new-chunk','kb_id':'kb'}],['kb'])
        assert [r['id'] for r in results]==['new-chunk']
        assert s.get(FileDocument,'old').local_path=='old'
        assert inactive_file_ids(s,['another'])==set()
        # API deletion removes file-owned chunks before the file record.
        s.delete(s.get(DocumentChunk,'new-chunk')); s.flush()
        s.delete(new); s.commit()
        assert inactive_file_ids(s,['kb'])==set()
    engine.dispose()
