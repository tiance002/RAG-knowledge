"""Mocked tests for the append-only 503 retry policy; no network."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
runner = load('runner503test', ROOT / 'scripts/ecs_dev200_router_validation.py')
retry = load('retry503test', ROOT / 'scripts/ecs_dev200_retry503.py')

class ProviderError(Exception):
    def __init__(self, status=None):
        super().__init__('error secret')
        self.status_code = status

class FakeClient:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = []
        self.chat = types.SimpleNamespace(completions=self)
    async def create(self, **kwargs):
        self.calls.append(kwargs)
        event = self.sequence.pop(0)
        if isinstance(event, Exception):
            raise event
        return types.SimpleNamespace(model_dump=lambda **kw: event)

class RetryTests(unittest.TestCase):
    def setup_case(self, root):
        messages = [{'role': 'user', 'content': 'same evidence'}]
        sha = runner.digest(runner.canonical(messages))
        prepared = {'index': 0, 'item_id': 'q', 'messages': messages, 'messages_sha256': sha}
        original = {'index': 0, 'item_id': 'q', 'role': 'cheap', 'status_code': 503,
                    'messages_sha256': sha, 'model_binding_id': 'id', 'requested_model': 'model'}
        path = root / 'attempts' / '000-cheap.json'
        runner.atomic_json(path, original)
        return path, original, prepared, {'id': 'id', 'model': 'model', 'api_key': 'secret'}

    def execute(self, sequence, *, reserved=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, original, prepared, binding = self.setup_case(root)
            if reserved:
                runner.reserve(root / 'retries503/000-cheap/01.json', {'state': 'RESERVED'})
            client = FakeClient(sequence)
            delays = []
            async def sleep(delay): delays.append(delay)
            with patch.object(runner, 'resources', return_value={'mem_available': 900*1024**2, 'disk_free': 4*1024**3}):
                result = asyncio.run(retry.retry_item(root, path, original, prepared, binding, client, runner, sleep))
            records = [retry.read(p) for p in sorted((root / 'retries503').rglob('*.json'))]
            self.assertEqual(retry.read(path), original)
            return result, client.calls, delays, records, prepared

    def test_three_503s_are_cap(self):
        result, calls, delays, records, _ = self.execute([ProviderError(503)] * 3)
        self.assertEqual(result[:2], ('abandoned', 3))
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [2, 4, 8])
        self.assertEqual([r['status_code'] for r in records], [503] * 3)
        self.assertTrue(all('secret' not in r['error'] for r in records))

    def test_503_then_success_keeps_request_identical(self):
        response = {'model': 'model', 'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 2}}
        result, calls, delays, records, prepared = self.execute([ProviderError(503), response])
        self.assertEqual(result[:2], ('success', 2))
        self.assertEqual(result[2], 'retries503/000-cheap/02.json')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0], runner.request_body(prepared['messages'], 'model'))
        self.assertEqual(delays, [2, 4])
        self.assertEqual(records[-1]['state'], 'COMPLETED')

    def test_timeout_is_not_retried(self):
        result, calls, _, _, _ = self.execute([TimeoutError('unknown')])
        self.assertEqual(result[:2], ('unknown', 1))
        self.assertEqual(len(calls), 1)

    def test_existing_reserved_is_not_retried(self):
        result, calls, _, _, _ = self.execute([], reserved=True)
        self.assertEqual(result[:2], ('unknown', 0))
        self.assertEqual(calls, [])

    def test_timeout_success_once_then_reuses_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, original, prepared, binding = self.setup_case(root)
            original.update(role='expensive', status_code=None, error_type='APITimeoutError')
            runner.atomic_json(path, original)
            response = {'model': 'model', 'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {}}
            client = FakeClient([response])
            async def sleep(delay): pass
            with patch.object(runner, 'resources', return_value={'mem_available': 900*1024**2, 'disk_free': 4*1024**3}):
                result = asyncio.run(retry.retry_item(root, path, original, prepared, binding, client, runner, sleep, kind='timeout'))
                repeat = asyncio.run(retry.retry_item(root, path, original, prepared, binding, client, runner, sleep, kind='timeout'))
            self.assertEqual(result[:2], ('success', 1))
            self.assertEqual(repeat[:2], ('success', 0))
            self.assertEqual(result[2], 'retries_timeout/000-expensive/01.json')
            self.assertEqual(len(client.calls), 1)

    def test_second_timeout_is_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, original, prepared, binding = self.setup_case(root)
            original.update(role='expensive', status_code=None, error_type='APITimeoutError')
            client = FakeClient([TimeoutError('timeout')])
            async def sleep(delay): pass
            with patch.object(runner, 'resources', return_value={'mem_available': 900*1024**2, 'disk_free': 4*1024**3}):
                first = asyncio.run(retry.retry_item(root, path, original, prepared, binding, client, runner, sleep, kind='timeout'))
                second = asyncio.run(retry.retry_item(root, path, original, prepared, binding, client, runner, sleep, kind='timeout'))
            self.assertEqual(first[:2], ('unknown', 1))
            self.assertEqual(second[:2], ('unknown', 0))
            self.assertEqual(len(client.calls), 1)

    def test_existing_503_success_is_not_resent_and_ledger_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, original, prepared, binding = self.setup_case(root)
            runner.atomic_json(root / 'prepared/000.json', prepared)
            runner.atomic_json(root / 'retries503/000-cheap/01.json',
                dict(original, state='COMPLETED', identity_status='MATCH'))
            existing = {'q:cheap': {'item_id': 'q', 'role': 'cheap', 'original_path': 'attempts/000-cheap.json',
                                    'effective_path': 'retries503/000-cheap/01.json'}}
            runner.atomic_json(root / 'effective-attempts.json', existing)
            bindings = {'cheap': dict(binding, base_url='https://example.invalid')}
            fingerprint = {'cheap': {'id': 'id', 'model': 'model', 'base_url': 'https://example.invalid',
                                     'credential_sha256': runner.digest(b'secret')}}
            runner.atomic_json(root / 'binding_fingerprint.json', fingerprint)
            client = FakeClient([])
            async def close(): pass
            client.close = close
            openai = types.ModuleType('openai'); openai.AsyncOpenAI = lambda **kw: client
            httpx = types.ModuleType('httpx'); httpx.AsyncClient = lambda **kw: None
            with patch.dict(sys.modules, {'openai': openai, 'httpx': httpx}), patch.object(runner, 'load_bindings', return_value=bindings):
                counts = asyncio.run(retry.run(root, root / 'db', runner, retry_timeout_once=True))
            self.assertEqual(counts['retry_calls'], 0)
            self.assertEqual(client.calls, [])
            self.assertEqual(json.loads((root / 'effective-attempts.json').read_text()), existing)

if __name__ == '__main__':
    unittest.main()
