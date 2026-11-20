"""Bounded model-generated search history; no source/question/Gold forwarding."""
from .contracts import ContractError,object_fields,text,string_list
import re,math


def validate_history(history):
    if not isinstance(history,list) or len(history)>3:raise ContractError('INVALID_AUTONOMY_HISTORY')
    for item in history:
        object_fields(item,('hypothesis','change','outcome','reason_codes','counts'),
            ('feedback_schema','quality_verdict','execution_status','failure_origin'))
        fields={'feedback_schema','quality_verdict','execution_status','failure_origin'}
        if fields & set(item):
            if not fields <= set(item) or item['feedback_schema']!='rsi-search-feedback/v2':raise ContractError('INVALID_AUTONOMY_HISTORY')
            if item['quality_verdict'] not in {'NOT_EVALUATED','OBSERVED','INCONCLUSIVE','REJECTED','VALIDATED'}:raise ContractError('INVALID_AUTONOMY_HISTORY')
            if item['execution_status'] not in {'NOT_STARTED','COMPLETED','FAILED','PARTIAL','STOPPED','UNKNOWN'}:raise ContractError('INVALID_AUTONOMY_HISTORY')
            if item['failure_origin'] not in {'NONE','CANDIDATE','INFRASTRUCTURE','PARENT','CONTRACT','GLOBAL_STOP','UNKNOWN'}:raise ContractError('INVALID_AUTONOMY_HISTORY')
        text(item['hypothesis'],maximum=512);text(item['change'],maximum=512)
        if item['outcome'] not in {'ABSTAIN','PATCH_INVALID','UNIT_FAILED','REJECTED','OBSERVED','VALIDATED','NO_GAIN','INCONCLUSIVE','NOT_EVALUATED','STOPPED'}:
            raise ContractError('INVALID_AUTONOMY_HISTORY')
        string_list(item['reason_codes'],limit=8)
        if any(not re.fullmatch('[A-Z0-9_]{1,64}',code) for code in item['reason_codes']):raise ContractError('INVALID_AUTONOMY_HISTORY')
        counts=item['counts']
        unsigned={'favorable','unfavorable','ties','fact_loss_cases','failed_suites','primary_questions','auxiliary_cases'}
        if not isinstance(counts,dict) or set(counts)-unsigned-{'delta_total','reduction_rate'}:
            raise ContractError('INVALID_AUTONOMY_HISTORY')
        if any(type(v)is not int or v<0 for k,v in counts.items() if k in unsigned):raise ContractError('INVALID_AUTONOMY_HISTORY')
        if 'delta_total' in counts and type(counts['delta_total']) is not int:raise ContractError('INVALID_AUTONOMY_HISTORY')
        if 'reduction_rate' in counts and (type(counts['reduction_rate']) not in (int,float) or not math.isfinite(counts['reduction_rate'])):raise ContractError('INVALID_AUTONOMY_HISTORY')
    return history


MEMORY_LIMIT=8


def validate_search_memory(memory, *, recent=None):
    object_fields(memory,('schema','rounds','evicted_entries','entries'))
    if memory['schema']!='rsi-autonomy-memory/v1':raise ContractError('INVALID_AUTONOMY_MEMORY')
    if any(type(memory[k]) is not int or memory[k]<0 for k in ('rounds','evicted_entries')):
        raise ContractError('INVALID_AUTONOMY_MEMORY')
    entries=memory['entries']
    if not isinstance(entries,list) or len(entries)>MEMORY_LIMIT:raise ContractError('INVALID_AUTONOMY_MEMORY')
    seen=set()
    for entry in entries:
        object_fields(entry,('selector_sha256','first_round','last_round','attempts','history'))
        key=entry['selector_sha256']
        if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key) or key in seen:
            raise ContractError('INVALID_AUTONOMY_MEMORY')
        seen.add(key)
        if any(type(entry[k]) is not int or entry[k]<1 for k in ('first_round','last_round','attempts')):
            raise ContractError('INVALID_AUTONOMY_MEMORY')
        if not entry['first_round']<=entry['last_round']<=memory['rounds'] or entry['attempts']>3*memory['rounds']:
            raise ContractError('INVALID_AUTONOMY_MEMORY')
        history=entry['history']
        if isinstance(history,dict) and set(history)=={'recent_history_index'}:
            index=history['recent_history_index']
            if type(index)is not int or index<0 or (recent is not None and index>=len(recent)):
                raise ContractError('INVALID_AUTONOMY_MEMORY')
        else:
            validate_history([history])
            if any(len(history[key])>96 for key in ('hypothesis','change')):
                raise ContractError('INVALID_AUTONOMY_MEMORY')
    return memory


def remember(memory,key,round_number,item):
    """Persist all fingerprints locally; model receives only a bounded projection."""
    validate_history([item])
    entries=memory['entries']
    previous=next((entry for entry in entries if entry['selector_sha256']==key),None)
    if previous:entries.remove(previous)
    entry={'selector_sha256':key,'first_round':previous['first_round'] if previous else round_number,
           'last_round':round_number,'attempts':previous['attempts']+1 if previous else 1,'history':item}
    entries.append(entry)
    memory['rounds']=round_number
    return entry



def _confirmed_negative(item):
    if item.get('failure_origin') in {'INFRASTRUCTURE', 'PARENT', 'GLOBAL_STOP', 'UNKNOWN'}:
        return False
    codes = set(item['reason_codes'])
    if item['outcome'] == 'PATCH_INVALID':
        return any(code.startswith(('STATIC_', 'AST_')) for code in codes)
    if item['outcome'] == 'UNIT_FAILED':
        return 'QUERY_UNIT_FAILED' in codes and item.get('failure_origin') == 'CANDIDATE'
    return (item['outcome'] == 'REJECTED' and item.get('quality_verdict') == 'REJECTED'
            and item.get('execution_status') == 'COMPLETED' and item.get('failure_origin') == 'CANDIDATE'
            and bool(codes & {'FACT_LOSS', 'LOCAL_REGRESSION', 'EXEC_FAULT'}))


def project_search_memory(memory, *, recent=()):
    """Project scoped evidence; process failures remain in the local archive."""
    recent=list(recent)
    candidates = list(memory['entries'])
    failures = [e for e in candidates if _confirmed_negative(e['history'])]
    frequent=sorted(failures,key=lambda e:(-e['attempts'],e['first_round'],e['selector_sha256']))[:4]
    chosen={e['selector_sha256']:e for e in frequent}
    observations = [e for e in candidates if _confirmed_negative(e['history']) or (
        e['history']['outcome'] in {'NO_GAIN', 'OBSERVED', 'VALIDATED'}
        and e['history'].get('execution_status', 'COMPLETED') == 'COMPLETED'
        and e['history'].get('failure_origin', 'NONE') == 'NONE')]
    for entry in sorted(observations,key=lambda e:(-e['last_round'],e['selector_sha256'])):
        if len(chosen)>=MEMORY_LIMIT:break
        chosen.setdefault(entry['selector_sha256'],entry)
    entries=[]
    for entry in chosen.values():
        try:
            recent_index=recent.index(entry['history'])
        except ValueError:
            recent_index=None
        item={**entry['history']}
        for key in ('hypothesis','change'):item[key]=item[key][:96]
        if recent_index is not None:
            item={'recent_history_index':recent_index}
        entries.append({**entry,'history':item})
    return validate_search_memory({**memory,'entries':entries},recent=recent)
