"""Isolated real upload/parse/index smoke with exact frozen cache vector; no model calls."""
import argparse, asyncio, hashlib, io, json, os, pathlib, shutil, sqlite3, sys, time, uuid
from array import array


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--backend', required=True)
    a = p.parse_args()
    base, backend = pathlib.Path(a.root).resolve(), pathlib.Path(a.backend).resolve()
    prior = base / 'runtime200-r4'
    cache = sqlite3.connect('file:' + str(prior / 'vector-cache.db') + '?mode=ro', uri=True)
    text = vector = None
    frozen_chunks = [json.loads(line)['value'] for line in (prior / 'control-sql.jsonl').read_text(encoding='utf-8').splitlines() if json.loads(line)['kind'] == 'chunk']
    frozen_chunks.extend(chunk for line in (prior / 'profiles.jsonl').read_text(encoding='utf-8').splitlines() for chunk in json.loads(line)['chunks'] if chunk.get('content_type') == 'text')
    for chunk in frozen_chunks:
        value = chunk.get('content') or ''
        if not value or value != value.strip(): continue
        key = hashlib.sha256(value.encode()).hexdigest()
        found = cache.execute('select dimensions, data from vectors where key=?', (key,)).fetchone()
        if found:
            candidate = array('d'); candidate.frombytes(found[1])
            if len(candidate) == found[0]: text, vector = value, candidate.tolist(); break
    cache.close()
    if text is None: raise RuntimeError('No exact cached control text')
    credentials = json.load(sys.stdin)
    keys = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')
    if not all(credentials.get(k) for k in keys) or credentials['LANGFUSE_BASE_URL'].rstrip('/') != 'https://us.cloud.langfuse.com':
        raise ValueError('US Langfuse credentials required')
    out = base / ('p0-ingest-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]); out.mkdir(mode=0o700)
    os.environ.update({k: str(credentials[k]) for k in keys}); del credentials
    os.environ.update(ENV='test', DATABASE_URL='sqlite:///' + str(out / 'isolated.db'), CHROMA_DB_DIR=str(out / 'chroma'),
                      UPLOAD_DIR=str(out / 'uploads'), IMAGE_DIR=str(out / 'images'), ANONYMIZED_TELEMETRY='False',
                      RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true', RAG_SNAPSHOTS_ENABLED='false', RAG_OBSERVATION_LOG=str(out / 'events.jsonl'))
    src = sqlite3.connect('file:' + str(prior / 'isolated.db') + '?mode=ro', uri=True)
    dst = sqlite3.connect(out / 'isolated.db'); src.backup(dst); dst.close(); src.close()
    os.chdir(out); sys.path.insert(0, str(backend))
    from fastapi import BackgroundTasks, UploadFile
    from sqlmodel import Session, select
    from app.core.database import engine
    from app.models import KnowledgeBase, FileDocument, DocumentChunk
    from app.api.files import upload_file
    from app.tasks.parse_tasks import process_file_parsing
    from app.services.embedding import EmbeddingService
    from app.services.vector_store import VectorStoreService
    from app.services import observability as obs
    kb_id = 'p0-ingest-' + uuid.uuid4().hex
    with Session(engine) as session:
        session.add(KnowledgeBase(id=kb_id, name=kb_id, embedding_model='sys_embedding', vlm_model='')); session.commit()
    count = 0
    async def exact_cache(self, texts, model_id=None):
        nonlocal count
        if count or texts != [text]: raise RuntimeError('Cache adapter refuses non-exact embedding input')
        count += 1
        obs.event('ingest.embedding_cache', status='CACHE', content_sha256=hashlib.sha256(text.encode()).hexdigest(), dimensions=len(vector))
        return [vector]
    EmbeddingService.embed_documents = exact_cache
    result = {'status': 'FAILED', 'output': str(out), 'model_calls': 0}
    try:
        with obs.span('p0.ingest.smoke', experiment_version='p0-ingest-v1', kb_id=kb_id) as root:
            result['trace_id'] = root['trace_id'] if root else None
            with Session(engine) as session:
                uploaded = asyncio.run(upload_file(kb_id=kb_id, background_tasks=BackgroundTasks(), file=UploadFile(filename='cached.txt', file=io.BytesIO(text.encode())), session=session))
                file_id = uploaded.data.file_id
            result.update(file_id=file_id, upload_status='PASS')
            process_file_parsing(file_id, chunk_mode='no_split', auto_vectorize=True)
            with Session(engine) as session:
                file_doc = session.get(FileDocument, file_id)
                chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == file_id)).all()
                if len(chunks) != 1 or chunks[0].content != text or count != 1: raise RuntimeError('Parsed chunk/cache mismatch')
                result['parse_status'] = str(file_doc.status)
                cid = str(chunks[0].id)
            service = VectorStoreService()
            collection = service.client.get_collection(service._get_collection_name(kb_id))
            if cid not in collection.get(ids=[cid], include=[])['ids']: raise RuntimeError('Indexed vector missing')
            result.update(status='PASS', index_status='PASS', chunk_id=cid, embedding_status='CACHE')
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    finally:
        client = obs._sdk()
        if client:
            try: client.flush(); result['flush_status'] = 'CALLED'
            except Exception as exc: result['flush_status'] = type(exc).__name__
        else: result['flush_status'] = 'CLIENT_UNAVAILABLE'
        (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1

if __name__ == '__main__': sys.exit(main())
