"""NEW trusted fixed-history source replay; original worker remains unchanged.

Standalone capsule file: the existing source archive already supplies the
protected replay helper and frozen backend. Provider calls use only its existing
recording-stub mechanism. Zero-result business branches are actually executed.
"""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import rsi_delivery_worker as legacy

require=legacy.require
duplicate_key=legacy.duplicate_key

# FIXED_HISTORY_HELPERS_BEGIN
EVALUATOR_PROFILE='source-relations-fixed-history/v1'

HISTORY_PROFILE='qa500-fixed-actual-history-sidecar/v1'

def history_digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def validate_history_messages(messages):
    if not isinstance(messages,list):raise ValueError('FIXED_HISTORY_LIST_REQUIRED')
    previous=None
    for i,message in enumerate(messages):
        if (not isinstance(message,dict) or set(message)!={'role','content'}
                or message['role'] not in {'user','assistant'} or not isinstance(message['content'],str)
                or not message['content'] or (i==0 and message['role']!='user')
                or (previous=='assistant' and message['role']=='assistant')):
            raise ValueError('FIXED_HISTORY_ROLE_ORDER_INVALID')
        previous=message['role']
    return messages

def validate_history_sidecar(sidecar,question,base_question_sha256=None):
    if (not isinstance(sidecar,dict) or sidecar.get('schema')!=HISTORY_PROFILE
            or sidecar.get('status')!='AUDIT_ONLY_NOT_ADMITTED'
            or sidecar.get('gold_history_used') is not False or sidecar.get('candidate_history_access') is not False
            or sidecar.get('subject_sha256')!=history_digest({k:v for k,v in sidecar.items() if k!='subject_sha256'})):
        raise ValueError('FIXED_HISTORY_TRUSTED_SIDECAR_REQUIRED')
    messages=validate_history_messages(sidecar.get('history'))
    if sidecar.get('history_sha256')!=history_digest(messages):raise ValueError('FIXED_HISTORY_HASH_CHANGED')
    base=base_question_sha256 if base_question_sha256 is not None else hashlib.sha256(question.encode('utf-8')).hexdigest() if isinstance(question,str) else None
    if not isinstance(question,str) or sidecar.get('question_sha256')!=base:
        raise ValueError('FIXED_HISTORY_CURRENT_USER_CHANGED')
    evidence=sidecar.get('prefix_evidence')
    if not isinstance(evidence,list):raise ValueError('FIXED_HISTORY_PREFIX_EVIDENCE_REQUIRED')
    if sidecar.get('input_policy')=='EMPTY_HISTORY_NEW_ISOLATED_RUN':
        if messages or evidence or sidecar.get('item_kind')!='standalone':raise ValueError('FIXED_HISTORY_STATELESS_POLICY_CHANGED')
    elif sidecar.get('input_policy')=='FIXED_ACTUAL_SEMANTIC_PREFIX':
        if sidecar.get('item_kind')!='session_turn' or not evidence:raise ValueError('FIXED_HISTORY_ACTUAL_PREFIX_REQUIRED')
        expected=[]
        for record in evidence:
            if (not isinstance(record,dict) or type(record.get('assistant_message_present'))is not bool
                    or not isinstance(record.get('input'),dict) or not isinstance(record.get('result'),dict)
                    or not record.get('run_id')):raise ValueError('FIXED_HISTORY_ACTUAL_PREFIX_REQUIRED')
            expected.append('user')
            if record['assistant_message_present']:expected.append('assistant')
        if [m['role'] for m in messages]!=expected:raise ValueError('FIXED_HISTORY_ACTUAL_ORDER_CHANGED')
    else:raise ValueError('FIXED_HISTORY_INPUT_POLICY_INVALID')
    return messages

def validate_envelope(envelope):
    fields={'profile','case','history_sidecar','history_sha256','sidecar_sha256','qid','lineage_id',
            'base_question_sha256','current_question_sha256','query_derivation_sha256'}
    if not isinstance(envelope,dict) or set(envelope)!=fields or envelope['profile']!=EVALUATOR_PROFILE:
        raise ValueError('FIXED_HISTORY_ENVELOPE_REQUIRED')
    case=envelope['case'];sidecar=envelope['history_sidecar']
    if not isinstance(case,dict):raise ValueError('FIXED_HISTORY_CASE_REQUIRED')
    if (not isinstance(case.get('question'),str) or envelope['current_question_sha256']!=hashlib.sha256(case['question'].encode('utf-8')).hexdigest()):
        raise ValueError('FIXED_HISTORY_CURRENT_USER_CHANGED')
    messages=validate_history_sidecar(sidecar,case.get('question'),base_question_sha256=envelope['base_question_sha256'])
    if any(not isinstance(envelope[k],str) or len(envelope[k])!=64 or any(c not in '0123456789abcdef' for c in envelope[k])
           for k in ('base_question_sha256','current_question_sha256','query_derivation_sha256')):
        raise ValueError('FIXED_HISTORY_QUERY_BINDING_REQUIRED')
    if (envelope['history_sha256']!=history_digest(messages) or envelope['sidecar_sha256']!=history_digest(sidecar)
            or envelope['qid']!=sidecar.get('item_id') or not envelope['lineage_id']):
        raise ValueError('FIXED_HISTORY_ENVELOPE_BINDING_CHANGED')
    return case,messages

def validate_snapshot_history(snapshot):
    if (snapshot.get('evaluator_profile')!=EVALUATOR_PROFILE or snapshot.get('history_profile')!=HISTORY_PROFILE
            or not snapshot.get('qid') or not snapshot.get('lineage_id')
            or not isinstance(snapshot.get('sidecar_sha256'),str) or len(snapshot['sidecar_sha256'])!=64):
        raise ValueError('FIXED_HISTORY_PREPARED_IDENTITY_REQUIRED')
    messages=validate_history_messages(snapshot.get('fixed_history'))
    if snapshot.get('history_sha256')!=history_digest(messages):raise ValueError('FIXED_HISTORY_PREPARED_HASH_CHANGED')
    if snapshot.get('current_question_sha256')!=hashlib.sha256(snapshot['question'].encode('utf-8')).hexdigest():
        raise ValueError('FIXED_HISTORY_CURRENT_USER_CHANGED')
    if any(not isinstance(snapshot.get(k),str) or len(snapshot[k])!=64 for k in ('base_question_sha256','query_derivation_sha256')):
        raise ValueError('FIXED_HISTORY_QUERY_BINDING_REQUIRED')
    return messages
# FIXED_HISTORY_HELPERS_END


def select_member_indices(members, chunks, files, seed_ids, selection, query):
    """Engineering migration of the protected selector; no new strategy."""
    require(selection in ('all', 'exact_duplicate'), 'invalid selection')
    keys = [duplicate_key(m, chunks[m['id']], files[m['file_id']]) for m in members]
    selected = []
    for i, m in enumerate(members):
        remove = (selection == 'exact_duplicate' and m['id'] not in seed_ids and keys[i] is not None
                  and any(keys[j] == keys[i] and (members[j]['id'] in seed_ids or j < i)
                          for j in range(len(members)) if j != i))
        if not remove:
            selected.append(i)
    return selected


def prepare(envelope,fixture_path):
    case,history=validate_envelope(envelope)
    snapshot=legacy.prepare(case,fixture_path)
    snapshot.update(evaluator_profile=EVALUATOR_PROFILE,history_profile=HISTORY_PROFILE,
        fixed_history=copy.deepcopy(history),history_sha256=envelope['history_sha256'],
        sidecar_sha256=envelope['sidecar_sha256'],qid=envelope['qid'],lineage_id=envelope['lineage_id'],
        base_question_sha256=envelope['base_question_sha256'],current_question_sha256=envelope['current_question_sha256'],
        query_derivation_sha256=envelope['query_derivation_sha256'])
    return snapshot


async def deliver(snapshot,config):
    history=validate_snapshot_history(snapshot)
    require(snapshot.get('status')=='OK','invalid snapshot')
    require(config.get('selection') in ('all','exact_duplicate'),'invalid selection')
    seeds=snapshot.get('seed_ids');expanded=snapshot.get('expanded')
    require(isinstance(seeds,list) and isinstance(expanded,list) and len(seeds)==snapshot.get('expected_seed_count'),'invalid snapshot seeds')
    require(bool(snapshot.get('allowed_kb_ids')),'FIXED_HISTORY_SELECTED_KB_SCOPE_REQUIRED')
    if not seeds:require(not expanded,'unexpected expansion for empty seed set')
    files=legacy.unique_map(snapshot.get('files'),'files');chunks=legacy.unique_map(snapshot.get('chunks'),'chunks')
    source_members={}
    for row in expanded:
        require(legacy.context_builder._verified_members(row) is not None,'unverified expansion')
        for member in row['context_group']['source_members']:
            chunk=chunks.get(member['id']);file=files.get(member.get('file_id'))
            require(chunk and file and chunk['file_id']==file['id'] and file['kb_id'] in snapshot['allowed_kb_ids']
                and member.get('kb_id')==file['kb_id'] and member['content']==chunk['content'],'untrusted expanded ownership')
            if member['id'] in source_members:
                prior=source_members[member['id']]
                require(all(prior.get(k)==member.get(k) for k in ('id','file_id','kb_id','content','page_number','parent_id','original_index','heading_text','heading_level')),'conflicting member identity')
            else:source_members[member['id']]=member
    require(all(s in source_members for s in seeds),'seed omitted from expansion')
    original_build=legacy.context_builder.build_chat_context
    captured={'baseline_members':[],'members':[],'removed_ids':[],'selected_citations':[]};calls=[]
    def selection(results,formatter,strategy='EXPANDED_CONTENT'):
        citations,summary=original_build(results,formatter,strategy=strategy)
        if not seeds:
            require(not citations,'zero-seed business branch produced citations')
            return citations,summary
        require(summary['source_mapping_status']=='VERIFIED','baseline mapping unknown')
        ids=[c['chunk_id'] for c in citations]
        require(len(set(ids))==len(ids) and all(s in ids for s in seeds),'baseline seed missing')
        members=[]
        for citation in citations:
            member=source_members.get(citation['chunk_id'])
            require(member and citation['content']==member['content'] and citation['fileId']==member['file_id']
                and citation['kb_id']==member['kb_id'],'baseline member mismatch')
            members.append({**member,'source_locator':chunks[member['id']].get('source_locator')})
        args=(members,chunks,files,seeds,config['selection'],snapshot['question']);before=copy.deepcopy(args)
        selected=select_member_indices(*args)
        require(args==before,'selector mutated input')
        require(isinstance(selected,list) and all(type(i)is int and 0<=i<len(members) for i in selected)
            and selected==sorted(set(selected)) and all(s in [members[i]['id'] for i in selected] for s in seeds),'selector indices or seed invariant failed')
        captured.update(baseline_members=members,members=[members[i] for i in selected],
            removed_ids=[m['id'] for i,m in enumerate(members) if i not in selected],
            selected_citations=[citations[i] for i in selected])
        return [citations[i] for i in selected],summary
    async def record_create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(id='synthetic',model='synthetic',usage=None,choices=[SimpleNamespace(
            message=SimpleNamespace(content='Synthetic response.'),finish_reason='stop')])
    service=legacy.chat.ChatService.__new__(legacy.chat.ChatService)
    service.llm_service=SimpleNamespace(model='synthetic',_get_client=lambda:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=record_create))))
    async def frozen_retrieval(**kwargs):
        return copy.deepcopy(expanded),{'retrieval_status':'SUCCESS' if expanded else 'NO_RESULTS'}
    service._retrieve_with_new_service=frozen_retrieval
    original_session=legacy.chat.Session
    original_spreadsheet=legacy.spreadsheet_grounding.prepare_spreadsheet_grounding
    legacy.chat.Session=legacy.FakeSession
    legacy.spreadsheet_grounding.prepare_spreadsheet_grounding=lambda *args:None
    legacy.context_builder.build_chat_context=selection
    business_input=copy.deepcopy(history)+[{'role':'user','content':snapshot['question']}]
    actual_messages=[legacy.Message(role=legacy.MessageRole(m['role']),content=m['content']) for m in business_input]
    try:
        events=[event async for event in service._chat_stream_normal(actual_messages,snapshot['allowed_kb_ids'],
            max(20,len(seeds)),0,None,False,0,None,search_mode='fulltext',context_window=1,use_rewrite=False)]
    finally:
        legacy.context_builder.build_chat_context=original_build
        legacy.chat.Session=original_session
        legacy.spreadsheet_grounding.prepare_spreadsheet_grounding=original_spreadsheet
    require(events and events[-1][0]=='done','business branch did not finish with actual terminal event')
    actual_input=[{'role':m.role.value,'content':m.content} for m in actual_messages]
    require(actual_input==business_input,'business Message identity changed')
    citations=captured.pop('selected_citations');proof=[];context='';messages=[]
    if seeds:
        require(len(calls)==1,'retrieved business branch did not reach recording provider')
        messages=calls[0]['messages'];prompt=messages[0]['content']
        prefix='请根据以下参考资料回答用户问题。资料不足时请如实说明，并适当引用来源。\n\n资料：\n'
        require(messages[0]['role']=='system' and prompt.startswith(prefix),'unexpected numbered source prompt')
        require(messages[1:]==business_input,'actual provider history/current-user order mismatch')
        context=prompt[len(prefix):]
        expected='\n\n'.join(f"[{i}] 来源：{c.get('fileName','未知')} {c.get('location','')}\n{c['content']}" for i,c in enumerate(citations,1))
        require(context==expected,'actual prompt does not match selected numbered citations')
        cursor=0
        for i,(member,citation) in enumerate(zip(captured['members'],citations),1):
            header=f"[{i}] 来源：{citation.get('fileName','未知')} {citation.get('location','')}\n"
            require(context[cursor:cursor+len(header)]==header,'numbered prompt header mismatch')
            pos=cursor+len(header);text=member['content']
            require(context[pos:pos+len(text)]==text,'delivered member missing from actual prompt')
            proof.append({'id':member['id'],'context_start':pos,'context_end':pos+len(text),
                          'prompt_start':len(prefix)+pos,'prompt_end':len(prefix)+pos+len(text)})
            cursor=pos+len(text)+(2 if i<len(citations) else 0)
    else:
        require(not calls and not citations and events[-1][1].get('answer_mode')=='INSUFFICIENT_EVIDENCE',
                'zero-result selected-KB branch must be actual no-provider insufficient evidence')
    terminal=[{'event':name,'data':copy.deepcopy(data)} for name,data in events if name in {'done','error'}]
    outcome={k:events[-1][1].get(k) for k in ('answer_mode','retrieval_status','generation_status')}
    outcome.update(terminal_event=events[-1][0],provider_calls=len(calls))
    return {'status':'OK',**captured,'messages':messages,'context':context,'request_calls':len(calls),'proof':proof,
        'evaluator_profile':EVALUATOR_PROFILE,'history_profile':HISTORY_PROFILE,'history_sha256':snapshot['history_sha256'],
        'sidecar_sha256':snapshot['sidecar_sha256'],'qid':snapshot['qid'],'lineage_id':snapshot['lineage_id'],
        'base_question_sha256':snapshot['base_question_sha256'],'current_question_sha256':snapshot['current_question_sha256'],
        'query_derivation_sha256':snapshot['query_derivation_sha256'],
        'provider_delivery_status':'PROVIDER_REQUEST_CAPTURED' if calls else 'NO_PROVIDER_REQUEST',
        'business_input_messages':actual_input,'business_input_messages_sha256':history_digest(actual_input),
        'terminal_events':terminal,'business_outcome':outcome,
        'events':[{'event':name,'data':data} for name,data in events],
        'execution_claim':'Actual frozen business branch with existing recording provider stub; no answer-quality or paid-model claim.'}


def main():
    p=argparse.ArgumentParser();commands=p.add_subparsers(dest='command',required=True)
    a=commands.add_parser('prepare');a.add_argument('case');a.add_argument('snapshot')
    a=commands.add_parser('deliver');a.add_argument('snapshot');a.add_argument('config');a.add_argument('capture')
    args=p.parse_args();output=args.snapshot if args.command=='prepare' else args.capture;receipt={}
    try:
        if args.command=='prepare':result=prepare(json.loads(Path(args.case).read_text(encoding='utf-8-sig')),Path(args.case).resolve())
        else:
            snapshot=json.loads(Path(args.snapshot).read_text(encoding='utf-8-sig'))
            config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
            receipt={'upstream_sha256':history_digest(snapshot),'selection':config.get('selection')}
            result=asyncio.run(deliver(snapshot,config))
    except Exception as error:result={'status':'BLOCKED','reason':type(error).__name__+': '+str(error)}
    result.update(receipt);Path(output).write_bytes(json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))


if __name__ == '__main__': main()
