import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from app.services.evaluation import evaluate_evidence, evaluate_ranking, evidence_transitions, execution_metrics, review_metrics

spec = importlib.util.spec_from_file_location("publish", Path(__file__).resolve().parents[2] / "scripts/publish_p0_scores.py")
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


def test_grain_completeness_and_rank():
    assert evaluate_evidence(["p1"], ["p1"], gold_grain="page", observed_grain="chunk", gold_complete=True)["coverage"] is None
    assert evaluate_evidence(["a", "b"], ["a"], gold_grain="chunk", observed_grain="chunk", gold_complete=True) == {"state": "PARTIAL", "coverage": .5}
    assert evaluate_evidence(["a"], [], gold_grain="chunk", observed_grain="chunk", gold_complete=False)["state"] == "UNKNOWN"
    args = dict(gold_grain="chunk", observed_grain="chunk", k=2)
    assert evaluate_ranking(["a"], ["x"], gold_complete=False, **args)["hit_at_k"] is None
    assert evaluate_ranking(["a"], ["x", "a"], gold_complete=True, **args)["mrr_at_k"] == .5
    assert evaluate_ranking(["a"], ["a"], gold_complete=True, **args)["ndcg_at_k"] is None
    assert evaluate_ranking(["a"], ["a"], gold_complete=True, grades={"a": 2}, **args)["ndcg_at_k"] == 1
    assert evaluate_ranking([], ["x"], gold_complete=True, **args)["recall_at_k"] is None


def test_transitions_and_execution_unknown():
    stages = [{"stage": "raw", "grain": "chunk", "units": ["a"]}, {"stage": "context", "grain": "chunk", "units": ["b"]}]
    assert evidence_transitions(["a", "b"], stages, gold_grain="chunk", gold_complete=True)["raw_to_context"] == {"coverage_delta": 0, "retained": [], "recovered": ["b"], "lost": ["a"]}
    assert execution_metrics()["eval.answer_empty"] is None
    assert execution_metrics(final_answer="", finish_reason="length")["eval.answer_truncated"] is True


def test_review_and_readback():
    review = {"reviewer_type": "AI_ASSISTED", "rubric_version": "v1", "answer_sha256": "a" * 64, "evidence_scope": "actual", "core_correctness": "PARTIAL"}
    assert "eval.core_pass" not in review_metrics(review)
    record = dict(review, trace_id="trace", evaluation_source="review", metrics=review_metrics(review))
    first = publish.e11_score_payloads([record])
    assert first == publish.e11_score_payloads([record])
    p = first[0]
    row = NS(id=p['score_id'], name=p['name'], value=p['value'], data_type=p['data_type'], subject=NS(kind='trace', id='trace'), metadata=p['metadata'], source='API')
    assert publish.verify_score_readback(first, [row]) == []
    row.value = 'FAIL'
    assert (p['score_id'], 'value') in publish.verify_score_readback(first, [row])
    with pytest.raises(ValueError): publish.e11_score_payloads([record, record])
    with pytest.raises(ValueError): review_metrics({"core_correctness": "PASS"})


def test_summary_separates_unknown_and_reviewed_denominators():
    from app.services.evaluation import summarize_attempts
    result = summarize_attempts([{"sent": True, "returned": True, "reviewed": True, "core_correctness": "PASS"}, {"execution_status": "NOT_RUN"}])
    assert result["n_planned"] == 2 and result["n_unknown"] == 1 and result["n_not_run"] == 1
    assert result["confirmed_pass_per_planned"] == .5 and result["strict_pass_per_judged"] == 1


def test_preflight_checks_trace_root_answer_and_observation():
    import hashlib
    answer = "observed final answer"
    sha = hashlib.sha256(answer.encode()).hexdigest()
    payload = {"score_id": "score", "trace_id": "trace", "observation_id": "child", "metadata": {"answer_sha256": sha}}
    root = NS(id="root", trace_id="trace", is_root_observation=True, output=answer, metadata={})
    child = NS(id="child", trace_id="trace", is_root_observation=False)
    class Client:
        api = NS(observations=NS(get_many=lambda **kwargs: NS(data=[root, child], meta=NS(cursor=None))))
    assert publish.preflight_subjects(Client(), [payload]) == []
    child.trace_id = 'another'
    assert ('score', 'observation_not_in_trace') in publish.preflight_subjects(Client(), [payload])
    root.output = 'different'
    assert ('score', 'answer_link_not_confirmed') in publish.preflight_subjects(Client(), [payload])
    root.metadata = {'final_answer_sha256': sha}
    assert ('score', 'answer_link_not_confirmed') not in publish.preflight_subjects(Client(), [payload])


def test_e1_import_validates_bytes_and_answer(tmp_path):
    import hashlib
    path = tmp_path / 'var' / 'e1-20261029' / 'response.sse'
    path.parent.mkdir(parents=True)
    path.write_text('event: answer_chunk\ndata: {"content":"hello"}\n\nevent: done\ndata: {}\n\n', encoding='utf8')
    spec = importlib.util.spec_from_file_location('e1_import', Path(__file__).resolve().parents[2] / 'scripts/import_e1_reviews.py')
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    row = {'qid':'q', 'sse':str(path.relative_to(tmp_path)), 'sse_sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'human_core':'PASS', 'result_case':{'trace_ids':['trace']}}
    result = adapter.import_row(row, tmp_path)
    assert result['answer_sha256'] == hashlib.sha256(b'hello').hexdigest()
    assert result['historical_rubric'] == 'NOT_CAPTURED_AS_VERSION' and result['reviewer_type'] == 'USER'
    assert result['metrics']['eval.faithfulness'] == 'UNKNOWN'
    path.write_text('changed', encoding='utf8')
    with pytest.raises(ValueError, match='SSE hash mismatch'): adapter.import_row(row, tmp_path)


def test_ai_review_preserves_delegated_provenance(tmp_path):
    import hashlib
    path = tmp_path / 'var' / 'e1-20261029' / 'response.sse'; path.parent.mkdir(parents=True)
    path.write_text('event: answer_chunk\ndata: {"content":"hello"}\n\nevent: done\ndata: {}\n\n', encoding='utf8')
    spec = importlib.util.spec_from_file_location('e1_import', Path(__file__).resolve().parents[2] / 'scripts/import_e1_reviews.py')
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    row = {'qid':'q', 'sse':str(path.relative_to(tmp_path)), 'sse_sha256':sha, 'human_core':'UNKNOWN', 'result_case':{'trace_ids':['trace']}}
    review = {'qid':'q', 'sse_sha256':sha, 'assessor':'AI delegated core review; not a human judgment', 'core_judgment':'CORRECT_UNDER_USER_PROXY'}
    result = adapter.import_row(row, tmp_path, review)
    assert result['reviewer_type'] == 'AI_DELEGATED' and result['original_ai_core_judgment'] == 'CORRECT_UNDER_USER_PROXY'
    assert result['metrics']['eval.core_correctness'] == 'PASS'
    review['sse_sha256'] = 'incorrect'
    with pytest.raises(ValueError, match='provenance'): adapter.import_row(row, tmp_path, review)


def test_historical_local_binding_and_cloud_root_identity():
    import json
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('e1_import', repo / 'scripts/import_e1_reviews.py')
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    ledger = repo / 'var/e1-20261029/ledger-v4.jsonl'
    if not ledger.exists(): pytest.skip('historical local evidence unavailable')
    row = next(json.loads(line) for line in ledger.read_text(encoding='utf8').splitlines() if json.loads(line)['qid'] == 'office50:001')
    binding = adapter.historical_local_binding(row, repo)
    assert binding['answer_sha256'] == 'abd31d8d370e4be4101c2337ce0f9ca29a6261d5f32786d8c447ccfb09fa483d'
    payload = {'score_id':'s', 'trace_id':binding['trace_id'], 'metadata':{'qid':row['qid'], 'answer_sha256':binding['answer_sha256']}}
    root = NS(id=binding['root_observation_id'], trace_id=binding['trace_id'], is_root_observation=True, output=None, metadata={})
    class Client:
        api = NS(observations=NS(get_many=lambda **kw: NS(data=[root], meta=NS(cursor=None))))
    details = []
    assert publish.preflight_subjects(Client(), [payload], {row['qid']:binding}, details) == []
    assert details[0]['linkage_mode'] == 'immutable_local_case_and_cloud_root_identity'
    assert details[0]['cloud_answer_hash_confirmed'] is False
    root.id = 'other'
    assert ('s', 'answer_link_not_confirmed') in publish.preflight_subjects(Client(), [payload], {row['qid']:binding})
    row['events_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='hash mismatch'): adapter.historical_local_binding(row, repo)


@pytest.mark.parametrize("name,invalid", [
    ("eval.core_correctness", "BOGUS"), ("eval.answer_completeness", "PASS"),
    ("eval.faithfulness", "PARTIAL"), ("eval.citation_support", "COMPLETE"),
    ("eval.abstention", "FAIL"), ("eval.execution_status", "SUCCESS"),
    ("eval.evidence_state.context", "RELEVANT"),
])
def test_categorical_values_are_contract_enums(name, invalid):
    record = {"trace_id":"trace", "answer_sha256":"a"*64, "rubric_version":"v1", "evaluation_source":"review", "reviewer_type":"USER", "evidence_scope":"actual", "metrics":{name:invalid}}
    with pytest.raises(ValueError, match="invalid categorical value"):
        publish.e11_score_payloads([record])


def test_unknown_numeric_does_not_become_zero_and_execution_not_run_is_preserved():
    record = {"trace_id":"trace", "answer_sha256":"a"*64, "rubric_version":"v1", "evaluation_source":"review", "reviewer_type":"USER", "evidence_scope":"actual", "metrics":{"eval.citation_coverage":"UNKNOWN", "eval.execution_status":"NOT_RUN", "eval.core_correctness":"UNKNOWN"}}
    rows = publish.e11_score_payloads([record]); values = {r['name']:r['value'] for r in rows}
    assert 'eval.citation_coverage' not in values
    assert values == {'eval.execution_status':'NOT_RUN', 'eval.core_correctness':'UNKNOWN'}


def test_published_dryrun_payload_is_unchanged():
    import json
    root = Path(__file__).resolve().parents[2]
    source = root / 'var/e11-20261029/e1-review-import-v2.jsonl'
    expected = root / 'var/e11-20261029/scores-dryrun-v2.jsonl'
    if not source.exists() or not expected.exists(): pytest.skip('historical dry-run evidence unavailable')
    records = [json.loads(line) for line in source.read_text(encoding='utf8').splitlines() if line]
    previous = [json.loads(line) for line in expected.read_text(encoding='utf8').splitlines() if line]
    assert publish.e11_score_payloads(records) == previous


def ranking_record(name, value, **updates):
    row = {"trace_id":"trace", "answer_sha256":"a"*64, "rubric_version":"v1", "evaluation_source":"qrel", "grain":"chunk", "gold_grain":"chunk", "observed_grain":"chunk", "k":2, "stage":"retrieval", "qrel_version":"qrel-v1", "gold_completeness":True, "metrics":{name:value}}
    row.update(updates)
    return row


@pytest.mark.parametrize('name', ['retrieval.recall_at_k', 'retrieval.mrr_at_k', 'retrieval.ndcg_at_k'])
def test_ranking_publication_fails_closed_for_incomplete_or_mismatched_gold(name):
    with pytest.raises(ValueError, match='complete gold'): publish.e11_score_payloads([ranking_record(name, .5, gold_completeness=False)])
    with pytest.raises(ValueError, match='matching explicit grains'): publish.e11_score_payloads([ranking_record(name, .5, observed_grain='page')])
    with pytest.raises(ValueError, match='qrel metadata'): publish.e11_score_payloads([ranking_record(name, .5, gold_grain=None)])
    with pytest.raises(ValueError, match='invalid ranking score'): publish.e11_score_payloads([ranking_record(name, 1.01)])
    assert publish.e11_score_payloads([ranking_record(name, .5)])[0]['value'] == .5


def test_hit_partial_qrel_only_confirmed_positive():
    assert publish.e11_score_payloads([ranking_record('retrieval.hit_at_k', 1, gold_completeness=False)])[0]['value'] == 1
    with pytest.raises(ValueError, match='miss requires complete gold'): publish.e11_score_payloads([ranking_record('retrieval.hit_at_k', 0, gold_completeness=False)])
    with pytest.raises(ValueError, match='hit must be'): publish.e11_score_payloads([ranking_record('retrieval.hit_at_k', .5)])
    with pytest.raises(ValueError, match='boolean'): publish.e11_score_payloads([ranking_record('retrieval.recall_at_k', .5, gold_completeness='true')])


def test_ranking_rejects_duplicate_units_and_invalid_grades():
    opts = dict(gold_grain='chunk', observed_grain='chunk', gold_complete=True, k=2)
    with pytest.raises(ValueError, match='duplicate ranked units'):
        evaluate_ranking(['a'], ['a', 'a'], grades={'a':1}, **opts)
    with pytest.raises(ValueError, match='duplicate gold units'):
        evaluate_ranking(['a', 'a'], ['a'], grades={'a':1}, **opts)
    for invalid in (float('nan'), float('inf'), -1, True):
        with pytest.raises(ValueError, match='finite nonnegative'):
            evaluate_ranking(['a'], ['a'], grades={'a':invalid}, **opts)
    assert evaluate_ranking(['a','b'], ['b','a'], grades={'a':2,'b':1}, **opts)['ndcg_at_k'] < 1


def test_completeness_requires_a_verified_boolean_not_a_truthy_string():
    from app.services.evaluation import evaluate_evidence
    assert evaluate_evidence(['a'], ['a'], gold_grain='page', observed_grain='page', gold_complete='false')['coverage'] is None
    result = evaluate_ranking(['a'], ['a'], gold_grain='page', observed_grain='page', gold_complete='false', k=1)
    assert result['hit_at_k'] == 1 and result['recall_at_k'] is None and result['mrr_at_k'] is None


def score_record_for_gate(metrics, **metadata):
    result = {'trace_id':'trace', 'answer_sha256':'a'*64, 'rubric_version':'v1', 'evaluation_source':'review', 'reviewer_type':'USER', 'evidence_scope':'actual', 'metrics':metrics}
    result.update(metadata)
    return result


def test_evidence_publish_gate_complete_and_grain():
    good = {'grain':'chunk', 'gold_grain':'chunk', 'observed_grain':'chunk', 'gold_completeness':True}
    assert publish.e11_score_payloads([score_record_for_gate({'eval.evidence_coverage.context':.5}, **good)])[0]['value'] == .5
    for bad in ({**good, 'gold_completeness':False}, {**good, 'observed_grain':'page'}, {}):
        for metrics in ({'eval.evidence_coverage.context':.5}, {'eval.evidence_state.context':'PRESENT'}):
            with pytest.raises(ValueError, match='same-grain gold'): publish.e11_score_payloads([score_record_for_gate(metrics, **bad)])
    assert publish.e11_score_payloads([score_record_for_gate({'eval.evidence_state.context':'UNKNOWN'})])[0]['value'] == 'UNKNOWN'
    assert publish.e11_score_payloads([score_record_for_gate({'eval.evidence_coverage.context':'UNKNOWN'})]) == []
    with pytest.raises(ValueError, match='invalid evidence coverage'): publish.e11_score_payloads([score_record_for_gate({'eval.evidence_coverage.context':1.1}, **good)])


def test_citation_coverage_requires_reviewed_counts_and_matching_ratio():
    metrics={'eval.citation_coverage':.5}
    assert publish.e11_score_payloads([score_record_for_gate(metrics, supported_claims=1, citable_claims=2)])[0]['value'] == .5
    for values in ({}, {'supported_claims':1,'citable_claims':0}, {'supported_claims':3,'citable_claims':2}):
        with pytest.raises(ValueError, match='claim counts'): publish.e11_score_payloads([score_record_for_gate(metrics, **values)])
    with pytest.raises(ValueError, match='ratio'): publish.e11_score_payloads([score_record_for_gate({'eval.citation_coverage':.7}, supported_claims=1,citable_claims=2)])


def test_core_pass_requires_matching_core_verdict():
    for verdict, value in [('PASS',False),('FAIL',True),('PARTIAL',False),('UNKNOWN',True),(None,True)]:
        m={'eval.core_pass':value}
        if verdict is not None: m['eval.core_correctness']=verdict
        with pytest.raises(ValueError, match='matching PASS or FAIL'): publish.e11_score_payloads([score_record_for_gate(m)])
    rows=publish.e11_score_payloads([score_record_for_gate({'eval.core_correctness':'FAIL','eval.core_pass':False})])
    assert {r['name']:r['value'] for r in rows} == {'eval.core_correctness':'FAIL','eval.core_pass':0.0}
