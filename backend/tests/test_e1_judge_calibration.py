import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'e1_judge_calibration.py'
spec = importlib.util.spec_from_file_location('e1_judge_calibration', SCRIPT)
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)


class E1JudgePreflightTests(unittest.TestCase):
    def test_real_inputs_are_allowlisted_and_consistent(self):
        items = judge.build_inputs(Path('var/e1-20261029'))
        self.assertEqual(len(items), 24)
        for item in items:
            self.assertEqual(set(item['input']), {'question', 'gold', 'answer', 'evidence'})
            self.assertEqual(item['input_sha256'], judge.digest(judge.canonical(item['input'])))
            self.assertTrue(item['input']['evidence'].startswith('[1]'))
            self.assertNotIn('human_core', item['input'])
            self.assertNotIn('ai_citation_audit', item['input'])

    def test_sse_error_and_missing_done_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'response.sse'
            p.write_text('event: error\ndata: {}\n\n', encoding='utf8')
            with self.assertRaises(ValueError): judge.extract_sse(p)
            p.write_text('event: answer_chunk\ndata: {"content":"a"}\n\n', encoding='utf8')
            with self.assertRaises(ValueError): judge.extract_sse(p)

    def make_db(self, path):
        c = sqlite3.connect(path, isolation_level=None)
        c.execute("CREATE TABLE IF NOT EXISTS calls (qid TEXT PRIMARY KEY, input_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL, state TEXT NOT NULL, raw BLOB, usage TEXT, http_status INTEGER)")
        return c

    def test_unknown_persists_and_blocks_restart_without_retry(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'journal.sqlite3'; c = self.make_db(path)
            body = {'question': 'q', 'gold': ['g'], 'answer': 'a', 'evidence': '[1] x'}
            item = {'qid': 'q1', 'input': body, 'input_sha256': judge.digest(judge.canonical(body))}
            calls = []
            def timeout(config, payload):
                calls.append(payload)
                raise TimeoutError('controlled')
            with self.assertRaises(TimeoutError): judge.run_one(c, item, {}, judge.digest(judge.PROMPT.encode()), timeout)
            c.close(); c = self.make_db(path)
            self.assertEqual(c.execute('select state from calls').fetchone()[0], 'UNKNOWN')
            second = dict(item, qid='q2')
            with self.assertRaisesRegex(RuntimeError, 'UNKNOWN_STOP'): judge.run_one(c, second, {}, judge.digest(judge.PROMPT.encode()), timeout)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(b'human_core', calls[0]); self.assertNotIn(b'ai_citation_audit', calls[0])
            c.close()

    def test_continuation_preserves_baseline_and_blocks_second_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            baseline={'qid':'old','input_hash':'old_input','prompt_hash':'old_prompt','allowed_qids':{'new','next'}}
            original=('old','old_input','old_prompt','UNKNOWN',None,None,None)
            c.execute('insert into calls values(?,?,?,?,?,?,?)', original)
            value={'question':'q','gold':['g'],'answer':'a','evidence':'e'}
            make=lambda q:{'qid':q,'input':value,'input_sha256':judge.digest(judge.canonical(value))}
            sends=[]
            def good(*args):
                sends.append(1)
                return 200,json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"core":"PASS","faithfulness":"PASS","citation_support":"PASS"}'}}]}).encode()
            ph=judge.digest(judge.PROMPT.encode())
            with self.assertRaisesRegex(RuntimeError, 'allowlist'): judge.run_one(c,make('old'),{},ph,good,baseline)
            with self.assertRaisesRegex(RuntimeError, 'allowlist'): judge.run_one(c,make('other'),{},ph,good,baseline)
            self.assertEqual(judge.run_one(c,make('new'),{},ph,good,baseline),'COMPLETED')
            with self.assertRaisesRegex(RuntimeError,'replay denied'): judge.run_one(c,make('new'),{},ph,good,baseline)
            self.assertEqual(c.execute('select * from calls where qid="old"').fetchone(),original)
            def timeout(*args):sends.append(1);raise TimeoutError()
            with self.assertRaises(TimeoutError): judge.run_one(c,make('next'),{},ph,timeout,baseline)
            with self.assertRaisesRegex(RuntimeError,'baseline changed or new UNKNOWN'): judge.run_one(c,make('new'),{},ph,good,baseline)
            self.assertEqual(c.execute('select * from calls where qid="old"').fetchone(),original)
            self.assertEqual(len(sends),2);c.close()

    def test_final_two_baselines_no_replay_and_new_unknown_stops(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3');raw=b'429';unknown1=('old','ih','ph','UNKNOWN',None,None,None);unknown2=('old2','ih2','ph2','UNKNOWN',None,None,None);failed=('failed','fi','fp','FAILED',raw,'null',429)
            for row in (unknown1,unknown2,failed): c.execute('insert into calls values(?,?,?,?,?,?,?)',row)
            for n in range(19):c.execute('insert into calls values(?,?,?,?,?,?,?)',(f'prior{n}','i','p','COMPLETED',None,None,200))
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','second_unknown':('old2','ih2','ph2'),'failed':('failed','fi','fp',judge.digest(raw)),'allowed_qids':{'next','last'}}
            judge.check_unknown_guard(c,base)
            with self.assertRaisesRegex(RuntimeError,'UNKNOWN_STOP'):judge.check_unknown_guard(c,dict(base,second_unknown=('old2','bad','ph2')))
            v={'question':'q','gold':['g'],'answer':'a','evidence':'e'};mk=lambda q:{'qid':q,'input':v,'input_sha256':judge.digest(judge.canonical(v))}
            sends=[]
            def timeout(*a):sends.append(1);raise TimeoutError()
            with self.assertRaisesRegex(RuntimeError,'allowlist'):judge.run_one(c,mk('old2'),{},judge.digest(judge.PROMPT.encode()),timeout,base)
            with self.assertRaises(TimeoutError):judge.run_one(c,mk('next'),{},judge.digest(judge.PROMPT.encode()),timeout,base)
            with self.assertRaisesRegex(RuntimeError,'UNKNOWN_STOP'):judge.run_one(c,mk('last'),{},judge.digest(judge.PROMPT.encode()),timeout,base)
            for row in (unknown1,unknown2,failed):self.assertEqual(c.execute('select * from calls where qid=?',(row[0],)).fetchone(),row)
            self.assertEqual(len(sends),1);c.close()

    def test_final_two_known_failure_stops_and_total_cap_counts_all(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3');raw=b'429'
            for row in [('old','ih','ph','UNKNOWN',None,None,None),('old2','ih2','ph2','UNKNOWN',None,None,None),('failed','fi','fp','FAILED',raw,'null',429)]:c.execute('insert into calls values(?,?,?,?,?,?,?)',row)
            for n in range(19):c.execute('insert into calls values(?,?,?,?,?,?,?)',(f'prior{n}','i','p','COMPLETED',None,None,200))
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','second_unknown':('old2','ih2','ph2'),'failed':('failed','fi','fp',judge.digest(raw)),'allowed_qids':{'next','last'}}
            v={'question':'q','gold':['g'],'answer':'a','evidence':'e'};mk=lambda q:{'qid':q,'input':v,'input_sha256':judge.digest(judge.canonical(v))};sends=[]
            def failure(*a):sends.append(1);return 429,b'new'
            self.assertEqual(judge.run_one(c,mk('next'),{},judge.digest(judge.PROMPT.encode()),failure,base),'FAILED')
            with self.assertRaisesRegex(RuntimeError,'new failure'):judge.run_one(c,mk('last'),{},judge.digest(judge.PROMPT.encode()),failure,base)
            self.assertEqual(len(sends),1);c.close()
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3')
            for row in [('old','ih','ph','UNKNOWN',None,None,None),('old2','ih2','ph2','UNKNOWN',None,None,None),('failed','fi','fp','FAILED',raw,'null',429)]:c.execute('insert into calls values(?,?,?,?,?,?,?)',row)
            for n in range(21):c.execute('insert into calls values(?,?,?,?,?,?,?)',(f'prior{n}','i','p','COMPLETED',None,None,200))
            with self.assertRaisesRegex(RuntimeError,'cap reached'):judge.reserve(c,{'qid':'last','input_sha256':'i'},'p',base)
            c.close()

    def test_continuation_known_429_baseline_preserved_and_new_failure_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3')
            raw=b'{"code":50609}'; failed=('failed','fi','fp','FAILED',raw,'null',429)
            original=('old','ih','ph','UNKNOWN',None,None,None)
            c.execute('insert into calls values(?,?,?,?,?,?,?)',original)
            c.execute('insert into calls values(?,?,?,?,?,?,?)',failed)
            for i in range(15):c.execute('insert into calls values(?,?,?,?,?,?,?)',(f'h{i}','i','p','COMPLETED',None,None,200))
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','allowed_qids':{'new','after'},'failed':('failed','fi','fp',judge.digest(raw))}
            v={'question':'q','gold':['g'],'answer':'a','evidence':'e'}
            item=lambda q:{'qid':q,'input':v,'input_sha256':judge.digest(judge.canonical(v))}
            calls=[]
            def fake(*a):calls.append(1);return 429,b'{"code":50609}'
            ph=judge.digest(judge.PROMPT.encode())
            self.assertEqual(judge.run_one(c,item('new'),{},ph,fake,base),'FAILED')
            with self.assertRaisesRegex(RuntimeError,'new failure'):judge.run_one(c,item('after'),{},ph,fake,base)
            self.assertEqual(len(calls),1)
            self.assertEqual(c.execute('select * from calls where qid="old"').fetchone(),original)
            self.assertEqual(c.execute('select * from calls where qid="failed"').fetchone(),failed)
            c.close()

    def test_known_429_baseline_sdk_blocks_following_new_failure(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3');raw=b'old429'
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('old','ih','ph','UNKNOWN',None,None,None))
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('failed','fi','fp','FAILED',raw,'null',429))
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','allowed_qids':{'x','y'},'failed':('failed','fi','fp',judge.digest(raw))}
            value={'question':'q','gold':['g'],'answer':'a','evidence':'e'}
            items=[{'qid':q,'input':value,'input_sha256':judge.digest(judge.canonical(value))} for q in ('x','y')]
            sends=[]
            def fake(*args):sends.append(1);return 429,b'new429'
            data,task,_=judge.make_experiment_adapter(items,c,{},judge.digest(judge.PROMPT.encode()),{},fake,base)
            self.assertEqual(task(item=data[0]),{'status':'FAILED'})
            self.assertEqual(task(item=data[1]),{'status':'STOPPED'})
            self.assertEqual(len(sends),1);c.close()

    def test_continuation_rejects_failed_baseline_drift_and_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3');raw=b'body'
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('old','ih','ph','UNKNOWN',None,None,None))
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('failed','fi','fp','FAILED',raw,'null',429))
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','allowed_qids':{'new'},'failed':('failed','fi','fp',judge.digest(raw))}
            judge.check_unknown_guard(c,base)
            for altered in [('other','fi','fp',judge.digest(raw)),('failed','bad','fp',judge.digest(raw)),('failed','fi','bad',judge.digest(raw)),('failed','fi','fp','bad')]:
                with self.assertRaisesRegex(RuntimeError,'FAILED baseline'):judge.check_unknown_guard(c,dict(base,failed=altered))
            with self.assertRaisesRegex(RuntimeError,'HTTP failure'):judge.check_unknown_guard(c,dict(base,failed=None))
            for i in range(22):c.execute('insert into calls values(?,?,?,?,?,?,?)',(str(i),'i','p','COMPLETED',None,None,200))
            with self.assertRaisesRegex(RuntimeError,'cap reached'):judge.reserve(c,{'qid':'new','input_sha256':'i'},'p',base)
            self.assertEqual(len(judge.CONTINUATION_SEVEN_QIDS),7)
            self.assertNotIn('ecs-table-probe/v2:120',judge.CONTINUATION_SEVEN_QIDS)
            c.close()

    def test_continuation_sdk_isolation_and_two_connections(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'j.sqlite3'; c=self.make_db(path); other=self.make_db(path)
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','allowed_qids':{'x','y'}}
            original=('old','ih','ph','UNKNOWN',None,None,None);c.execute('insert into calls values(?,?,?,?,?,?,?)',original)
            v={'question':'q','gold':['g'],'answer':'a','evidence':'e'}
            items=[{'qid':q,'input':v,'input_sha256':judge.digest(judge.canonical(v))} for q in ('x','y')]
            calls=[]
            def timeout(*a):calls.append(1);raise TimeoutError()
            data,task,_=judge.make_experiment_adapter(items,c,{},judge.digest(judge.PROMPT.encode()),{},timeout,base)
            self.assertEqual(task(item=data[0]),{'status':'STOPPED'})
            self.assertEqual(task(item=data[1]),{'status':'STOPPED'})
            with self.assertRaisesRegex(RuntimeError,'UNKNOWN_STOP'):judge.reserve(other,items[1],'ph',base)
            self.assertEqual(len(calls),1);self.assertEqual(c.execute('select * from calls where qid="old"').fetchone(),original)
            other.close();c.close()

    def test_continuation_rejects_changed_baseline_failed_and_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3')
            base={'qid':'old','input_hash':'ih','prompt_hash':'ph','allowed_qids':{'new'}}
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('old','ih','ph','UNKNOWN',None,None,None))
            judge.check_unknown_guard(c,base)
            for altered in [dict(base,input_hash='other'),dict(base,prompt_hash='other'),dict(base,qid='other')]:
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN_STOP'):judge.check_unknown_guard(c,altered)
            c.execute('insert into calls values(?,?,?,?,?,?,?)',('failed','i','p','FAILED',None,None,500))
            with self.assertRaisesRegex(RuntimeError,'HTTP failure'):judge.check_unknown_guard(c,base)
            c.execute('delete from calls where qid="failed"')
            for i in range(23):c.execute('insert into calls values(?,?,?,?,?,?,?)',(str(i),'i','p','COMPLETED',None,None,200))
            with self.assertRaisesRegex(RuntimeError,'cap reached'):judge.reserve(c,{'qid':'new','input_sha256':'i'},'p',base)
            c.close()

    def test_completed_no_replay_and_cap(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            body = {'question': 'q', 'gold': ['g'], 'answer': 'a', 'evidence': '[1] x'}
            item = {'qid': 'q1', 'input': body, 'input_sha256': judge.digest(judge.canonical(body))}
            calls = []
            def fake(config, payload):
                calls.append(payload)
                return 200, json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"core":"PASS","faithfulness":"UNKNOWN","citation_support":"PASS"}'}}], 'usage':{'total_tokens': 3}}).encode()
            self.assertEqual(judge.run_one(c, item, {}, judge.digest(judge.PROMPT.encode()), fake), 'COMPLETED')
            with self.assertRaisesRegex(RuntimeError, 'replay denied'): judge.run_one(c, item, {}, judge.digest(judge.PROMPT.encode()), fake)
            for n in range(2,25): c.execute("insert into calls(qid,input_hash,prompt_hash,state) values(?,?,?,?)", (str(n),'h','h','COMPLETED'))
            with self.assertRaisesRegex(RuntimeError, 'cap reached'): judge.run_one(c, dict(item,qid='extra'), {}, judge.digest(judge.PROMPT.encode()), fake)
            self.assertEqual(len(calls), 1)
            c.close()

    def test_transport_issues_one_send_and_rejects_unframed(self):
        class Response:
            length = None
            chunked = False
            status = 200
        class Connection:
            calls = 0
            def __init__(self, *a, **kw): pass
            def request(self, *a, **kw): type(self).calls += 1
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(judge.http.client, 'HTTPSConnection', Connection):
            with self.assertRaisesRegex(RuntimeError, 'unframed'):
                judge.https_send({'host':'example.test','port':443,'path':'/v1/chat/completions','key':'x'}, b'{}')
        self.assertEqual(Connection.calls, 1)

    def test_prompt_mismatch_blocks_before_reserve_or_send(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            value = {'question':'q', 'gold':['g'], 'answer':'a', 'evidence':'[1] e'}
            item = {'qid':'x', 'input':value, 'input_sha256':judge.digest(judge.canonical(value))}
            sends = []
            with self.assertRaisesRegex(RuntimeError, 'prompt differs'):
                judge.run_one(c, item, {}, 'incorrect', lambda *args: sends.append(args))
            self.assertEqual(sends, [])
            self.assertEqual(c.execute('select count(*) from calls').fetchone()[0], 0)
            c.close()

    def test_known_outcomes_skip_to_next_case_without_replay(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            value = {'question':'q', 'gold':['g'], 'answer':'a', 'evidence':'[1] e'}
            items = [{'qid':str(i), 'input':value, 'input_sha256':judge.digest(judge.canonical(value))} for i in range(4)]
            results = [('length','x'), ('stop',''), ('stop','bad'), ('stop','{"core":"PASS","faithfulness":"PASS","citation_support":"PASS"}')]
            sends = []
            def fake(config, body):
                reason, content = results[len(sends)]; sends.append(body)
                return 200, json.dumps({'choices':[{'finish_reason':reason,'message':{'content':content}}]}).encode()
            judge.run_cases(c, items, {}, judge.digest(judge.PROMPT.encode()), fake)
            self.assertEqual([r[0] for r in c.execute('select state from calls order by qid')], ['TRUNCATED','EMPTY','INVALID','COMPLETED'])
            judge.run_cases(c, items, {}, judge.digest(judge.PROMPT.encode()), fake)
            self.assertEqual(len(sends), 4)
            c.close()

    def test_fenced_json_only_and_raw_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            value = {'question':'q', 'gold':['g'], 'answer':'a', 'evidence':'[1] e'}
            item = {'qid':'1','input':value,'input_sha256':judge.digest(judge.canonical(value))}
            good = '{"core":"PASS","faithfulness":"PASS","citation_support":"UNKNOWN"}'
            def payload(content): return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':content}}]}).encode()
            raw = payload('```json\n'+good+'\n```')
            self.assertEqual(judge.run_one(c, item, {}, judge.digest(judge.PROMPT.encode()), lambda *_:(200,raw)), 'COMPLETED')
            self.assertEqual(c.execute('select raw from calls').fetchone()[0], raw)
            self.assertEqual(judge.classify(200, payload('text\n```json\n'+good+'\n```'))[0], 'INVALID')
            self.assertEqual(judge.classify(200, payload('```json\n'+good+'\n```\nmore'))[0], 'INVALID')
            c.close()

    def test_experiment_mock_privacy_and_unknown_stops_all_sends(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            value = {'question':'PRIVATE_QUESTION','gold':['PRIVATE_GOLD'],'answer':'PRIVATE_ANSWER','evidence':'PRIVATE_EVIDENCE'}
            items = [{'qid':'secret-qid-'+str(i), 'input':value, 'input_sha256':judge.digest(judge.canonical(value))} for i in range(3)]
            sends = []
            def timeout(config, body):
                sends.append(body); raise TimeoutError('PRIVATE_EXCEPTION')
            data, task, evaluator = judge.make_experiment_adapter(items,c,{},judge.digest(judge.PROMPT.encode()), {'secret-qid-0':'PASS'},timeout)
            class FakeSDK:
                def run_experiment(self, *, data, task, evaluators, max_concurrency):
                    self.captured = {'data':data,'outputs':[], 'scores':[], 'concurrency':max_concurrency}
                    for item in data:
                        output = task(item=item)
                        self.captured['outputs'].append(output)
                        self.captured['scores'].append(evaluators[0](input=item['input'],output=output))
            sdk = FakeSDK(); sdk.run_experiment(data=data, task=task, evaluators=[evaluator], max_concurrency=1)
            self.assertEqual(len(sends),1)
            self.assertEqual([v['status'] for v in sdk.captured['outputs']], ['STOPPED']*3)
            encoded=json.dumps(sdk.captured)
            for secret in ['PRIVATE_QUESTION','PRIVATE_GOLD','PRIVATE_ANSWER','PRIVATE_EVIDENCE','PRIVATE_EXCEPTION','secret-qid']:
                self.assertNotIn(secret, encoded)
            self.assertEqual(c.execute('select state from calls').fetchone()[0], 'UNKNOWN')
            self.assertEqual(judge.experiment_mask({'question':'PRIVATE_QUESTION','input':'ab'*32}), {'[REDACTED]':'[REDACTED]','input':'ab'*32})
            c.close()

    def test_experiment_completed_returns_only_labels(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.make_db(Path(d)/'j.sqlite3')
            value={'question':'PRIVATE','gold':['G'],'answer':'A','evidence':'E'}
            item={'qid':'qid','input':value,'input_sha256':judge.digest(judge.canonical(value))}
            content={'core':'PASS','faithfulness':'UNKNOWN','citation_support':'FAIL','reasoning':'PRIVATE_REASON'}
            raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(content)}}]}).encode()
            data,task,evaluator=judge.make_experiment_adapter([item],c,{},judge.digest(judge.PROMPT.encode()),{'qid':'PASS'},lambda *_:(200,raw))
            result=task(item=data[0])
            self.assertEqual(result,{'status':'COMPLETED','labels':{'core':'PASS','faithfulness':'UNKNOWN','citation_support':'FAIL'}})
            self.assertEqual(evaluator(input=data[0]['input'],output=result),{'name':'core_agreement','value':1})
            self.assertNotIn('PRIVATE_REASON',json.dumps(result));c.close()

    def test_experiment_evaluator_skips_unknown_without_null_score(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.make_db(Path(d) / 'j.sqlite3')
            value = {'question':'q','gold':['g'],'answer':'a','evidence':'e'}
            item = {'qid':'qid','input':value,'input_sha256':judge.digest(judge.canonical(value))}
            data, _, evaluate = judge.make_experiment_adapter([item],c,{},judge.digest(judge.PROMPT.encode()),{'qid':'PASS'})
            ref = data[0]['input']
            self.assertEqual(evaluate(input=ref,output={'status':'STOPPED'}), [])
            self.assertEqual(evaluate(input=ref,output={'status':'COMPLETED','labels':{'core':'UNKNOWN'}}), [])
            self.assertEqual(evaluate(input='unknown',output={'labels':{'core':'PASS'}}), [])
            self.assertEqual(evaluate(input=ref,output={'labels':{'core':'FAIL'}}), {'name':'core_agreement','value':0})
            c.close()

    def test_human_reference_must_cover_every_case(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'refs.jsonl'
            path.write_text(json.dumps({'qid': 'a', 'human_correctness': 'PASS'}) + '\n', encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'reference set incomplete'):
                judge.load_human_references(path, [{'qid': 'a'}, {'qid': 'b'}])
            path.write_text(json.dumps({'qid': 'a', 'human_correctness': 'PASS'}) + '\n' +
                            json.dumps({'qid': 'b', 'human_correctness': 'FAIL'}) + '\n', encoding='utf8')
            self.assertEqual(judge.load_human_references(path, [{'qid': 'a'}, {'qid': 'b'}]), {'a': 'PASS', 'b': 'FAIL'})

    def test_experiment_span_filter_denies_unknown_scope_and_events(self):
        valid=SimpleNamespace(name='experiment-item-task',instrumentation_scope=SimpleNamespace(name='langfuse-sdk'),events=[],links=[])
        self.assertTrue(judge.should_export_experiment_span(valid))
        for item in [SimpleNamespace(name='other',instrumentation_scope=valid.instrumentation_scope,events=[],links=[]),
                     SimpleNamespace(name=valid.name,instrumentation_scope=SimpleNamespace(name='untrusted'),events=[],links=[]),
                     SimpleNamespace(name=valid.name,instrumentation_scope=valid.instrumentation_scope,events=['private'],links=[])]:
            self.assertFalse(judge.should_export_experiment_span(item))

    def test_experiment_preflight_rejects_bad_version_and_unsafe_environment(self):
        from unittest.mock import patch
        env={'LANGFUSE_BASE_URL':'https://us.cloud.langfuse.com','LANGFUSE_PUBLIC_KEY':'pk-lf-test','LANGFUSE_SECRET_KEY':'sk-lf-test'}
        with patch.dict(judge.os.environ,env,clear=True), patch('importlib.metadata.version',return_value='5.0.4'):
            judge.verify_experiment_environment()
            with patch.dict(judge.os.environ,{'OTEL_EXPORTER_OTLP_ENDPOINT':'https://invalid'}):
                with self.assertRaisesRegex(RuntimeError,'unexpected tracing'):judge.verify_experiment_environment()
            with patch('importlib.metadata.version',return_value='5.1.0'):
                with self.assertRaisesRegex(RuntimeError,'unsupported'):judge.verify_experiment_environment()

    def test_outcomes_and_ambiguous_finish(self):
        def payload(reason, content): return json.dumps({'choices':[{'finish_reason':reason,'message':{'content':content}}]}).encode()
        self.assertEqual(judge.classify(200,payload('length','x'))[0], 'TRUNCATED')
        self.assertEqual(judge.classify(200,payload('stop',''))[0], 'EMPTY')
        self.assertEqual(judge.classify(500,b'err')[0], 'FAILED')
        self.assertIsNone(judge.classify(200,payload(None,'x'))[0])
        self.assertIsNone(judge.classify(200,b'{')[0])


if __name__ == '__main__': unittest.main()
