"""Controlled F2 receipts use synthetic inputs and the approved formal product."""
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rsi_quality_execute as execute
import rsi_quality_registry as registry
from rsi_candidates import canonical, sha
from rsi_quality_controlled_worker import verify_approved_source
from rsi_quality_loop import execute_controlled_loop


def synthetic_inputs(directory):
    source = directory / 'synthetic.txt'
    chunks = [{'id': f'chunk-{index}', 'source_id': 'synthetic',
               'content': f'Alpha protocol step {index}: validate input and save output.'}
              for index in range(1, 4)]
    source.write_text('\n'.join(row['content'] for row in chunks), encoding='utf-8')
    corpus = directory / 'corpus.json'
    corpus.write_bytes(canonical({'chunks': chunks}))
    inputs = {'dataset_version': 'f2-synthetic-only-v1',
              'corpus_manifest': {'path': str(corpus), 'sha256': sha(corpus.read_bytes())},
              'chunks': [{'id': row['id'], 'sha256': sha(row['content'].encode())} for row in chunks],
              'sources': [{'id': 'synthetic', 'title': 'synthetic.txt', 'path': str(source),
                           'sha256': sha(source.read_bytes()), 'authorized': True}],
              'cases': [{'qid': 'synthetic-alpha', 'question': 'Alpha protocol steps?',
                         'scope': {'chunk_ids': [row['id'] for row in chunks]}}]}
    path = directory / 'inputs.json'
    path.write_bytes(canonical(inputs))
    return path


def test_controlled_loop_binds_actual_prompt_and_parameter_to_one_parent(tmp_path):
    product = Path(os.environ.get('RSI_F2_PRODUCT_ROOT', 'D:/RAG knowledge'))
    addendum = 'Keep each source number beside the claim.'
    out = tmp_path / 'control-loop'
    result = execute_controlled_loop(out, product_root=product,
                                     source_fingerprint=execute.source_fingerprint(product),
                                     prompt_addendum=addendum)
    assert result['status'] == 'OFFLINE_CONTRACT_COMPLETE'
    assert result['actual_external_calls'] == 0
    assert [c['scope'] for c in result['candidates']] == ['prompt', 'parameter']
    assert all(c['parent_id'] == result['baseline']['candidate_id'] and
               c['A']['results'] == result['baseline']['results'] for c in result['candidates'])

    rows, messages = {}, {}
    for arm in ('baseline', 'prompt', 'parameter'):
        archived = json.loads((out / arm / 'worker.json').read_bytes())
        row, = archived['records']
        rows[arm] = row
        raw_record = Path(row['raw_record'])
        raw = json.loads(raw_record.read_bytes())
        request = json.loads((raw_record.parent / raw['final_request']['request_file']).read_bytes())
        messages[arm] = request['actual_request']['messages']
        assert sha(canonical(messages[arm])) == row['final_prompt_messages_sha256']
    assert all(addendum not in str(message) for message in messages['baseline'])
    assert any(message['role'] == 'system' and addendum in str(message['content'])
               for message in messages['prompt'])
    assert [m for m in messages['baseline'] if m['role'] == 'user'] == [
        m for m in messages['prompt'] if m['role'] == 'user']
    assert rows['baseline']['effective_config_sha256'] == rows['prompt']['effective_config_sha256']
    assert rows['baseline']['retrieval_observations'] == rows['prompt']['retrieval_observations']
    assert [rows[arm]['observed_top_k'] for arm in ('baseline', 'prompt', 'parameter')] == [10, 10, 1]
    assert [len(rows[arm]['retrieval_observations'][0]['returned_ids'])
            for arm in ('baseline', 'prompt', 'parameter')] == [3, 3, 1]


def artifact(top_k):
    return {'prompt_addendum': '', 'memory_addendum': '',
            'workflow': {'mode': 'chat', 'use_rewrite': False},
            'retrieval': {'search_mode': 'fulltext', 'top_k': top_k, 'context_window': 0,
                          'rerank_enabled': False, 'score_threshold': 0,
                          'vector_weight': 0.7, 'bm25_weight': 0.3}}


def test_controlled_real_product_top_k_and_prompt_receipts(tmp_path):
    product = Path(os.environ.get('RSI_F2_PRODUCT_ROOT', 'D:/RAG knowledge'))
    assert product.is_dir(), 'Exact approved formal source checkout required'
    # Fail rather than skip if the available source has drifted.
    before = verify_approved_source(product)
    fingerprint = execute.source_fingerprint(product)
    inputs = synthetic_inputs(tmp_path)
    results = []
    for top_k in (1, 3):
        result = execute.execute_controlled_artifact(artifact(top_k), inputs,
            tmp_path / f'k-{top_k}', product_root=product, source_fingerprint=fingerprint)
        assert result['exit_code'] == 0
        assert result['status'] == 'RETURNED'
        assert result['actual_external_calls'] == result['provider_calls'] == 0
        assert result['provider_stub_calls'] == 1
        assert len(result['records']) == 1
        row = result['records'][0]
        assert row['evidence_mode'] == 'OFFLINE_PROVIDER_STUB'
        assert row['qid'] == 'synthetic-alpha' and row['repeat_id'] == 1
        assert row['status'] == 'RETURNED'
        assert row['observed_top_k'] == top_k
        assert row['input_tokens'] is row['output_tokens'] is None
        assert len(row['retrieval_observations'][0]['returned_ids']) == top_k
        raw = json.loads(Path(row['raw_record']).read_text(encoding='utf-8'))
        assert raw['usage'] is None
        assert raw['final_context_status'] == 'VERIFIED_IN_ACTUAL_REQUEST'
        assert raw['done']
        final = Path(row['raw_record']).parent / raw['final_request']['request_file']
        request = json.loads(final.read_text(encoding='utf-8'))
        assert sha(request['actual_request_utf8'].encode()) == row['actual_request_sha256']
        assert sha(canonical(request['actual_request']['messages'])) == row['final_prompt_messages_sha256']
        assert not list((tmp_path / f'k-{top_k}').rglob('*.db'))
        results.append(row)
    assert results[0]['final_prompt_messages_sha256'] != results[1]['final_prompt_messages_sha256']
    assert results[0]['effective_config_sha256'] != results[1]['effective_config_sha256']
    assert verify_approved_source(product) == before
