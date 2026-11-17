import importlib.util
from pathlib import Path


def test_final_sse_citation_update_replaces_previous_list():
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'run_p1_http_regression.py'
    spec = importlib.util.spec_from_file_location('p1_regression_runner', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    original = [{'chunk_id': 'a'}, {'chunk_id': 'b'}]
    updated = [{'chunk_id': 'a', 'location': 'page 10; cover page 1'}]
    assert runner.final_citations([('rag_result', {'citations': original}),
                                   ('rag_result', {'citations': updated}),
                                   ('done', {})]) == updated
    assert runner.final_citations([('done', {})]) == []
    assert runner.final_citations([('rag_result', {'citations': original}),
                                   ('rag_result', {'citations': []})]) == []


def test_source_expansion_is_not_counted_as_indexed_quote(tmp_path):
    import sqlite3
    import hashlib
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'run_p1_http_regression.py'
    spec = importlib.util.spec_from_file_location('p1_runner_source_check', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'%PDF-1.7 source')
    db = sqlite3.connect(':memory:')
    db.execute('create table file_documents(id,kb_id,local_path)')
    db.execute('create table document_chunks(id,file_id,content)')
    db.execute('insert into file_documents values (?,?,?)', ('f','kb',str(source)))
    db.execute('insert into document_chunks values (?,?,?)', ('parent','f','quote'))
    citation = {'source_type':'source_native_table_expansion','parent_chunk_id':'parent',
                'fileId':'f','source_metadata':{'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}}
    result = runner.citation_check(db,citation,'kb')
    assert result['valid_scope_and_quote'] is None
    assert result['valid_scope_and_source_hash'] is True
    assert runner.citation_check(db,citation,'other')['valid_scope_and_source_hash'] is False
    source.write_bytes(b'changed')
    assert runner.citation_check(db,citation,'kb')['valid_scope_and_source_hash'] is False


def test_metrics_accept_normalized_source_route_and_reject_private_labels():
    import pytest
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'run_p1_http_regression.py'
    spec = importlib.util.spec_from_file_location('p1_metrics_capture', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    line = 'rag_http_requests_total{method="GET",mode="http",outcome="COMPLETED",route="/api/v1/files/{file_id}/source",status_code="200"} 2.0'
    assert runner.sanitized_metrics('# HELP ignored\n' + line) == line + '\n'
    for bad in (line.replace('{file_id}', 'private-document-id'),
                line.replace('mode="http"', 'trace_id="sensitive"'),
                line.replace('mode="http"', 'mode="http",mode="chat"')):
        with pytest.raises(RuntimeError):
            runner.sanitized_metrics(bad)
