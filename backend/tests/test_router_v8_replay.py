import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
replay = load('replay_test', BASE / 'scripts/ecs_router_v8_replay.py')
runner = load('runner_test', BASE / 'scripts/ecs_dev200_router_validation.py')
overlay = load('overlay_test', BASE / 'scripts/cheap_grounding_contract_v8.py')
retry = load('retry_test', BASE / 'scripts/ecs_dev200_retry503.py')

class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.messages = [{'role':'user','content':'hello'}]
        self.source = self.root / 'source.json'
        self.save_source()
        self.job = {'index': 4, 'item_id':'q', 'role':'cheap', 'prepared_path':str(self.source),
                    'messages_sha256':runner.digest(runner.canonical(self.messages)), 'overlay':True, 'variant':'v8'}
        self.binding = {'id':'id', 'model':'model', 'api_key':'secret'}
        runner.resources = lambda root: {'mem_available': 2**31, 'disk_free': 2**33}
    def tearDown(self): self.tmp.cleanup()
    def save_source(self):
        self.source.write_text(json.dumps({'item_id':'q','status':'PREPARED','messages':self.messages,
            'messages_sha256':runner.digest(runner.canonical(self.messages))}), encoding='utf-8')
    def test_overlay_and_hash(self):
        p = replay.prepare(self.job, runner, overlay, 'model')
        self.assertEqual(p['messages'][:-1], self.messages)
        self.assertEqual(len(self.messages), 1)
        self.job['messages_sha256'] = '0'*64
        with self.assertRaisesRegex(RuntimeError, 'SOURCE_MESSAGE_IDENTITY_MISMATCH'):
            replay.prepare(self.job, runner, overlay, 'model')
    async def test_budget_skips_without_send(self):
        self.messages[0]['content'] = 'x'*32000
        self.save_source()
        self.job['messages_sha256'] = runner.digest(runner.canonical(self.messages))
        result = await replay.process(self.job, self.root, runner, retry, overlay, self.binding, None, asyncio.Semaphore(2), False)
        self.assertEqual(result['status'], 'SKIPPED_BUDGET')
        self.assertFalse((self.root/'attempts').exists())
    async def test_reserved_no_duplicate_and_bad_prior(self):
        p = replay.prepare(self.job, runner, overlay, 'model')
        path = self.root/'attempts/004-cheap.json'
        runner.atomic_json(path, {'index':4, 'item_id':'q', 'role':'cheap', 'messages_sha256':p['messages_sha256'],
                                  'model_binding_id':'id', 'requested_model':'model','state':'RESERVED'})
        result = await replay.process(self.job, self.root, runner, retry, overlay, self.binding, None, asyncio.Semaphore(2), False)
        self.assertEqual(result['status'], 'RESERVED')
        prior = replay.read(path); prior['item_id'] = 'other'; runner.atomic_json(path, prior)
        result = await replay.process(self.job, self.root, runner, retry, overlay, self.binding, None, asyncio.Semaphore(2), False)
        self.assertEqual(result['error_code'], 'PRIOR_ATTEMPT_IDENTITY_MISMATCH')
    async def test_503_only_and_failure_continuation(self):
        calls=[]
        original_send=runner.send_one
        original_retry=retry.retry_item
        async def send(root, prepared, role, binding, client, semaphore):
            if prepared['index'] == 4: raise RuntimeError('ONE_JOB_FAILED')
            return {'state':'UNKNOWN', 'status_code':503}
        async def do_retry(*args, **kwargs):
            calls.append(kwargs['kind']); return 'abandoned', 3, None
        runner.send_one=send; retry.retry_item=do_retry
        try:
            other=dict(self.job,index=5)
            one,two=await asyncio.gather(*(replay.process(j,self.root,runner,retry,overlay,self.binding,None,asyncio.Semaphore(2),False) for j in (self.job,other)))
            self.assertEqual(one['status'],'FAILED')
            self.assertEqual(two['status'],'RETRY_ABANDONED')
            self.assertEqual(calls,['503'])
        finally:
            runner.send_one=original_send; retry.retry_item=original_retry
    async def test_retry_item_three_503_and_unknown_stop(self):
        p = replay.prepare(self.job, runner, overlay, 'model')
        orig={'index':4,'item_id':'q','role':'cheap','messages_sha256':p['messages_sha256'],
              'model_binding_id':'id','requested_model':'model','state':'UNKNOWN','status_code':503}
        path=self.root/'attempts/004-cheap.json'; runner.atomic_json(path,orig)
        class Client:
            def __init__(self):
                self.chat=self; self.completions=self; self.n=0
            async def create(self, **kwargs):
                self.n+=1
                e=Exception('unavailable'); e.status_code=503; raise e
        client=Client()
        async def no_sleep(_): pass
        outcome,sent,_=await retry.retry_item(self.root,path,orig,p,self.binding,client,runner,sleep=no_sleep)
        self.assertEqual((outcome,sent,client.n),('abandoned',3,3))
        outcome,sent,_=await retry.retry_item(self.root,path,orig,p,self.binding,client,runner,sleep=no_sleep)
        self.assertEqual((outcome,sent,client.n),('abandoned',0,3))

    async def test_probe_failure_aborts_all_other_jobs(self):
        jobs=[dict(self.job,index=n) for n in (4,5,6)]
        calls=[]
        old=replay.tracked_process
        async def fake(job,*args):
            calls.append(job['index'])
            return dict(index=job['index'],item_id=job['item_id'],role=job['role'],variant=job['variant'],status='UNKNOWN')
        replay.tracked_process=fake
        try:
            result=await replay.run_cohort(jobs,self.root,runner,retry,overlay,{'cheap':self.binding},{'cheap':None},{'cheap':None},False)
            self.assertEqual(calls,[4])
            self.assertEqual([x['status'] for x in result],['UNKNOWN','NOT_DISPATCHED_PROBE_FAILED','NOT_DISPATCHED_PROBE_FAILED'])
            self.assertEqual(replay.read(self.root/'probe_receipt.json')['status'],'UNKNOWN')
            self.assertTrue((self.root/'outcomes/005-cheap.json').exists())
        finally: replay.tracked_process=old
    async def test_valid_probe_dispatches_rest(self):
        jobs=[dict(self.job,index=n) for n in (4,5,6)]
        calls=[]; old=replay.tracked_process
        async def fake(job,*args):
            calls.append(job['index'])
            return dict(index=job['index'],item_id=job['item_id'],role=job['role'],variant=job['variant'],status='COMPLETED')
        replay.tracked_process=fake
        try:
            result=await replay.run_cohort(jobs,self.root,runner,retry,overlay,{'cheap':self.binding},{'cheap':None},{'cheap':None},False)
            self.assertEqual(calls,[4,5,6])
            self.assertTrue(all(x['status']=='COMPLETED' for x in result))
        finally: replay.tracked_process=old

if __name__ == '__main__': unittest.main()
