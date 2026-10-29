"""No provider calls: rewrite-only scope, budget and request-contract tests."""
import importlib.util
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[2] / 'scripts' / 'run_p1_http_regression.py'
spec = importlib.util.spec_from_file_location('p1_runner', path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

BASE = ['--backend', '/tmp/backend', '--code-sha256', 'abc', '--llm-model', 'model', '--embedding-model', 'embed']
REWRITE = ['--rewrite-only', '--reuse-runtime', '/tmp/p1-http-test', '--scratch-fulltext', '--case', 'case-1', '--grounded-generation', 'off']


def test_rewrite_only_is_opt_in_and_one_send():
    args = runner.parse_args(BASE + REWRITE)
    assert runner.planned_bounds(args) == {'chat': 1, 'per_question': 1, 'embedding': 0, 'rerank': 0}
    case = {'qid': 'case-1', 'mode': 'chat', 'kb_id': 'kb', 'question': 'original', 'grounded_generation': 'off'}
    request = runner.build_http_request(case, 'verified-model', True)
    assert request['use_rewrite'] is True and request['search_mode'] == 'fulltext'
    assert request['rerank_enabled'] is False and request['mode'] == 'chat'
    assert runner.request_config(case, True) == {'mode': 'chat', 'search_mode': 'fulltext', 'use_rewrite': True, 'grounded_generation': 'off'}
    assert runner.send_is_permitted(case, rewrite_only=True, in_rewrite=True, used=False)
    assert not runner.send_is_permitted(case, rewrite_only=True, in_rewrite=False, used=False)
    assert not runner.send_is_permitted(case, rewrite_only=True, in_rewrite=True, used=True)
    assert not runner.send_is_permitted({**case, 'qid': 'empty-chat'}, rewrite_only=True, in_rewrite=True, used=False)
    assert not runner.send_is_permitted({**case, 'grounded_generation': 'on'}, rewrite_only=True, in_rewrite=True, used=False)


@pytest.mark.parametrize('extra', [
    ['--rewrite-only'],
    ['--rewrite-only', '--scratch-fulltext', '--case', 'case-1', '--grounded-generation', 'off'],
    ['--rewrite-only', '--reuse-runtime', '/tmp/p1-http-test', '--case', 'case-1', '--grounded-generation', 'off'],
    ['--rewrite-only', '--reuse-runtime', '/tmp/p1-http-test', '--scratch-fulltext', '--case', 'case-1'],
    REWRITE + ['--case', 'case-2'], REWRITE + ['--compare-grounded'], REWRITE + ['--empty-only'],
])
def test_invalid_rewrite_scope_exits_before_preflight(extra):
    with pytest.raises(SystemExit):
        runner.parse_args(BASE + extra)


def test_standard_budget_and_request_remain_unchanged():
    off = runner.parse_args(BASE + ['--grounded-generation', 'off'])
    on = runner.parse_args(BASE)
    compare = runner.parse_args(BASE + ['--grounded-generation', 'off', '--compare-grounded'])
    assert runner.planned_bounds(off)['chat'] == runner.planned_bounds(off)['per_question'] == 0
    assert runner.planned_bounds(on)['chat'] == runner.planned_bounds(compare)['chat'] == 6
    case = {'qid':'case-1', 'mode':'chat', 'kb_id':'kb', 'question':'q', 'grounded_generation':'off'}
    assert runner.build_http_request(case, 'model', False)['use_rewrite'] is False
    assert not runner.send_is_permitted(case, rewrite_only=False, in_rewrite=True, used=False)
    case['grounded_generation'] = 'on'
    assert runner.send_is_permitted(case, rewrite_only=False, in_rewrite=False, used=False)
    assert not runner.send_is_permitted(case, rewrite_only=False, in_rewrite=False, used=True)
    assert runner.request_config(case, False)['grounded_generation'] == 'on'


def test_execute_fails_closed_before_runtime_or_provider_access():
    with pytest.raises(ValueError, match='invalid rewrite-only execution scope'):
        runner.execute(Path('/missing'), [], {}, {}, {}, 'model', 'https://example.invalid', {}, rewrite_only=True)
