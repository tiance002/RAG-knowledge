"""Independent six-argument experimental seam, never an old-registry candidate.

The trusted adapter forwards only the runtime question. The strict body guard is
reused, while the old five-argument seam and six protected files stay unchanged.
"""
import ast
from pathlib import Path

from .experiment import bytes_hash,registry_api
from .foundation import verify_boundary
from .locking import atomic_write
from .selector_contract import interface_contract as legacy_contract
from .state import canonical,digest
from .events import EventLog
from run_rsi_loop import selector,validate_policy

PROFILE='rsi-query-aware-selector/v1'
ARGS=['members','chunks','files','seed_ids','selection','query']
OLD_CALL="select_member_indices(members, chunks, files, seeds, config['selection'])"
NEW_CALL="select_member_indices(members, chunks, files, seeds, config['selection'], snapshot['question'])"


def interface_contract():
    result=legacy_contract()
    result.update(schema_version=PROFILE,query='actual runtime question string; read-only; no Gold or evaluation labels',
                  signature='select_member_indices(members, chunks, files, seed_ids, selection, query)',
                  learning_scope='independent query-aware engineering exploration; not old-registry admission or population significance')
    return result


def autonomy_interface_contract():
    result=interface_contract()
    result.update(schema_version='rsi-query-autonomous-selector/v1',
        local_mutation='Only scalar rebinding and append to a fresh private list. All attribute/subscript stores are forbidden, including writes into a newly-created dict.',
        learning_scope='frozen autonomous target-policy search; no prescribed optimization strategy and no system self-modification')
    return result


def validate_function(source):
    tree=ast.parse(source)
    if len(tree.body)!=1 or not isinstance(tree.body[0],ast.FunctionDef):raise ValueError('ONE_SELECTOR_REQUIRED')
    fn=tree.body[0]
    if (fn.name!='select_member_indices' or [a.arg for a in fn.args.args]!=ARGS
            or fn.args.defaults or fn.args.kwonlyargs or fn.args.posonlyargs or fn.args.vararg or fn.args.kwarg
            or fn.decorator_list or fn.returns or any(a.annotation for a in fn.args.args)):
        raise ValueError('QUERY_SEAM_SIGNATURE_CHANGED')
    validate_policy(fn)
    return fn


def adapt_legacy(worker):
    verify_boundary()
    source=worker.decode();_,old=selector(source)
    new=old.replace('seed_ids, selection):','seed_ids, selection, query):',1)
    validate_function(new)
    if source.count(OLD_CALL)!=1:raise ValueError('QUERY_ADAPTER_CALL_AMBIGUOUS')
    adapted=source.replace(old,new,1).replace(OLD_CALL,NEW_CALL,1).encode()
    compile(adapted,'<query-aware-adapter>','exec')
    return adapted


def apply_patch(worker,patch):
    if set(patch)!={'old','new'} or any(not isinstance(v,str) or not v.strip() for v in patch.values()):
        raise ValueError('ONE_QUERY_PATCH_REQUIRED')
    source=worker.decode();_,old=selector(source);validate_function(old)
    eol='\r\n' if '\r\n' in old else '\n'
    before,after=[patch[k].replace('\r\n','\n').replace('\n',eol) for k in ('old','new')]
    if old.count(before)!=1 or before==after:raise ValueError('QUERY_PATCH_NOT_UNIQUE')
    new=old.replace(before,after,1);validate_function(new)
    changed=source.replace(old,new,1).encode()
    compile(changed,'<query-aware-candidate>','exec')
    return changed


def invariant_probe_source(worker):
    """Emit a standalone probe worker; generated policies never execute on host."""
    _,code=selector(worker.decode());validate_function(code)
    prefix='import copy,json,sys\ndef require(value,reason):\n    if not value:raise ValueError(reason)\ndef duplicate_key(*args):return None\n'
    probes='''
def run_probes():
    count=0
    for query in ('','x','xy','abc xyz','中文测试问题'):
        for size in (0,1,2,6):
            for seed_pos in range(max(1,size)):
                for selection in ('all','exact_duplicate'):
                    members=[{'id':str(i),'file_id':'f','kb_id':'k','content':'abcxyz中文'+('x' if i%2 else 'x'*400)} for i in range(size)]
                    chunks={m['id']:{**m,'original_index':i} for i,m in enumerate(members)}
                    seeds=[] if not size else [members[seed_pos]['id']]
                    args=(members,chunks,{'f':{'id':'f','kb_id':'k','name':'f.txt'}},seeds,selection,query)
                    before=copy.deepcopy(args);indices=select_member_indices(*args)
                    if (args!=before or not isinstance(indices,list) or any(type(i)is not int or i<0 or i>=size for i in indices)
                            or indices!=sorted(set(indices)) or any(s not in [members[i]['id'] for i in indices] for s in seeds)):
                        raise ValueError('QUERY_SELECTOR_INVARIANT_FAILED')
                    if seeds and len(query)<2 and indices!=list(range(size)):
                        raise ValueError('QUERY_SHORT_INPUT_FALLBACK_FAILED')
                    count+=1
    return {'status':'PASS','probes':count,'scope':'synthetic invariants, not source facts'}
with open(sys.argv[-1],'x') as output:json.dump(run_probes(),output)
'''
    return (prefix+code+'\n'+probes).encode()


class QueryRegistry:
    def __init__(self,directory):self.directory=Path(directory)

    def quarantine(self,cid,receipt_path,reason):
        self.get(cid)
        EventLog(self.directory/'policy.sqlite').append('quarantine:'+cid,'QUERY_CANDIDATE_QUARANTINED',
            {'candidate_id':cid,'receipt_path':str(Path(receipt_path).resolve()),
             'receipt_sha256':bytes_hash(Path(receipt_path).read_bytes()),'reason':reason})
        links=self.directory/'learning-links.sqlite'
        if links.exists():
            from .handbook.store import Handbook
            from .query_learning import quarantine_candidate_learning
            for event in EventLog(links).read():
                item=event['payload']
                if item.get('candidate_id')==cid:
                    quarantine_candidate_learning(Handbook(item['handbook'],evidence_mode=item['evidence_mode']),cid,
                        evidence_hash=bytes_hash(Path(receipt_path).read_bytes()),reason=reason)

    def require_not_quarantined(self,cid):
        if any(e['event_type']=='QUERY_CANDIDATE_QUARANTINED' and e['payload']['candidate_id']==cid
               for e in EventLog(self.directory/'policy.sqlite').read()):raise ValueError('QUERY_PARENT_QUARANTINED')

    def put(self,worker,config,*,parent_id,origin,evidence):
        _,code=selector(worker.decode());validate_function(code)
        record={'schema':PROFILE,'parent_id':parent_id,'origin':origin,'worker_sha256':bytes_hash(worker),
                'config_sha256':bytes_hash(config),'evidence':evidence,'production_changed':False}
        cid=digest(record)
        for sub,key,data in [('workers',record['worker_sha256'],worker),('configs',record['config_sha256'],config),('candidates',cid,canonical(record))]:
            path=self.directory/sub/key
            if path.exists():
                if path.read_bytes()!=data:raise ValueError('QUERY_ARTIFACT_CHANGED')
            else:atomic_write(path,data,exclusive=True)
        return cid

    def get(self,cid):
        if len(cid)!=64 or any(c not in '0123456789abcdef' for c in cid):raise ValueError('INVALID_QUERY_CANDIDATE_ID')
        raw=(self.directory/'candidates'/cid).read_bytes()
        if bytes_hash(raw)!=cid:raise ValueError('QUERY_CANDIDATE_CHANGED')
        import json
        record=json.loads(raw)
        if record.get('schema')!=PROFILE:raise ValueError('QUERY_CANDIDATE_PROFILE_MISMATCH')
        worker=(self.directory/'workers'/record['worker_sha256']).read_bytes()
        config=(self.directory/'configs'/record['config_sha256']).read_bytes()
        if bytes_hash(worker)!=record['worker_sha256'] or bytes_hash(config)!=record['config_sha256']:
            raise ValueError('QUERY_ARTIFACT_CHANGED')
        validate_function(selector(worker.decode())[1])
        return {'candidate_id':cid,'worker':worker,'config':config,'record':record}
