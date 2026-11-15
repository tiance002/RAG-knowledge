"""Bounded model-generated search history; no source/question/Gold forwarding."""
from .contracts import ContractError,object_fields,text,string_list
import re


def validate_history(history):
    if not isinstance(history,list) or len(history)>3:raise ContractError('INVALID_AUTONOMY_HISTORY')
    for item in history:
        object_fields(item,('hypothesis','change','outcome','reason_codes','counts'))
        text(item['hypothesis'],maximum=512);text(item['change'],maximum=512)
        if item['outcome'] not in {'ABSTAIN','PATCH_INVALID','UNIT_FAILED','REJECTED','OBSERVED','VALIDATED','NO_GAIN'}:
            raise ContractError('INVALID_AUTONOMY_HISTORY')
        string_list(item['reason_codes'],limit=8)
        if any(not re.fullmatch('[A-Z0-9_]{1,64}',code) for code in item['reason_codes']):raise ContractError('INVALID_AUTONOMY_HISTORY')
        if (not isinstance(item['counts'],dict) or set(item['counts'])-{'favorable','unfavorable','ties','fact_loss_cases','failed_suites','primary_questions','auxiliary_cases'}
                or any(type(v)is not int or v<0 for v in item['counts'].values())):
            raise ContractError('INVALID_AUTONOMY_HISTORY')
    return history


MEMORY_LIMIT=8


def validate_search_memory(memory):
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
        validate_history([entry['history']])
        if any(len(entry['history'][key])>96 for key in ('hypothesis','change')):
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



def project_search_memory(memory):
    # Frequent failures remain visible across distant rounds; remaining slots show
    # recent distinct candidates. Selection is factual, not an algorithm prescription.
    failures=[e for e in memory['entries'] if e['history']['outcome']!='VALIDATED']
    frequent=sorted(failures,key=lambda e:(-e['attempts'],e['first_round'],e['selector_sha256']))[:4]
    chosen={e['selector_sha256']:e for e in frequent}
    for entry in sorted(memory['entries'],key=lambda e:(-e['last_round'],e['selector_sha256'])):
        if len(chosen)>=MEMORY_LIMIT:break
        chosen.setdefault(entry['selector_sha256'],entry)
    entries=[]
    for entry in chosen.values():
        item={**entry['history']}
        for key in ('hypothesis','change'):item[key]=item[key][:96]
        entries.append({**entry,'history':item})
    return validate_search_memory({**memory,'entries':entries})
