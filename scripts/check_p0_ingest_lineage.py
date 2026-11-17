"""Read-only retrieval against the isolated ingest sample, exact cached embedding."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from array import array

parser = argparse.ArgumentParser()
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--backend', type=Path, required=True)
parser.add_argument('--prior', type=Path, required=True)
args = parser.parse_args()
credentials = json.load(sys.stdin)
for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL'):
    os.environ[key] = credentials[key]
del credentials
out = args.runtime.resolve()
os.environ.update(RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true',
                  RAG_OBSERVATION_LOG=str(out/'retrieval-events.jsonl'),
                  DATABASE_URL='sqlite:///'+str(out/'isolated.db'), CHROMA_DB_DIR=str(out/'chroma'))
sys.path.insert(0, str(args.backend.resolve()))
os.chdir(out)
from sqlmodel import Session
from app.core.database import engine
from app.models import DocumentChunk, FileDocument
from app.services.retrieval import RetrievalService
from app.services.observability import span, _sdk
result = json.loads((out/'result.json').read_text())
with Session(engine) as session:
    chunk = session.get(DocumentChunk, result['chunk_id'])
    file = session.get(FileDocument, result['file_id'])
    query = chunk.content
    cache = sqlite3.connect('file:'+str(args.prior.resolve()/'vector-cache.db')+'?mode=ro', uri=True)
    row = cache.execute('select dimensions,data from vectors where key=?', (hashlib.sha256(query.encode()).hexdigest(),)).fetchone()
    cache.close()
    assert row is not None
    vector = array('d'); vector.frombytes(row[1]); assert len(vector) == row[0]
    service = RetrievalService()
    async def cached(*unused): return vector.tolist()
    service._embed_query_for_kb = cached
    with span('p0.ingest.qa_retrieval', file_id=file.id, kb_id=file.kb_id) as root:
        found, _ = asyncio.run(service.search(session, query, [file.kb_id], search_mode='vector', top_k=1,
                                               use_cache=False, log_retrieval=False))
        assert [x['id'] for x in found] == [chunk.id]
        audit = {'status': 'PASS', 'trace_id': root['trace_id'], 'ingest_trace_id': result['trace_id'],
                 'file_id': file.id, 'chunk_id': chunk.id, 'model_calls': 0}
if _sdk(): _sdk().flush()
(out/'lineage-check.json').write_text(json.dumps(audit,indent=2),encoding='utf-8')
print(json.dumps(audit))
