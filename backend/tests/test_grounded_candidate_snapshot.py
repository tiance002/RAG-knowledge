import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import grounded_generation


def test_candidate_snapshot_is_opt_in_and_events_do_not_contain_raw_text(monkeypatch, tmp_path):
    secret_text = 'private candidate: 12345'
    response = SimpleNamespace(
        id='request-id', model='model', usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=secret_text), finish_reason='stop')],
    )
    create = AsyncMock(return_value=response)
    llm = SimpleNamespace(model='model', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    captured = []
    monkeypatch.setattr(grounded_generation, 'event', lambda name, **kwargs: captured.append((name, kwargs)))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_SNAPSHOT_DIR', str(tmp_path))
    monkeypatch.delenv('RAG_SNAPSHOTS_ENABLED', raising=False)

    async def run():
        return await grounded_generation.generate_grounded_candidate(
            llm, [{'role': 'user', 'content': 'question'}],
            [{'chunk_id': 'c1', 'content': 'evidence'}])

    assert asyncio.run(run())[0] == secret_text
    request = create.await_args.kwargs['messages'][0]['content']
    assert '请根据以下参考资料回答用户问题' in request
    assert '逐字照录' not in request
    assert 'evidence' in request
    candidate_event = next(data for name, data in captured if name == 'grounded.candidate')
    assert candidate_event['candidate']['snapshot_id'] is None
    assert secret_text not in json.dumps(candidate_event)
    assert not list(tmp_path.iterdir())

    captured.clear()
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'true')
    assert asyncio.run(run())[0] == secret_text
    candidate_event = next(data for name, data in captured if name == 'grounded.candidate')
    ref = candidate_event['candidate']['snapshot_id']
    assert ref and (tmp_path / (ref + '.txt')).read_text(encoding='utf-8') == secret_text
    assert secret_text not in json.dumps(candidate_event)
