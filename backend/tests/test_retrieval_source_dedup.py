from app.services.retrieval import deduplicate_results


def result(chunk_id, file_id, content, kb_id='kb'):
    return {'id': chunk_id, 'file_id': file_id, 'kb_id': kb_id, 'content': content}


def test_near_identical_cross_document_evidence_is_retained():
    text = 'Quarterly progress report project title budget status schedule'
    results = [result('a', 'doc-a', text), result('b', 'doc-b', text)]
    assert [r['id'] for r in deduplicate_results(results)] == ['a', 'b']


def test_same_source_content_and_same_chunk_id_are_still_deduplicated():
    text = 'Quarterly progress report project title budget status schedule'
    results = [result('a', 'doc-a', text), result('b', 'doc-a', text), result('a', 'doc-b', text)]
    assert [r['id'] for r in deduplicate_results(results)] == ['a']


def test_unknown_source_and_distinct_kb_are_not_deduplicated_by_content():
    text = 'Quarterly progress report project title budget status schedule'
    results = [result('a', 'doc-a', text), result('b', None, text), result('c', 'doc-a', text, 'other')]
    assert [r['id'] for r in deduplicate_results(results)] == ['a', 'b', 'c']
