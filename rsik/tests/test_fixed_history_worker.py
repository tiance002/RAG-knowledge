"""Synthetic data + actual frozen backend/recording stub, never paid QA."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from rsik.capsule import BASE_COMMIT
from rsik.fixed_history_worker import (EVALUATOR_PROFILE,HISTORY_PROFILE,build_worker,history_digest,
    validate_envelope,validate_history_sidecar,validate_snapshot_history)

ROOT=Path(__file__).resolve().parents[2]


def envelope(*,zero=False,failed_prefix=False):
    question='SYNTHETIC current query'
    history=[{'role':'user','content':'SYNTHETIC previous question'},
             {'role':'assistant','content':'SYNTHETIC actual emitted answer'}]
    evidence=[{'input':{'SYNTHETIC':'NOT_REAL_PROOF'},'result':{'SYNTHETIC':'NOT_REAL_PROOF'},
               'run_id':'SYNTHETIC','assistant_message_present':True}]
    if failed_prefix:
        history.append({'role':'user','content':'SYNTHETIC failed earlier question'})
        evidence.append({'input':{'SYNTHETIC':'NOT_REAL_PROOF'},'result':{'SYNTHETIC':'NOT_REAL_PROOF'},
                         'run_id':'SYNTHETIC','assistant_message_present':False})
    sidecar={'schema':HISTORY_PROFILE,'status':'AUDIT_ONLY_NOT_ADMITTED','item_id':'SYNTHETIC-1',
        'item_kind':'session_turn','input_policy':'FIXED_ACTUAL_SEMANTIC_PREFIX','history':history,
        'history_sha256':history_digest(history),'question_sha256':hashlib.sha256(question.encode()).hexdigest(),
        'prefix_evidence':evidence,'gold_history_used':False,'candidate_history_access':False}
    sidecar['subject_sha256']=history_digest(sidecar)
    chunks=[] if zero else [{'id':'synthetic-seed','file_id':'synthetic-file','content':'SYNTHETIC source fact.',
        'content_sha256':hashlib.sha256(b'SYNTHETIC source fact.').hexdigest(),'original_index':0,
        'source_locator':{'record_start':1,'record_end':1},'content_type':'text'}]
    case={'question':question,'allowed_kb_ids':['synthetic-kb'],'files':[] if zero else
        [{'id':'synthetic-file','name':'synthetic-source.txt','kb_id':'synthetic-kb'}],
        'chunks':chunks,'seed_ids':[] if zero else ['synthetic-seed'],'expected_seed_count':0 if zero else 1}
    return {'profile':EVALUATOR_PROFILE,'case':case,'history_sidecar':sidecar,
            'history_sha256':history_digest(history),'sidecar_sha256':history_digest(sidecar),
            'qid':'SYNTHETIC-1','lineage_id':'SYNTHETIC-FIXED-HISTORY-ONLY',
            'base_question_sha256':sidecar['question_sha256'],'current_question_sha256':sidecar['question_sha256'],
            'query_derivation_sha256':history_digest(None)}


def rebind(value):
    sidecar=value['history_sidecar'];sidecar['history_sha256']=history_digest(sidecar['history'])
    sidecar['subject_sha256']=history_digest({k:v for k,v in sidecar.items() if k!='subject_sha256'})
    value['history_sha256']=history_digest(sidecar['history']);value['sidecar_sha256']=history_digest(sidecar)


def test_standalone_template_matches_trusted_helpers_and_six_argument_parent():
    raw=(ROOT/'scripts/rsi_fixed_history_worker_v1.py').read_bytes()
    assert build_worker()==raw
    from rsik.query_batch import diagnostic_worker
    compile(diagnostic_worker(raw),'<fixed-history-diagnostic-batch>','exec')
    from rsik.query_aware import adapt_legacy
    parent=adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    child=build_worker(parent)
    from run_rsi_loop import selector
    assert selector(child.decode())[1]==selector(parent.decode())[1]


@pytest.mark.parametrize('bad,reason',[
    ('tamper','TRUSTED_SIDECAR_REQUIRED'),('order','ROLE_ORDER_INVALID'),
    ('current','CURRENT_USER_CHANGED'),('gold','TRUSTED_SIDECAR_REQUIRED'),
    ('extra_reference','ROLE_ORDER_INVALID'),
])
def test_history_tamper_order_current_user_and_gold_rejected(bad,reason):
    value=envelope()
    if bad=='tamper':value['history_sidecar']['history'][0]['content']='changed'
    elif bad=='order':value['history_sidecar']['history'].reverse();rebind(value)
    elif bad=='current':value['case']['question']='different current user'
    elif bad=='gold':value['history_sidecar']['gold_history_used']=True;rebind(value)
    else:value['history_sidecar']['history'][1]['answer_reference']='not actual';rebind(value)
    with pytest.raises(ValueError,match=reason):validate_envelope(value)


def test_actual_failed_prefix_roles_are_preserved_not_forced_alternating():
    value=envelope(failed_prefix=True)
    _,history=validate_envelope(value)
    assert [m['role'] for m in history]==['user','assistant','user']
    prepared={'evaluator_profile':EVALUATOR_PROFILE,'history_profile':HISTORY_PROFILE,'fixed_history':history,
        'history_sha256':value['history_sha256'],'sidecar_sha256':value['sidecar_sha256'],'qid':value['qid'],
        'lineage_id':value['lineage_id'],'question':value['case']['question'],
        'current_question_sha256':value['history_sidecar']['question_sha256'],'base_question_sha256':value['base_question_sha256'],
        'query_derivation_sha256':value['query_derivation_sha256']}
    assert validate_snapshot_history(prepared)==history
    changed=copy.deepcopy(prepared);changed['fixed_history'].reverse()
    with pytest.raises(ValueError,match='ROLE_ORDER_INVALID|PREPARED_HASH_CHANGED'):validate_snapshot_history(changed)


def test_explicit_variant_keeps_original_sidecar_and_binds_current_query_separately():
    value=envelope();original=copy.deepcopy(value['history_sidecar'])
    value['case']['question']='SYNTHETIC reviewed alternate current query'
    value['current_question_sha256']=hashlib.sha256(value['case']['question'].encode()).hexdigest()
    value['query_derivation_sha256']=history_digest({'SYNTHETIC':'HOST_REVIEW_BINDING_ONLY'})
    validate_envelope(value)
    assert value['history_sidecar']==original
    value['base_question_sha256']='0'*64
    with pytest.raises(ValueError,match='CURRENT_USER_CHANGED'):validate_envelope(value)


@pytest.fixture(scope='module')
def frozen_backend(tmp_path_factory):
    target=tmp_path_factory.mktemp('actual-frozen-backend-fixed-history')
    archive=subprocess.check_output(['git','-C',str(ROOT),'archive',BASE_COMMIT,'backend/app'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(target,filter='data')
    scripts=target/'scripts';scripts.mkdir()
    for name in ('rsi_delivery_worker.py','replay_source_preparation.py'):
        (scripts/name).write_bytes((ROOT/'scripts'/name).read_bytes())
    (scripts/'rsi_fixed_history_worker_v1.py').write_bytes(build_worker())
    env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',LANGFUSE_ENABLED='false',DATABASE_URL='sqlite://',
        PYTHONPATH=os.pathsep.join((str(target/'backend'),str(scripts))))
    return target,scripts/'rsi_fixed_history_worker_v1.py',env


def run_stage(frozen_backend,tmp_path,stage,*inputs):
    root,worker,env=frozen_backend;output=tmp_path/(stage+'-capture.json')
    command=[sys.executable,'-B',str(worker),stage,*map(str,inputs),str(output)]
    result=subprocess.run(command,cwd=root,env=env,capture_output=True,timeout=45)
    (tmp_path/(stage+'-stdout.log')).write_bytes(result.stdout)
    (tmp_path/(stage+'-stderr.log')).write_bytes(result.stderr)
    assert result.returncode==0, f'LOCAL_SYNTHETIC_FROZEN_BACKEND process exit {result.returncode}'
    assert output.is_file(),'Worker output absent; see retained stderr/stdout'
    return json.loads(output.read_text(encoding='utf-8-sig')),output


def test_frozen_business_provider_gets_numbered_system_then_fixed_history_current_user(frozen_backend,tmp_path):
    value=envelope(failed_prefix=True);input_path=tmp_path/'envelope.json';input_path.write_text(json.dumps(value),encoding='utf-8')
    prepared,snapshot=run_stage(frozen_backend,tmp_path,'prepare',input_path)
    assert prepared['status']=='OK'
    assert prepared['fixed_history']==value['history_sidecar']['history']
    assert 'history_sidecar' not in prepared and 'prefix_evidence' not in prepared
    assert 'gold_history_used' not in prepared
    config=tmp_path/'config.json';config.write_bytes(b'{"selection":"all"}')
    capture,_=run_stage(frozen_backend,tmp_path,'deliver',snapshot,config)
    assert capture['status']=='OK',capture.get('reason')
    assert capture['request_calls']==1 and capture['provider_delivery_status']=='PROVIDER_REQUEST_CAPTURED'
    assert capture['messages'][0]['role']=='system' and '[1]' in capture['messages'][0]['content']
    expected=value['history_sidecar']['history']+[{'role':'user','content':value['case']['question']}]
    assert capture['messages'][1:]==capture['business_input_messages']==expected
    assert capture['history_sha256']==value['history_sha256'] and capture['sidecar_sha256']==value['sidecar_sha256']
    assert capture['proof'] and capture['terminal_events'][-1]['event']=='done'
    assert capture['business_outcome']['answer_mode']=='GROUNDED_GENERATION'


def test_frozen_zero_seed_kb_branch_executes_real_no_provider_terminal(frozen_backend,tmp_path):
    value=envelope(zero=True);input_path=tmp_path/'envelope.json';input_path.write_text(json.dumps(value),encoding='utf-8')
    prepared,snapshot=run_stage(frozen_backend,tmp_path,'prepare',input_path)
    assert prepared['status']=='OK'
    config=tmp_path/'config.json';config.write_bytes(b'{"selection":"all"}')
    capture,_=run_stage(frozen_backend,tmp_path,'deliver',snapshot,config)
    assert capture['status']=='OK',capture.get('reason')
    assert capture['request_calls']==0 and capture['provider_delivery_status']=='NO_PROVIDER_REQUEST'
    assert capture['messages']==[] and capture['proof']==[] and capture['members']==[]
    assert capture['business_input_messages']==value['history_sidecar']['history']+[{'role':'user','content':value['case']['question']}]
    assert capture['business_outcome']['answer_mode']=='INSUFFICIENT_EVIDENCE'
    assert capture['business_outcome']['retrieval_status']=='NO_RESULTS'
    assert capture['terminal_events'][-1]['event']=='done'
    assert any(e['event']=='answer_chunk' for e in capture['events'])
