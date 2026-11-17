"""Trusted templating and integrity checks for a NEW fixed-history worker.

Hashes bind already-reviewed input; they do not authenticate arbitrary user
history. The profile adapter must independently bind the actual sidecar proofs.
Only the existing six-argument selector body is transplantable.
"""
import ast
import hashlib
import json
from pathlib import Path

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


def helper_source():
    """Embed these exact trusted pure validators in the standalone capsule file."""
    source=Path(__file__).read_text(encoding='utf-8-sig');tree=ast.parse(source)
    names={'history_digest','validate_history_messages','validate_history_sidecar','validate_envelope','validate_snapshot_history'}
    pieces=[f'EVALUATOR_PROFILE={EVALUATOR_PROFILE!r}',f'HISTORY_PROFILE={HISTORY_PROFILE!r}']
    pieces.extend(ast.get_source_segment(source,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names)
    return '\n\n'.join(pieces)+'\n'


def build_worker(parent_worker=None):
    """Trusted new root template; parent provenance is verified by the caller."""
    from .query_aware import validate_function
    from run_rsi_loop import selector
    path=Path(__file__).resolve().parents[1]/'scripts/rsi_fixed_history_worker_v1.py'
    source=path.read_text(encoding='utf-8-sig')
    begin='# FIXED_HISTORY_HELPERS_BEGIN\n';end='# FIXED_HISTORY_HELPERS_END'
    if source.count(begin)!=1 or source.count(end)!=1:raise ValueError('FIXED_HISTORY_TEMPLATE_MARKERS_CHANGED')
    left=source.index(begin)+len(begin);right=source.index(end)
    source=source[:left]+helper_source()+source[right:]
    _,old=selector(source);validate_function(old)
    if parent_worker is not None:
        _,parent=selector(parent_worker.decode('utf-8-sig'));validate_function(parent)
        source=source.replace(old,parent,1)
    compile(source,'<trusted-fixed-history-worker>','exec')
    return source.encode('utf-8')
