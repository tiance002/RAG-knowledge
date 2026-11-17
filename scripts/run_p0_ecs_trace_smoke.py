"""Bounded P0 trace smoke. Credentials are read only from stdin on --execute."""
import argparse
import asyncio
from array import array
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, help='dataset200 experiment root')
    ap.add_argument('--backend', required=True, help='staged backend containing app/')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--preflight', action='store_true')
    group.add_argument('--execute', action='store_true')
    args = ap.parse_args()
    base, backend = Path(args.root).resolve(), Path(args.backend).resolve()
    prior, dataset = base / 'runtime200-r4', base / 'dataset200'
    freeze = json.loads((dataset / 'freeze.json').read_bytes())
    for name, digest in freeze['files'].items():
        if sha((dataset / name).read_bytes()) != digest:
            raise ValueError('Frozen dataset hash mismatch: ' + name)
    registry = {r['qid']: r for r in rows(dataset / 'registry200.jsonl')}
    gold = rows(dataset / 'gold100.jsonl')
    cohorts = {'hotpotqa': [], 'longbench-zh': [], 'pdf': []}
    for case in gold:
        profile = registry[case['qid']]['profile']
        key = 'pdf' if profile.startswith('pdf-') else profile
        if key in cohorts:
            cohorts[key].append((case, profile))
    selected = []
    for key, count in [('hotpotqa', 3), ('longbench-zh', 3), ('pdf', 4)]:
        candidates = sorted(cohorts[key], key=lambda pair: pair[0]['qid'])
        if len(candidates) < count:
            raise ValueError('Insufficient frozen gold cases: ' + key)
        selected.extend(candidates[:count])
    assembly = json.loads((prior / 'assembled.json').read_bytes())
    if assembly['status'] != 'ASSEMBLED' or sha((prior / 'profiles.jsonl').read_bytes()) != assembly['profiles_sha256']:
        raise ValueError('Prior assembly integrity mismatch')
    control = rows(prior / 'control-sql.jsonl')
    old_kbs = {r['value']['name'].removeprefix('prod-'): r['value']['id'] for r in control if r['kind'] == 'kb'}
    def kb_for(profile):
        return old_kbs[profile] if profile in old_kbs else 'v200-' + profile + ('-after' if profile.startswith('pdf-') else '')
    for path in (prior / 'isolated.db', prior / 'chroma', prior / 'vector-cache.db', backend / 'app', Path('/opt/amrag/ids.json'), Path('/opt/amrag/backend/local.db')):
        if not path.exists():
            raise FileNotFoundError(path)
    db = sqlite3.connect('file:' + str(prior / 'vector-cache.db') + '?mode=ro', uri=True)
    cached = []
    for case, profile in selected:
        row = db.execute('select dimensions, data from vectors where key=?', (sha(case['question'].encode()),)).fetchone()
        if row is None:
            raise ValueError('Missing query vector for ' + case['qid'] + '; no calls made')
        vector = array('d'); vector.frombytes(row[1])
        if len(vector) != row[0]:
            raise ValueError('Invalid vector cache')
        cached.append(vector.tolist())
    db.close()
    plan = {'experiment_version': 'p0-live10-v1', 'dataset_freeze_sha256': sha((dataset / 'freeze.json').read_bytes()),
            'runner_sha256': sha(Path(__file__).read_bytes()),
            'profiles_sha256': assembly['profiles_sha256'],
            'control_sha256': sha((prior / 'control-sql.jsonl').read_bytes()),
            'vector_cache_sha256': sha((prior / 'vector-cache.db').read_bytes()), 'inputs': [{'qid': c['qid'], 'profile': p,
            'kb_id': kb_for(p), 'question_sha256': sha(c['question'].encode())} for c, p in selected],
            'bounds': {'questions': 10, 'embedding_calls': 0, 'rerank_http_calls': 20, 'generation_http_calls': 10},
            'parameters': {'mode': 'hybrid', 'top_k': 50, 'vector_weight': .7, 'bm25_weight': .3, 'max_output_tokens': 4096},
            'cost': 'UNKNOWN'}
    if args.preflight:
        print(json.dumps({'status': 'PREFLIGHT_ZERO_CALLS', **plan})); return
    credentials = json.load(sys.stdin)
    allowed = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')
    if not all(credentials.get(k) for k in allowed) or credentials['LANGFUSE_BASE_URL'].rstrip('/') != 'https://us.cloud.langfuse.com':
        raise ValueError('Missing US Langfuse credentials')
    out = base / ('p0-live10-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    out.mkdir(mode=0o700)
    os.environ.update({k: str(credentials[k]) for k in allowed})
    os.environ.update(RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true', RAG_SNAPSHOTS_ENABLED='false',
                      RAG_OBSERVATION_LOG=str(out / 'events.jsonl'), ENV='test', DATABASE_URL='sqlite:///' + str(out / 'isolated.db'),
                      CHROMA_DB_DIR=str(out / 'chroma'), IMAGE_DIR=str(out / 'images'), UPLOAD_DIR=str(out / 'uploads'), ANONYMIZED_TELEMETRY='False')
    del credentials
    source = sqlite3.connect('file:' + str(prior / 'isolated.db') + '?mode=ro', uri=True)
    dest = sqlite3.connect(out / 'isolated.db'); source.backup(dest); dest.close(); source.close()
    shutil.copytree(prior / 'chroma', out / 'chroma')
    (out / 'manifest.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chdir(out); sys.path.insert(0, str(backend))
    import httpx
    from openai import AsyncOpenAI
    from tenacity import stop_after_attempt
    from sqlmodel import Session
    from app.core.database import engine
    from app.core.config import get_settings
    from app.models import CustomModel
    from app.services.observability import span, event
    from app.services.retrieval import RetrievalService
    from app.services.rerank import RerankService, batch_rerank, expand_context
    from app.services.llm import LLMService
    ids = json.loads(Path('/opt/amrag/ids.json').read_bytes())['models']
    conn = sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro', uri=True); conn.row_factory = sqlite3.Row
    models = {}
    for role in ('llm', 'rerank'):
        record = conn.execute('select * from custom_models where id=?', (ids[role],)).fetchone()
        if record is None: raise ValueError('Missing model ' + role)
        models[role] = CustomModel(**dict(record))
    conn.close()
    old_models = json.loads((prior / "state.json").read_bytes())["models"]
    for role, model in models.items():
        expected = {"id": model.id, "model": model.model_name, "endpoint_sha256": sha(model.base_url.encode())}
        if old_models[role] != expected: raise ValueError("Model identity drift: " + role)
    if get_settings().LLM_ANSWER_MAX_TOKENS != 4096: raise ValueError('Expected 4096 answer limit')
    class ModelSession:
        def __init__(self, session): self.session = session
        def get(self, kind, key):
            if kind is CustomModel: return next((m for m in models.values() if m.id == key), None)
            return self.session.get(kind, key)
        def __getattr__(self, key): return getattr(self.session, key)
    class CappedRerank(RerankService):
        calls = 0
        denied = False
        def _rerank_via_chat(self, *args):
            raise RuntimeError('Chat fallback disabled to enforce request bound')
        def _rerank_via_api(self, *args):
            if self.calls >= 20:
                type(self).denied = True
                raise RuntimeError('Rerank call bound reached')
            type(self).calls += 1
            return super()._rerank_via_api(*args)
    for (case, profile), vector in zip(selected, cached):
        qid = case['qid']
        with span('p0.smoke.question', qid=qid, experiment_version='p0-live10-v1', kb_id=kb_for(profile)) as trace:
            event('p0.smoke.cost', status='UNKNOWN')
            try:
                with Session(engine) as raw:
                    session = ModelSession(raw); service = RetrievalService()
                    async def query_embedding(*unused): return vector
                    service._embed_query_for_kb = query_embedding
                    found, metrics = asyncio.run(service.search(session, case['question'], [kb_for(profile)], search_mode='hybrid',
                        top_k=50, score_threshold=0, vector_weight=.7, bm25_weight=.3, use_cache=False, log_retrieval=False))
                    ranked = batch_rerank(case['question'], found, CappedRerank(session), models['rerank'], top_k=20, score_threshold=0)
                    if CappedRerank.denied or not ranked: raise RuntimeError('Rerank failed or call bound reached')
                    expanded = expand_context(ranked, session, window_size=1)
                    context = '\n\n'.join(f'[{i}] 来源: {r.get("file_name", "未知")}\n{r.get("content", "")}' for i, r in enumerate(expanded, 1))
                prompt = '你是一个智能问答助手，请根据以下参考资料回答用户的问题。\n参考资料：\n' + context + '\n要求：基于参考资料回答；资料不足时如实说明。使用中文回答。'
                llm = models['llm']; service = LLMService(base_url=llm.base_url, api_key=llm.api_key, model=llm.model_name)
                service._client = AsyncOpenAI(base_url=service.base_url, api_key=service.api_key, max_retries=0, timeout=120, http_client=httpx.AsyncClient(trust_env=False))
                async def generate():
                    try:
                        return await service.generate.retry_with(stop=stop_after_attempt(1))(service, [{'role': 'user', 'content': case['question']}], system_prompt=prompt)
                    finally:
                        await service._client.close()
                answer = asyncio.run(generate())
                result = {'qid': qid, 'trace_id': trace['trace_id'] if trace else None, 'status': 'COMPLETED', 'answer_sha256': sha((answer or '').encode()), 'answer_chars': len(answer or ''), 'context_sha256': sha(context.encode()), 'cost': 'UNKNOWN', 'metrics': metrics}
            except Exception as exc:
                result = {'qid': qid, 'status': 'UNKNOWN_OR_FAILED', 'error_type': type(exc).__name__, 'cost': 'UNKNOWN'}
            with (out / 'results.jsonl').open('a', encoding='utf-8') as handle: handle.write(json.dumps(result, ensure_ascii=False) + '\n')
            if result['status'] != 'COMPLETED':
                print(json.dumps({'status': 'STOPPED', 'qid': qid, 'output': str(out)})); return 1
    from app.services.observability import _sdk
    if _sdk():
        _sdk().flush()
    print(json.dumps({'status': 'FINISHED', 'output': str(out), 'rerank_calls': CappedRerank.calls, 'cost': 'UNKNOWN'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
