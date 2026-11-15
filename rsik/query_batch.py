"""Diagnostic snapshot replay batch; never substitutes for admission evidence.

Only source snapshots/configs enter the capsule, never evaluation predicates.
Each item still calls the unchanged business deliver path. Model selector bodies
remain read-only and cannot share state. Qualified adoption uses the ordinary
per-case evidence path until a separate batch evidence contract is implemented.
"""
from .query_aware import validate_function
from run_rsi_loop import selector

GUARD="if __name__ == '__main__': main()"
BATCH='''
def trusted_batch_main():
    request=json.loads(Path(sys.argv[-2]).read_text(encoding='utf-8'))
    captures=[]
    for item in request['entries']:
        snapshot,config=item['snapshot'],item['config']
        try:
            result=asyncio.run(deliver(snapshot,config))
        except Exception as error:
            result={'status':'BLOCKED','reason':type(error).__name__}
        result.update(upstream_sha256=hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),selection=config.get('selection'))
        captures.append({'key':item['key'],'capture':result})
    Path(sys.argv[-1]).write_text(json.dumps({'status':'OK','captures':captures},ensure_ascii=False),encoding='utf-8')
trusted_batch_main()
'''


def diagnostic_worker(worker):
    source=worker.decode();validate_function(selector(source)[1])
    if source.count(GUARD)!=1:raise ValueError('BATCH_ENTRYPOINT_AMBIGUOUS')
    changed=source.replace(GUARD,'',1)+'\n'+BATCH
    compile(changed,'<trusted-batch>','exec')
    return changed.encode()
