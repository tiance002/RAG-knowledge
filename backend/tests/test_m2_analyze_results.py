"""Small synthetic inputs only; no service/model/network execution."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'm2_analyze_results.py'
SPEC = importlib.util.spec_from_file_location('m2_analysis_test', PATH)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def write_rows(path, rows):
    path.write_bytes(b''.join(analyzer.canonical(row) + b'\n' for row in rows))


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, 'ROOT', tmp_path)
    run = tmp_path / 'var' / 'run';run.mkdir(parents=True)
    manifest = {'dataset_version': 'synthetic-v1', 'repetitions': 2,
                'pricing': {'input_cny_per_million': .8, 'output_cny_per_million': 2.8},
                'chunks': [{'id': 'alpha', 'content': 'PRIVATE FACT alpha'},
                           {'id': 'pdf-layout', 'content': 'PRIVATE PDF TEXT 81 78 Men Women'}],
                'cases': [{'qid': 'q1', 'question': 'PRIVATE QUESTION 1', 'expected_chunk_ids': ['alpha'], 'negative': False},
                          {'qid': 'pdf', 'question': 'PRIVATE QUESTION PDF', 'expected_chunk_ids': ['pdf-layout'], 'negative': False},
                          {'qid': 'negative', 'question': 'PRIVATE QUESTION NEGATIVE', 'expected_chunk_ids': [], 'negative': True}]}
    (run / 'manifest.json').write_bytes(analyzer.canonical(manifest))
    trials, ledger = [], []
    identities = [('q1', 'chat', 'A', 1), ('q1', 'chat', 'B', 1), ('q1', 'chat', 'A', 2),
                  ('q1', 'chat', 'B', 2), ('q1', 'agent', 'A', 1), ('pdf', 'chat', 'A', 1), ('negative', 'chat', 'A', 1)]
    for call_id, (qid, mode, arm, rep) in enumerate(identities, 1):
        unknown = call_id == 5
        trial = {'qid': qid, 'mode': mode, 'arm': arm, 'repetition': rep, 'run_id': 'run',
                 'latency_ms': call_id * 100, 'provider_call_ids': [] if qid == 'negative' else [call_id],
                 'state': 'GUARD_STOP' if unknown else 'RETURNED', 'events': [
                     {'event': 'answer_chunk', 'data': {'content': '' if unknown else 'PRIVATE ANSWER TEXT'}},
                     {'event': 'rag_result', 'data': {'citations': [{'content': 'PRIVATE CITATION'}]}},
                     *([] if unknown else [{'event': 'done', 'data': {'generation_status': 'COMPLETED'}}])]}
        trials.append(trial)
        if qid == 'negative':continue
        pending = {'call_id': call_id, 'qid': qid, 'mode': mode, 'arm': arm, 'repetition': rep,
                   'state': 'UNKNOWN_PENDING', 'reservation_cny': '.02'}
        ledger.append(pending)
        if not unknown:
            completed = {**pending, 'state': 'COMPLETED', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50},
                         'price_based_cost_cny': '.00022'}
            if call_id == 4:completed.pop('price_based_cost_cny')
            ledger.append(completed)
            chunk = 'PRIVATE PDF TEXT 81 78 Men Women' if qid == 'pdf' else 'PRIVATE FACT alpha'
            (run / f'request-{call_id:04d}.json').write_bytes(analyzer.canonical({'messages': [{'role': 'system', 'content': chunk}]}))
            (run / f'response-{call_id:04d}.body').write_bytes(analyzer.canonical({'choices': [{
                'message': {'content': 'PRIVATE ANSWER TEXT'}, 'finish_reason': 'length' if call_id == 2 else 'stop'}]}))
    write_rows(run / 'trials.jsonl', trials);write_rows(run / 'provider-ledger.jsonl', ledger)
    reviews = []
    for qid, mode, arm, rep in identities:
        if mode == 'agent' or (arm == 'B' and rep == 2):continue
        row = {'qid': qid, 'mode': mode, 'arm': arm, 'repetition': rep,
               'core': 'FAIL' if arm == 'B' else 'UNKNOWN' if rep == 2 else 'PASS',
               'faithfulness': 'PASS', 'core_evidence_delivery': 'UNKNOWN' if qid == 'pdf' else 'PASS',
               'core_inline_citation_recall': None if qid == 'negative' else 1,
               'core_rationale': 'PRIVATE REVIEW RATIONALE', 'actual_answer': 'PRIVATE ACTUAL ANSWER'}
        if qid == 'pdf':row.update(citation_support_original='PASS', citation_support_delivered_text='UNKNOWN')
        else:row['citation_support']='PASS'
        reviews.append(row)
    write_rows(run / 'review-office001.jsonl', reviews)
    observed = []
    for mode, arm, rep in [('chat', 'A', 1), ('agent', 'A', 1)]:
        identity = {'qid': 'q1', 'run_id': 'run', 'attempt_id': f'{mode}-{arm}-r{rep}-{analyzer.sha(b"q1")[:12]}'}
        observed += [{**identity, 'stage': 'retrieval.bm25', 'metadata': {'candidates': [{}, {}]}},
                     {**identity, 'stage': 'retrieval.outcome', 'metadata': {'result_count': 2}}]
        if mode == 'chat':observed.append({**identity, 'stage': 'qa.request', 'event': 'end'})
        else:observed += [{**identity, 'stage': 'agent.tool_search'},
                          {**identity, 'stage': 'agent.retry_decision', 'metadata': {'action': 'SCHEDULED'}}]
    write_rows(run / 'observations.jsonl', observed)
    return run, manifest, trials, ledger, reviews


def row(result, qid='q1', mode='chat', arm='A', rep=1):
    return next(x for x in result['trials'] if (x['qid'],x['mode'],x['arm'],x['repetition']) == (qid,mode,arm,rep))


def test_missing_unreviewed_and_explicit_denominators(data):
    run, *_ = data
    result = analyzer.analyze(run, run / 'manifest.json')
    assert result['totals']['counts']['planned'] == 48
    assert result['totals']['counts']['recorded'] == 7
    assert result['totals']['counts']['answered'] == 6
    assert result['totals']['counts']['missing_trial_records'] == 41
    assert result['totals']['counts']['empty_answer'] == 1
    assert row(result, arm='B', rep=2)['core'] == 'UNKNOWN'
    assert row(result, arm='C')['execution_state'] == 'NOT_RUN'
    assert result['totals']['review_metrics']['core']['answered_denominator'] == 6
    assert result['source_run_final'] is False
    assert sum(x['status']=='MISSING' for x in result['inputs']) >= 4


def test_review_only_no_private_content_output_and_inputs_unchanged(data):
    run, *_ = data
    before = {p:p.read_bytes() for p in run.iterdir()}
    result = analyzer.analyze(run, run / 'manifest.json')
    assert b'PRIVATE' not in analyzer.canonical(result)
    assert before == {p:p.read_bytes() for p in run.iterdir()}
    assert row(result, arm='B')['core'] == 'FAIL'
    assert row(result, rep=2)['core'] == 'UNKNOWN'


def test_pdf_original_support_does_not_claim_delivered_alignment(data):
    run, *_ = data
    result = analyzer.analyze(run, run/'manifest.json')
    pdf = row(result,qid='pdf')
    assert pdf['citation_support']=='UNKNOWN'
    assert pdf['citation_support_original']=='PASS'
    assert pdf['citation_support_delivered_text']=='UNKNOWN'
    assert pdf['exact_qrel_chunk_text_recall']==1
    assert pdf['context_recall'] is None
    assert pdf['context_recall_status']=='UNKNOWN_ALIGNMENT_OR_PARTIAL_TEXT'
    assert row(result)['context_recall']==1
    assert row(result,qid='negative')['context_recall_status']=='NOT_APPLICABLE'


def test_paired_comparison_same_question_rep_only_and_unknown_not_imputed(data):
    run, *_ = data
    result=analyzer.analyze(run,run/'manifest.json')
    pair=next(x for x in result['paired_comparisons'] if (x['mode'],x['case_type'],x['left'],x['right'])==('chat','positive','A','B'))
    assert pair['planned_pair_denominator']==4
    assert pair['paired_recorded']==2 and pair['unpaired_recorded']==1 and pair['both_missing']==1
    assert pair['review_metrics']['core']['transitions']=={'PASS->FAIL':1,'UNKNOWN->UNKNOWN':1}
    assert pair['review_metrics']['core']['both_determinate_denominator']==1
    assert pair['shared_answered_keys']==[{'qid':'q1','repetition':1},{'qid':'q1','repetition':2}]
    assert pair['numeric_deltas']['latency_ms']['paired_known_denominator']==2


def test_tokens_cost_and_unknown_provider_are_independent(data):
    run, *_=data
    result=analyzer.analyze(run,run/'manifest.json')
    good=row(result)
    assert good['prompt_tokens']==100 and good['completion_tokens']==50
    assert good['price_derived_cost_cny']=='0.00022'
    no_cost=row(result,arm='B',rep=2)
    assert no_cost['prompt_tokens']==100 and no_cost['price_derived_cost_cny'] is None
    pending=row(result,mode='agent')
    assert pending['prompt_tokens'] is None and pending['price_derived_cost_cny'] is None
    assert pending['unknown_provider_calls']==1
    assert pending['provider_retry_count']=='UNKNOWN_NOT_DIRECTLY_RECORDED'
    assert result['provider_attempts']==6
    assert result['provider_states_latest']=={'COMPLETED':5,'UNKNOWN_PENDING':1}
    assert row(result,arm='B')['truncated'] is True


def test_tools_retries_candidates_require_observable_events(data):
    run,*_=data
    result=analyzer.analyze(run,run/'manifest.json')
    assert row(result)['tool_calls']==0 and row(result)['retrieval_retries']==0
    assert row(result)['bm25_candidate_count']==2
    assert row(result,mode='agent')['tool_calls']==1
    assert row(result,mode='agent')['retrieval_retries']==1
    assert row(result,arm='B')['tool_calls'] is None
    assert row(result,arm='B')['bm25_candidate_count'] is None
    assert row(result,arm='B')['delivered_citation_counts_per_packet']==[1]


def test_duplicate_review_and_provider_identity_mismatch_fail(data):
    run,manifest,trials,ledger,reviews=data
    write_rows(run/'review-office001.jsonl',reviews+[reviews[0]])
    with pytest.raises(ValueError,match='Duplicate review'):analyzer.analyze(run,run/'manifest.json')
    write_rows(run/'review-office001.jsonl',reviews)
    ledger[1]['arm']='C';write_rows(run/'provider-ledger.jsonl',ledger)
    with pytest.raises(ValueError,match='identity disagree'):analyzer.analyze(run,run/'manifest.json')


def test_stale_review_provider_ids_becomes_unknown(data):
    run,manifest,trials,ledger,reviews=data
    reviews[0]['provider_call_ids']=[999];write_rows(run/'review-office001.jsonl',reviews)
    result=analyzer.analyze(run,run/'manifest.json')
    assert row(result)['review_usable'] is False and row(result)['core']=='UNKNOWN'


def test_snapshot_detects_change_and_absent_file_appearing(data):
    run,*_=data
    snapshot=analyzer.Snapshot();path=run/'manifest.json';snapshot.read(path)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed during'):snapshot.verify()
    snapshot=analyzer.Snapshot();new=run/'future.jsonl';snapshot.read(new,optional=True);new.write_bytes(b'{}')
    with pytest.raises(ValueError,match='changed during'):snapshot.verify()


def test_negative_recall_denominator_is_not_unknown(data):
    run,*_=data
    result=analyzer.analyze(run,run/'manifest.json')
    group=next(x for x in result['groups'] if x['mode']=='chat' and x['arm']=='A' and x['case_type']=='negative')
    recall=group['numeric_metrics']['core_inline_citation_recall']
    assert recall['not_applicable_denominator']==2 and recall['unknown_denominator']==0


def test_candidate_metrics_preferred_over_projected_candidate_list(data):
    run,manifest,trials,ledger,reviews=data
    rows=[json.loads(line) for line in (run/'observations.jsonl').read_bytes().splitlines()]
    identity={key:rows[0][key] for key in ('qid','attempt_id','run_id')}
    rows.append({**identity,'stage':'retrieval.metrics','metadata':{'metrics':{'bm25_count':50}}})
    write_rows(run/'observations.jsonl',rows)
    result=analyzer.analyze(run,run/'manifest.json')
    assert row(result)['bm25_candidate_count']==50
    assert row(result)['bm25_candidate_count_source']=='RETRIEVAL_METRICS'


def add_extra(data):
    run,manifest,trials,ledger,reviews=data
    manifest['budget_cny']=1
    (run/'manifest.json').write_bytes(analyzer.canonical(manifest))
    config={'search_mode':'fulltext','top_k':10,'context_window':0,'rerank_enabled':False,'use_rewrite':False}
    (run/'preflight.json').write_bytes(analyzer.canonical({'fixed_search':config}))
    extra=run.parent/'continuation';extra.mkdir()
    (extra/'manifest.json').write_bytes(analyzer.canonical(manifest))
    (extra/'preflight.json').write_bytes(analyzer.canonical({'fixed_search':config}))
    trial={'qid':'q1','mode':'agent','arm':'B','repetition':1,'run_id':'continuation','state':'RETURNED',
           'provider_call_ids':[1],'latency_ms':900,'events':[
               {'event':'answer_chunk','data':{'content':'PRIVATE EXTRA ANSWER'}},
               {'event':'done','data':{'generation_status':'COMPLETED'}}]}
    pending={'call_id':1,'qid':'q1','mode':'agent','arm':'B','repetition':1,'state':'UNKNOWN_PENDING','reservation_cny':'.02'}
    completed={**pending,'state':'COMPLETED','usage':{'prompt_tokens':7,'completion_tokens':3},
               'price_based_cost_cny':'.000014','provider_request_id':'extra-unique-provider-id'}
    write_rows(extra/'trials.jsonl',[trial]);write_rows(extra/'provider-ledger.jsonl',[pending,completed])
    (extra/'request-0001.json').write_bytes(analyzer.canonical({'messages':[{'role':'system','content':'PRIVATE FACT alpha'}]}))
    (extra/'response-0001.body').write_bytes(analyzer.canonical({'choices':[{'message':{'content':'PRIVATE EXTRA ANSWER'},'finish_reason':'stop'}]}))
    write_rows(extra/'review-technical.jsonl',[{'qid':'q1','mode':'agent','arm':'B','repetition':1,
        'core':'PARTIAL','faithfulness':'UNKNOWN','core_evidence_delivery':'PASS','provider_call_ids':[1],
        'final_request_id':1,'core_inline_citation_recall':1}])
    return extra,config


def add_supplement(data,config):
    run,manifest,*_=data
    directory=run.parent/'supplement';directory.mkdir()
    case={'qid':'supp-only','question':'PRIVATE SUPPLEMENT QUESTION','expected_chunk_ids':['alpha'],'negative':False}
    spec={'parent_manifest_path':str(run/'manifest.json'),
          'parent_manifest_sha256':analyzer.sha((run/'manifest.json').read_bytes()),'case':case}
    (directory/'supplement-manifest.json').write_bytes(analyzer.canonical(spec))
    trial={'qid':'supp-only','mode':'chat','arm':'A','repetition':1,'run_id':'supplement','state':'RETURNED',
           'provider_call_ids':[1],'events':[{'event':'answer_chunk','data':{'content':'PRIVATE SUPPLEMENT ANSWER'}},
                                           {'event':'done','data':{'generation_status':'COMPLETED'}}]}
    pending={'call_id':1,'qid':'supp-only','mode':'chat','arm':'A','repetition':1,'state':'UNKNOWN_PENDING','reservation_cny':'.02'}
    completed={**pending,'state':'COMPLETED','usage':{'prompt_tokens':2,'completion_tokens':1},
               'price_based_cost_cny':'.0000044','provider_request_id':'supplement-unique-provider-id'}
    write_rows(directory/'trials.jsonl',[trial]);write_rows(directory/'provider-ledger.jsonl',[pending,completed])
    (directory/'summary.json').write_bytes(analyzer.canonical({'status':'STOPPED','actual_http_calls':1,'recorded_trials':1,
        'fixed_search':config,'committed_including_unknown_cny':'.0000044',
        'global_committed_including_unknown_cny':'123456'}))
    (directory/'request-0001.json').write_bytes(analyzer.canonical({'messages':[{'role':'system','content':'PRIVATE FACT alpha'}]}))
    (directory/'response-0001.body').write_bytes(analyzer.canonical({'choices':[{'message':{'content':'PRIVATE SUPPLEMENT ANSWER'},'finish_reason':'stop'}]}))
    write_rows(directory/'review-supplement.jsonl',[{'qid':'supp-only','mode':'chat','arm':'A','repetition':1,
        'core':'PASS','faithfulness':'PASS','core_evidence_delivery':'PASS'}])
    return directory


def test_combine_scopes_restarted_provider_ids_and_original_denominator(data):
    run,*_=data
    extra,config=add_extra(data)
    result=analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra],extra_review_dirs=[extra])
    assert result['totals']['counts']['planned']==48
    assert result['totals']['counts']['recorded']==8
    assert result['totals']['counts']['missing_trial_records']==40
    assert result['provider_attempts']==7
    original=row(result);continued=row(result,mode='agent',arm='B')
    assert original['provider_call_ids']==continued['provider_call_ids']==[1]
    assert original['prompt_tokens']==100 and continued['prompt_tokens']==7
    assert original['scoped_provider_calls'][0]['run_dir']!=continued['scoped_provider_calls'][0]['run_dir']
    assert continued['core']=='PARTIAL'
    assert continued['evidence']['review']['line']==1
    assert continued['evidence']['trial']['path']==str(extra/'trials.jsonl')
    assert b'PRIVATE' not in analyzer.canonical(result)


def test_multirun_duplicate_identity_or_frozen_config_mismatch_rejected(data):
    run,manifest,trials,*_=data
    extra,config=add_extra(data)
    write_rows(extra/'trials.jsonl',[trials[0]])
    with pytest.raises(ValueError,match='Duplicate frozen trial'):analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra])
    (extra/'preflight.json').write_bytes(analyzer.canonical({'fixed_search':{**config,'top_k':9}}))
    with pytest.raises(ValueError,match='configuration'):analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra])


def test_multirun_manifest_mismatch_rejected(data):
    run,manifest,*_=data
    extra,config=add_extra(data)
    (extra/'manifest.json').write_bytes(analyzer.canonical({**manifest,'max_output_tokens':999}))
    with pytest.raises(ValueError,match='manifest differs'):analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra])


def test_d_supplement_separate_statistics_but_budget_added_once(data):
    run,*_=data
    extra,config=add_extra(data);supplement=add_supplement(data,config)
    result=analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra],supplement_run_dirs=[supplement])
    assert result['totals']['counts']['planned']==48 and result['totals']['counts']['recorded']==8
    assert result['supplementary_D'][0]['totals']['counts']['planned']==8
    assert result['supplementary_D'][0]['totals']['counts']['recorded']==1
    assert {x['arm'] for x in result['supplementary_D'][0]['trials']}=={'A','D'}
    assert result['supplementary_D'][0]['four_arm_inference']=='NOT_ESTABLISHED_BY_SUPPLEMENT'
    assert len(result['budget_accounting']['per_run'])==3
    assert result['budget_accounting']['known_price_derived_cost_cny']=='0.0008984'
    assert result['budget_accounting']['committed_including_unknown_cny']=='0.0408984'
    assert result['budget_accounting']['complete_price_derived_cost_cny'] is None
    assert result['budget_accounting']['within_recorded_budget'] is True
    assert all(x['qid']!='supp-only' for x in result['trials'])


def test_continuation_contract_discovers_supplement_and_verifies_all_hashes(data):
    run,*_=data
    extra,config=add_extra(data);supplement=add_supplement(data,config)
    (run/'summary.json').write_bytes(analyzer.canonical({'status':'STOPPED','actual_http_calls':6,
                                                       'committed_including_unknown_cny':'.04088'}))
    contract={'parent_output':str(run),'parent_manifest_path':str(run/'manifest.json'),
              'parent_manifest_sha256':analyzer.sha((run/'manifest.json').read_bytes()),
              'prior_runs':[{'kind':'D_ELIGIBILITY_ONLY','output':str(supplement)}]}
    for role,name in [('summary','summary.json'),('ledger','provider-ledger.jsonl'),('trials','trials.jsonl')]:
        contract[f'parent_{role}_sha256']=analyzer.sha((run/name).read_bytes())
        contract['prior_runs'][0][f'{role}_sha256']=analyzer.sha((supplement/name).read_bytes())
    (extra/'continuation-manifest.json').write_bytes(analyzer.canonical(contract))
    result=analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra])
    assert len(result['supplementary_D'])==1 and len(result['budget_accounting']['per_run'])==3
    contract['prior_runs'][0]['trials_sha256']='wrong'
    (extra/'continuation-manifest.json').write_bytes(analyzer.canonical(contract))
    with pytest.raises(ValueError,match='supplement evidence hash'):analyzer.analyze(run,run/'manifest.json',extra_run_dirs=[extra])


def test_multirun_snapshot_mutation_produces_no_output(data,monkeypatch):
    run,*_=data;extra,config=add_extra(data)
    original=analyzer.shared_budget
    def mutate(*args,**kwargs):
        result=original(*args,**kwargs);path=extra/'trials.jsonl';path.write_bytes(path.read_bytes()+b'\n');return result
    monkeypatch.setattr(analyzer,'shared_budget',mutate)
    output=run.parent/'no-inconsistent-output'
    monkeypatch.setattr('sys.argv',['m2_analyze_results.py','--run-dir',str(run),'--extra-run-dir',str(extra),'--output',str(output)])
    assert analyzer.main()==1 and not output.exists()
