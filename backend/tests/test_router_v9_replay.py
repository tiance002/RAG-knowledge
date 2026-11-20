import json
import pathlib
import unittest
import importlib.util

ROOT=pathlib.Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('v9',ROOT/'scripts/ecs_router_v9_replay.py')
v9=importlib.util.module_from_spec(spec); spec.loader.exec_module(v9)
AREA=ROOT/'var/router-risk-v9-20261117'
INPUT=ROOT/'var/router-risk-v8-20261117/original300-reconstructed-inputs'

class Original300PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=json.loads((AREA/'runner-original300-preparation.json').read_text(encoding='utf-8'))
        cls.jobs=cls.report['manifest']['jobs']
    def test_counts_unique_and_no_history_send(self):
        self.assertEqual((self.report['inputs'],len(self.jobs),len(self.report['history_only'])),(300,268,32))
        self.assertEqual(len({j['index'] for j in self.jobs}),268)
        self.assertTrue(all(not j['overlay'] and j['role']=='cheap' for j in self.jobs))
        self.assertTrue(set(j['index'] for j in self.jobs).isdisjoint(x['index'] for x in self.report['history_only']))
    def test_all_source_and_output_hashes_and_budget(self):
        for j in self.jobs:
            name=f"{j['index']:03d}.json"
            old=(INPUT/name).read_bytes(); src=json.loads(old)
            prepared=json.loads((AREA/'preparation-input'/name).read_text(encoding='utf-8')); audit=next(a for a in self.report['audits'] if a['index']==j['index'])
            with self.subTest(index=j['index']):
                self.assertEqual(prepared['item_id'],src['item_id'])
                self.assertEqual(audit['original_file_sha256'],v9.sha(old))
                self.assertEqual(src['messages_sha256'],v9.sha(v9.canonical(src['messages'])))
                self.assertEqual(j['messages_sha256'],v9.sha(v9.canonical(prepared['messages'])))
                self.assertLessEqual(audit['budget_audit']['request_chars'],32000)
                self.assertLessEqual(audit['budget_audit']['provider_body_chars'],32000)
                self.assertFalse(audit['budget_audit']['source_bodies_cropped'])
    def test_repacking_whole_blocks_and_turns(self):
        repacked=[a for a in self.report['audits'] if a['budget_audit']['omitted_history'] or a['budget_audit']['omitted_citations']]
        self.assertEqual(len(repacked),3)
        self.assertTrue(all(not a['budget_audit']['source_bodies_cropped'] for a in repacked))

if __name__=='__main__': unittest.main()
