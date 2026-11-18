"""Offline checks for the bounded diagnostic runner; no provider/Chroma calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/check_hybrid_ranking_control.py'
spec = importlib.util.spec_from_file_location('ranking_control_under_test', SCRIPT)
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


def test_import_is_inert_in_fresh_process():
    code = '''import importlib.util,os,socket,sys
before=dict(os.environ)
def denied(*args,**kwargs):raise AssertionError('network forbidden')
socket.socket.connect=denied
spec=importlib.util.spec_from_file_location('control',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
assert dict(os.environ)==before
assert not any(k=='app' or k.startswith('app.') for k in sys.modules)
assert not any('hybrid_concurrency_probe' in k for k in sys.modules)
print('INERT_IMPORT_PASS')
'''
    result = subprocess.run([sys.executable, '-c', code, str(SCRIPT)],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'INERT_IMPORT_PASS'


def test_plan_deduplicates_historical_pairs_without_passing_gold(tmp_path):
    config = {'search_mode': 'hybrid', 'rerank_enabled': False, 'top_k': 10,
              'score_threshold': 0, 'vector_weight': .7, 'bm25_weight': .3, 'context_window': 1}
    cases = {'kb_ids': ['kb'], 'fixed_config': config, 'cases': [
        {'qid': 'q1', 'question': 'first question', 'evidence': [{'quote': 'PRIVATE_GOLD'}]},
        {'qid': 'q2', 'question': 'second question', 'gold_original': 'PRIVATE_GOLD'}]}
    cases_path = tmp_path / 'cases.json'
    cases_path.write_text(json.dumps(cases), encoding='utf8')
    pairs = tmp_path / 'pairs'; pairs.mkdir()
    for qid, repetition in [('q1', 0), ('q1', 1), ('q2', 0)]:
        question = next(x['question'] for x in cases['cases'] if x['qid'] == qid)
        for arm in ('serial', 'parallel'):
            record = {'id': f'{qid}-{repetition}-{arm}', 'qid': qid, 'repetition': repetition,
                'arm': arm, 'status': 'COMPLETED',
                'request': {'data': {**config, 'query': question}, 'kb_ids': ['kb']},
                'body': {'data': {'results': [{'chunkId': arm, 'content': arm}]}}}
            (pairs / (record['id'] + '.json')).write_text(json.dumps(record), encoding='utf8')
    plan = control.select_plan(cases_path, pairs)
    assert len(plan['historical_mismatched_pairs']) == 3 and len(plan['questions']) == 2
    assert 'PRIVATE_GOLD' not in json.dumps(plan)
    assert all(set(q) == {'qid', 'query'} for q in plan['questions'])


def test_capture_checks_actual_vector_hash_and_excludes_bodies():
    state = {'frozen_vector_sha256': control.fingerprint([.1, .2]),
             'chroma_pages': [], '_lock': threading.Lock()}
    class Collection:
        calls = 0
        def query(self, **kwargs):
            self.calls += 1
            return {'ids': [['a', 'b']], 'distances': [[.2, .4]],
                    'documents': [['PRIVATE_BODY', 'PRIVATE_BODY']]}
    actual = Collection()
    proxy = control.CaptureCollection(actual, 'kb', state, SimpleNamespace(check=lambda: None))
    result = proxy.query(query_embeddings=[[.1, .2]], n_results=2, include=['documents', 'distances'])
    assert result['documents'][0][0] == 'PRIVATE_BODY'
    page = state['chroma_pages'][0]
    assert page['distances'] == [.2, .4] and page['ids'] == ['a', 'b']
    assert page['ids_distances_sha256'] == control.fingerprint([('a', .2), ('b', .4)])
    assert 'PRIVATE_BODY' not in json.dumps(state['chroma_pages'])
    with pytest.raises(control.ControlStop, match='CHROMA_QUERY_VECTOR_CHANGED'):
        proxy.query(query_embeddings=[[.1, .3]], n_results=2)
    assert actual.calls == 1


@pytest.mark.parametrize('vector', [[True], [float('nan')], [float('inf')], ['.1'], [10**400], []])
def test_invalid_embedding_values_cannot_be_frozen(vector):
    with pytest.raises(control.ControlStop, match='INVALID_EMBEDDING_VECTOR'):
        control.checked_vector(vector, 1)


def test_comparison_distinguishes_route_input_from_final_difference():
    fields = ('chroma_pages_sha256', 'vector_rrf_input_sha256', 'lexical_rrf_input_sha256',
              'fused_sha256', 'final_sha256')
    a = {'qid': 'q', 'mode': 'S1', **dict.fromkeys(fields, 'same')}
    b = {**a, 'mode': 'P1', 'final_sha256': 'different'}
    row = control.summarize_searches([a, b])[0]
    assert row['vector_rrf_input_sha256'] and row['lexical_rrf_input_sha256']
    assert not row['final_sha256']


@pytest.mark.parametrize('execute', [False, True])
def test_real_sqlite_runner_is_read_only_with_controlled_chroma_and_model(tmp_path, monkeypatch, execute):
    import hashlib
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import CustomModel, DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase, ModelType
    from app.services.embedding import EmbeddingService
    from app.services.vector_store import VectorStoreService
    db_path = tmp_path / 'source.db'
    engine = create_engine('sqlite:///' + db_path.as_posix())
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(CustomModel(id='model', name='model', model_type=ModelType.EMBEDDING,
            base_url='http://embedding.invalid/v1', model_name='bge-test', api_key='PRIVATE_CREDENTIAL', context_length=4096))
        db.add(KnowledgeBase(id='kb', name='kb', embedding_model='model', vlm_model=''))
        db.add(FileDocument(id='file', kb_id='kb', name='file', local_path='fixture', size=0, active_build_id='build'))
        db.add(DocumentChunk(id='chunk', file_id='file', content='PRIVATE_DOCUMENT'))
        raw = json.dumps({'chunk_ids': ['chunk'], 'dimension': 3})
        db.add(FileIndexBuild(build_id='build', file_id='file', kb_id='kb', state='ACTIVE',
            generation=1, task_id='task', attempt_no=1, manifest=raw,
            manifest_hash=hashlib.sha256(raw.encode()).hexdigest(), chunk_count=1))
        db.commit()
    engine.dispose()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    output = tmp_path / 'output'; output.mkdir()
    for name in ('private-vectors', 'embedding', 'search'):
        (output / name).mkdir()
    from app.services import retrieval as actual_retrieval
    args = SimpleNamespace(backend_root=Path(actual_retrieval.__file__).resolve().parents[2], db=db_path,
        env_file=None, chroma_host='127.0.0.1', chroma_port=8001, chroma_workers=1, execute=execute)
    plan = {'scope': ['kb'], 'questions': [{'qid': 'q', 'query': 'public question'}],
        'historical_mismatched_pairs': [], 'cases_sha256': 'frozen',
        'config': {'search_mode': 'hybrid', 'top_k': 1, 'score_threshold': 0,
                   'vector_weight': .7, 'bm25_weight': .3}}
    model = AsyncMock(side_effect=[[.1, .2, .3], [.11, .2, .3]])
    monkeypatch.setattr(EmbeddingService, 'embed_query', model)
    class Collection:
        def query(self, **kwargs):
            return {'ids': [['chunk']], 'distances': [[2 * kwargs['query_embeddings'][0][0]]],
                    'documents': [['PRIVATE_DOCUMENT']], 'metadatas': [[{'build_id': 'build'}]]}
    class Client:
        def get_collection(self, **kwargs): return Collection()
        def get_max_batch_size(self): return 500
    def bootstrap(self, **kwargs):
        self.client = Client()
        return self.query(**kwargs)
    monkeypatch.setattr(VectorStoreService, '_query_with_bootstrap', bootstrap)
    monkeypatch.setattr(control.Resources, 'check', lambda self: {'mem_available': 10**10, 'disk_free': 10**11})
    with patch.dict('os.environ'):
        result = asyncio.run(control.execute(args, plan, output))
    assert result['status'] == ('COMPLETED' if execute else 'PREFLIGHT_ONLY'), result
    assert result['embedding_method_calls'] == (2 if execute else 0)
    assert model.await_count == (2 if execute else 0)
    assert result['embedding_http_sends'] == 0  # Explicit local boundary double.
    assert result['searches'] == (4 if execute else 0)
    if execute:
        assert len(result['comparisons']) == 6
        assert all(all(row[k] for k in ('chroma_pages_sha256', 'vector_rrf_input_sha256',
            'lexical_rrf_input_sha256', 'fused_sha256', 'final_sha256')) for row in result['comparisons'])
        control.write_new(output / 'summary.json', result)
        before_replay = control.artifact_hashes(output)
        prior = control.load_replay(output, plan)
        assert prior['changed_qids'] == ['q']
        replay_output = tmp_path / 'replay'; replay_output.mkdir()
        for name in ('private-vectors', 'embedding', 'search'):
            (replay_output / name).mkdir()
        args.replay_from = output
        model.side_effect = AssertionError('new provider calls forbidden during replay')
        with patch.dict('os.environ'):
            replay = asyncio.run(control.execute(args, plan, replay_output))
        assert replay['status'] == 'COMPLETED', replay
        assert replay['embedding_method_calls'] == 2 and replay['searches'] == 6
        assert replay['incremental_embedding_method_calls'] == replay['incremental_embedding_http_sends'] == 0
        assert replay['incremental_searches'] == 2 and model.await_count == 2
        assert replay['prior_artifacts_unchanged'] and control.artifact_hashes(output) == before_replay
        assert not list((replay_output / 'private-vectors').iterdir())
        comparison = replay['comparisons'][0]
        assert comparison['lexical_rrf_input_sha256']
        assert not comparison['chroma_pages_sha256'] and not comparison['vector_rrf_input_sha256']
        assert not comparison['final_sha256']
        retry_output = tmp_path / 'forbidden-retry'; retry_output.mkdir()
        with patch.dict('os.environ'):
            retry = asyncio.run(control.execute(args, plan, retry_output))
        assert retry['status'] == 'ABORTED' and retry['reason_code'] == 'REPLAY_ALREADY_CLAIMED_NO_AUTOMATIC_RETRY'
        assert model.await_count == 2 and control.artifact_hashes(output) == before_replay
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    manifest = (output / 'manifest.json').read_text(encoding='utf8')
    assert 'PRIVATE_CREDENTIAL' not in manifest and 'PRIVATE_DOCUMENT' not in manifest
