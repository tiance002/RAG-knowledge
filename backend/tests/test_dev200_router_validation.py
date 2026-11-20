"""No network: exercise the runner's capture and reservation boundaries."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[2] / 'scripts' / 'ecs_dev200_router_validation.py'
spec = importlib.util.spec_from_file_location('dev200_runner', RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_readonly_search_only_disables_diagnostic_logging(self):
        calls = []
        async def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return ('results', {'retrieval': 'unchanged'})
        wrapped = runner.readonly_retrieval_search(spy)
        args = ('session', 'query', ['kb'])
        params = {'search_mode': 'hybrid', 'top_k': 50, 'log_retrieval': True, 'original_query': 'original'}
        result = asyncio.run(wrapped(*args, **params))
        self.assertEqual(result, ('results', {'retrieval': 'unchanged'}))
        self.assertEqual(calls, [(args, dict(params, log_retrieval=False))])
        positional = tuple(range(11))
        asyncio.run(wrapped(*positional))
        self.assertEqual(calls[1][0], positional[:9] + (False, positional[10]))
        self.assertEqual(calls[1][1], {})

    def test_prepare_runs_inside_all_api_managed_contexts(self):
        active = []
        events = []
        def factory(name):
            @asynccontextmanager
            async def managed(**kwargs):
                if name == 'provider':
                    self.assertEqual(kwargs, {'preinitialize': True})
                else:
                    self.assertEqual(kwargs, {})
                active.append(name)
                events.append('enter:' + name)
                try:
                    yield
                finally:
                    self.assertEqual(active.pop(), name)
                    events.append('exit:' + name)
            return managed
        provider = types.ModuleType('app.services.provider_http')
        chroma = types.ModuleType('app.services.bounded_chroma_executor')
        lexical = types.ModuleType('app.services.bounded_lexical_executor')
        provider.managed_provider_http = factory('provider')
        chroma.managed_chroma_executor = factory('chroma')
        lexical.managed_lexical_executor = factory('lexical')
        async def capture_all(args, rows, source):
            self.assertEqual(active, ['provider', 'chroma', 'lexical'])
            events.append('prepare')
        app = types.ModuleType('app'); app.__path__ = []
        services = types.ModuleType('app.services'); services.__path__ = []
        modules = {'app': app, 'app.services': services,
                   'app.services.provider_http': provider,
                   'app.services.bounded_chroma_executor': chroma,
                   'app.services.bounded_lexical_executor': lexical}
        with patch.dict(sys.modules, modules), patch.object(runner, 'prepare', capture_all):
            asyncio.run(runner.prepare_with_lifespan(None, [], {}))
        self.assertEqual(events, ['enter:provider', 'enter:chroma', 'enter:lexical', 'prepare',
                                  'exit:lexical', 'exit:chroma', 'exit:provider'])

    def test_context_identical_except_model(self):
        messages = [{'role': 'user', 'content': 'question'}, {'role': 'system', 'content': 'evidence'}]
        a = runner.request_body(messages, 'a')
        b = runner.request_body(messages, 'b')
        self.assertEqual({k: v for k, v in a.items() if k != 'model'},
                         {k: v for k, v in b.items() if k != 'model'})
        self.assertEqual(a['extra_body'], {'enable_thinking': False})

    def test_reservation_blocks_any_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempt.json'
            self.assertTrue(runner.reserve(path, {'state': 'RESERVED'}))
            self.assertFalse(runner.reserve(path, {'state': 'COMPLETED'}))
            self.assertIn('RESERVED', path.read_text())

    def test_existing_unknown_attempt_never_sends(self):
        class Completions:
            async def create(self, **kwargs):
                raise AssertionError('existing attempt must not be sent')
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
        messages = [{'role': 'user', 'content': 'q'}]
        item = {'index': 0, 'item_id': '1', 'messages': messages,
                'messages_sha256': runner.digest(runner.canonical(messages))}
        binding = {'id': 'id', 'model': 'm'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.reserve(root / 'attempts' / '000-cheap.json', {'state': 'UNKNOWN'})
            with patch.object(runner, 'resources', return_value={'mem_available': 900*1024**2, 'disk_free': 4*1024**3}):
                result = asyncio.run(runner.send_one(root, item, 'cheap', binding, client, asyncio.Semaphore(1)))
            self.assertEqual(result['state'], 'UNKNOWN')

    def test_prepare_exception_recorded_and_next_question_continues(self):
        rows = [{'item_id': str(i), 'language': 'en', 'question': 'q'} for i in range(2)]
        async def capture(row, source):
            if row['item_id'] == '0':
                raise ValueError('constructor failed')
            return {'item_id': row['item_id'], 'status': 'UNCAPTURABLE'}
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(root=Path(directory))
            snapshot = {'mem_available': 900*1024**2, 'disk_free': 4*1024**3}
            with patch.object(runner, 'capture_question', capture), patch.object(runner, 'resources', return_value=snapshot):
                asyncio.run(runner.prepare(args, rows, {}))
            import json
            first = json.loads((args.root / 'prepared' / '000.json').read_text())
            second = json.loads((args.root / 'prepared' / '001.json').read_text())
            self.assertEqual((first['status'], first['error_type']), ('PREPARATION_ERROR', 'ValueError'))
            self.assertEqual(second['status'], 'UNCAPTURABLE')

    def test_interrupted_preparation_not_reissued(self):
        row = {'item_id': '0', 'language': 'en', 'question': 'q'}
        async def capture(*args):
            raise AssertionError('must not repeat preparation')
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(root=Path(directory))
            runner.reserve(args.root / 'prepare_attempts' / '000.json', {'state': 'RESERVED'})
            with patch.object(runner, 'capture_question', capture):
                asyncio.run(runner.prepare(args, [row], {}))
            import json
            saved = json.loads((args.root / 'prepared' / '000.json').read_text())
            self.assertEqual(saved['status'], 'UNKNOWN_PREPARATION')

    def test_guard_and_response_states(self):
        self.assertFalse(runner.guard_ok({'mem_available': 800*1024**2-1, 'disk_free': 4*1024**3}))
        self.assertFalse(runner.guard_ok({'mem_available': 900*1024**2, 'disk_free': 3*1024**3-1}))
        self.assertTrue(runner.guard_ok({'mem_available': 800*1024**2, 'disk_free': 3*1024**3}))
        self.assertEqual(runner.classify('', 'stop'), 'EMPTY')
        self.assertEqual(runner.classify('incomplete', 'length'), 'TRUNCATED')
        self.assertEqual(runner.classify('ok', 'stop'), 'COMPLETED')
        self.assertEqual(runner.classify('ok', None), 'UNKNOWN')

    def test_capture_closes_before_final_send(self):
        gg = types.ModuleType('app.services.grounded_generation')
        prepared = types.SimpleNamespace(messages=[{'role': 'user', 'content': 'q'}], citations=[], summary={'n': 1})
        gg.prepare_grounded_request = lambda *a, **k: prepared
        gg.route_direct_history_generation = lambda *a, **k: None
        gg.generate_no_results_fallback = lambda *a, **k: None
        state = {'sent': False, 'closed': False}
        class Service:
            def __init__(self):
                self.retrieval_service = types.SimpleNamespace(search=lambda *a, **k: None)
            async def chat_stream(self, **kwargs):
                try:
                    gg.prepare_grounded_request(None, [], [])
                    yield 'rag_result', {'request_budget': {'n': 1}}
                    state['sent'] = True
                    raise AssertionError('final provider send should not run')
                finally:
                    state['closed'] = True
        chat = types.ModuleType('app.services.chat')
        chat.ChatService = Service
        schemas = types.ModuleType('app.schemas.schemas')
        schemas.Message = lambda **kwargs: kwargs
        memory = types.ModuleType('app.services.memory')
        memory.PreferenceScope = lambda **kwargs: kwargs
        commands = types.ModuleType('app.services.memory_commands')
        commands.parse_preference_commands = lambda q: ([], q)
        app = types.ModuleType('app'); app.__path__ = []
        services = types.ModuleType('app.services'); services.__path__ = []; services.grounded_generation = gg
        schema_pkg = types.ModuleType('app.schemas'); schema_pkg.__path__ = []
        modules = {'app': app, 'app.services': services, 'app.schemas': schema_pkg,
                   'app.services.grounded_generation': gg, 'app.services.chat': chat,
                   'app.schemas.schemas': schemas, 'app.services.memory': memory,
                   'app.services.memory_commands': commands}
        with patch.dict(sys.modules, modules):
            output = asyncio.run(runner.capture_question({'item_id': '1', 'language': 'en', 'question': 'q'},
                                                         {'config_body': {}, 'scope_ids': []}))
        self.assertEqual(output['status'], 'PREPARED')
        self.assertFalse(state['sent'])
        self.assertTrue(state['closed'])


if __name__ == '__main__':
    unittest.main()
