"""Small real-replacement contracts for the existing local benchmark runner."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bm25_write_benchmark', ROOT / 'scripts/benchmark_bm25_write_payload.py')
benchmark = importlib.util.module_from_spec(spec); spec.loader.exec_module(benchmark)


def test_replacement_really_removes_old_target_and_retains_other_file(tmp_path):
    raw = (ROOT / 'backend/app/services/bm25.py').read_bytes()
    module = benchmark.load_source('contract', raw, tmp_path)
    run = benchmark.run_one('contract', module, module.BM25Index,
                            benchmark.fixture(parents=2, children_per_parent=3, vocabulary=20), tmp_path, 'small')
    assert set(run['phases']) == {'prepare','begin','sql_replace','finish','flush','commit'}
    assert run['phases']['begin']['sql_delete_calls'] >= 2
    assert run['phases']['sql_replace']['sql_delete_calls'] == 1
    assert run['semantic']['total_docs'] == 7
    assert (run['semantic']['parent_count'], run['semantic']['child_count']) == (2, 6)
    assert run['parents_sql_only'] and run['background_unchanged']
    assert run['semantic']['queries']['prior'] == []
    assert run['semantic']['queries']['fresh']
    assert run['sqlite_policy']['journal_mode'] == 'wal'
    assert run['sqlite_policy']['wal_autocheckpoint'] == 0
    assert run['begin_query_plans']


def test_meter_does_not_patch_global_json_and_equivalence_rejects_changes():
    original = json.dumps
    meter = benchmark.Measurement(); proxy = benchmark.JsonMeter(meter)
    meter.active = 'prepare'; assert proxy.loads(proxy.dumps({'word': 3})) == {'word': 3}
    assert json.dumps is original and meter.phases['prepare']['json_encode_calls'] == 1
    sample = {'full_logical_sha256':'same','total_docs':1,'chunk_index_count':1,'posting_count':1,
              'parent_count':0,'child_count':1,'background_sha256':'bg','avg_doc_length':3.,'queries':{'word':[('c',1.)]}}
    benchmark.assert_equal(sample, dict(sample))
    changed = {**sample, 'queries': {'word': [('other',1.)]}}
    with pytest.raises(AssertionError): benchmark.assert_equal(sample, changed)


def test_posting_batch_measurement_records_largest_dispatch_not_total():
    meter = benchmark.Measurement()
    meter.active = 'finish'
    statement = 'INSERT INTO bm25_postings (kb_id, term, index_id, tf) VALUES (?, ?, ?, ?)'
    for count in (3, 2):
        meter.sql(None, None, statement, [('kb', 'word', str(i), 1) for i in range(count)], None, True)
    assert meter.phases['finish']['posting_insert_max_rows'] == 3
    assert meter.phases['finish']['sql_parameter_sets'] == 5
    assert meter.phases['finish']['sql_insert_calls'] == 2
