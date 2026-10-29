"""Serve an isolated FastAPI metrics smoke app with no provider calls.

The controlled retrieval failure is injected only in this process. The two
knowledge bases exist solely in an in-memory SQLite database.
"""
import os
from pathlib import Path

os.environ.update(
    ENV='test', DATABASE_URL='sqlite:///:memory:', RAG_PROMETHEUS_ENABLED='true',
    RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false',
    LLM_API_KEY='disabled', ANONYMIZED_TELEMETRY='False',
    CHROMA_DB_DIR='./smoke-chroma', IMAGE_DIR='./smoke-images', UPLOAD_DIR='./smoke-uploads',
)

from sqlmodel import Session
from app.core.database import engine, init_db
from app.models import FileDocument, FileStatus, KnowledgeBase
from app.services.chat import ChatService
from app.services.llm import LLMService


def forbid_provider(self):
    raise RuntimeError('provider calls disabled in isolated smoke')


LLMService._get_client = forbid_provider

init_db()
with Session(engine) as session:
    session.add(KnowledgeBase(id='smoke-empty', name='smoke-empty', embedding_model='unused', vlm_model='unused'))
    session.add(KnowledgeBase(id='smoke-fault', name='smoke-fault', embedding_model='unused', vlm_model='unused'))
    session.add(FileDocument(id='smoke-file', kb_id='smoke-fault', name='synthetic.pdf',
                             local_path='synthetic.pdf', size=1, status=FileStatus.READY))
    session.commit()


def init_without_provider(self):
    self.llm_service = None


async def controlled_retrieval_fault(self, *args, **kwargs):
    raise RuntimeError('controlled smoke fault')


ChatService.__init__ = init_without_provider
ChatService._retrieve_with_new_service = controlled_retrieval_fault

from app.main import app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=18200, log_level='warning')
