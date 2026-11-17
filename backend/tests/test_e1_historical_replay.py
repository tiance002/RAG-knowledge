"""Replay integrity and privacy gates; no production app/provider imports."""
import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('e1_historical_replay', ROOT / 'scripts/e1_historical_replay.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
BUNDLE = ROOT / 'var/e1-historical-replay-20261029-r2'


class HistoricalReplayTests(unittest.TestCase):
    def test_all_selected_answers_match_original_sse_and_hashes(self):
        items, records = replay.validate_bundle(BUNDLE)
        self.assertEqual(len(items), 5)
        self.assertEqual(len({r['qid'] for r in records}), 5)
        self.assertTrue(any('utc' in i['input']['question'] for i in items))

    def test_tampered_answer_stops_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ['dry-run.json', 'dataset-items.jsonl', 'historical-answers.jsonl']:
                (path / name).write_bytes((BUNDLE / name).read_bytes())
            (path / 'historical-answers.jsonl').write_bytes(b'{}\n')
            with self.assertRaisesRegex(ValueError, 'bundle hash mismatch'):
                replay.validate_bundle(path)

    def test_authorization_denial_stops_before_cloud(self):
        approval = json.loads((BUNDLE / 'authorization.approved.json').read_text(encoding='utf-8'))
        approval['approved'] = False
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            return json.dumps(approval) if path.name == 'authorization.approved.json' else real_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertRaisesRegex(ValueError, 'authorization absent'):
            replay.authorized_bundle(BUNDLE)

    def test_supplemental_human_fail_is_hash_bound(self):
        approval = json.loads((BUNDLE / 'authorization.approved.json').read_text(encoding='utf-8'))
        approval['supplemental_human_review']['answer_hash'] = '0' * 64
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            return json.dumps(approval) if path.name == 'authorization.approved.json' else real_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), self.assertRaisesRegex(ValueError, 'not bound'):
            replay.authorized_bundle(BUNDLE)

    def test_original_human_unknown_is_preserved_with_separate_user_fail(self):
        items, records = replay.authorized_bundle(BUNDLE)
        record = next(r for r in records if r['qid'].endswith('row-0431'))
        self.assertEqual(record['review']['human_core'], 'UNKNOWN')
        self.assertEqual(record['review']['supplemental_human_review']['human_core'], 'FAIL')
        self.assertEqual(sum(i['metadata']['effective_human_core'] == 'FAIL' for i in items), 2)

    def test_remote_model_dns_is_blocked(self):
        with replay.deny_network('us.cloud.langfuse.com') as blocked:
            with self.assertRaisesRegex(RuntimeError, 'endpoint blocked'):
                socket.getaddrinfo('model.invalid', 443)
        self.assertEqual(len(blocked), 1)

    def test_remote_gold_tampering_stops_before_observation(self):
        items, records = replay.validate_bundle(BUNDLE)
        changed = copy.deepcopy(items[0]); changed['expected_output']['reference_answer'] = ['wrong']
        with self.assertRaisesRegex(ValueError, 'dataset differs'):
            replay.make_task(None, items, records)(item=changed)

    def test_sensitive_scan_reads_actual_text_not_json_escapes(self):
        self.assertEqual(replay.scan_sensitive({'answer': 'title is:\ntext'})['local_path'], 0)
        self.assertEqual(replay.scan_sensitive({'answer': 'private C:\\data'})['local_path'], 1)
        self.assertEqual(replay.scan_sensitive({'answer': 'a@example.com'})['email'], 1)


if __name__ == '__main__':
    unittest.main()
