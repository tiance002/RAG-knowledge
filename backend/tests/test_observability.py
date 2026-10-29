import asyncio
import json
from types import SimpleNamespace

import pytest
from app.services import observability as obs


def test_disabled_has_no_side_effects(monkeypatch, tmp_path):
    monkeypatch.delenv('RAG_OBSERVABILITY_ENABLED', raising=False)
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    @obs.observe('request')
    def run():
        obs.event('private', query='private query')
        return 42
    assert run() == 42
    assert not list(tmp_path.iterdir())


def test_retrieval_outcome_remains_readable_without_exposing_query(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    with obs.span('request'):
        obs.event('retrieval.outcome', outcome='NO_RESULTS', query='private query')
    rows = [json.loads(x) for x in (tmp_path/'events').read_text().splitlines()]
    row = next(x for x in rows if x['stage'] == 'retrieval.outcome')
    assert row['metadata']['outcome'] == 'NO_RESULTS'
    assert 'private query' not in json.dumps(row)


def test_fail_open_does_not_retry_or_swallow_application_errors(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_sdk', lambda: SimpleNamespace(start_as_current_observation=lambda **kw: (_ for _ in ()).throw(RuntimeError('export failed'))))
    calls = []
    @obs.observe('qa')
    def run():
        calls.append(1)
        raise ValueError('application error')
    with pytest.raises(ValueError, match='application error'):
        run()
    assert calls == [1]
    assert obs._current.get() is None
    rows = [json.loads(line) for line in (tmp_path/'events').read_text().splitlines()]
    assert all(row.get('event_id') for row in rows)


def test_flush_uses_existing_client_and_is_fail_open(monkeypatch):
    calls = []
    monkeypatch.setattr(obs, '_client', SimpleNamespace(flush=lambda: calls.append('flush')))
    obs.flush_observations()
    assert calls == ['flush']
    monkeypatch.setattr(obs, '_client', SimpleNamespace(flush=lambda: (_ for _ in ()).throw(RuntimeError('failed'))))
    assert obs.flush_observations() is None


def test_parallel_roots_nested_stages_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    @obs.observe('qa')
    async def run(i):
        await asyncio.sleep(0)
        obs.event('query', query='secret question', api_key='secret-key')
        with obs.span('retrieval'):
            obs.candidates('raw', [{'id': str(i), 'content': 'private document', 'score': .8}])
    async def all_runs():
        await asyncio.gather(*(run(i) for i in range(10)))
    asyncio.run(all_runs())
    data = (tmp_path/'events').read_text()
    assert 'private document' not in data and 'secret question' not in data and 'secret-key' not in data
    rows = [json.loads(x) for x in data.splitlines()]
    roots = [x for x in rows if x['stage']=='qa' and x['event']=='start']
    assert len({x['trace_id'] for x in roots}) == 10
    assert len([x for x in rows if x['stage']=='raw']) == 10


def test_stream_order_and_exception_preserved(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    @obs.observe('stream')
    async def stream():
        yield 'first'
        raise ValueError('stream error')
    async def read():
        gen = stream()
        assert await anext(gen) == 'first'
        with pytest.raises(ValueError):
            await anext(gen)
    asyncio.run(read())
