"""E0.4: observe real chat assembly with fake retrieval and a fake provider."""
import asyncio
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.schemas import Message, MessageRole
from app.services import chat, grounded_generation, spreadsheet_grounding, rerank
from app.services.trace_schema import content_bytes


class FakeSession:
    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False


def test_native_source_identity_is_not_fabricated_as_a_chunk():
    original = {'source_type': 'source_native_table_expansion', 'fileId': 'f',
                'parent_chunk_id': 'parent', 'source_metadata': {'source_sha256': 'a'*64, 'page': 9, 'table_bbox': [1,2,3,4]},
                'content': 'native proof'}
    record = grounded_generation.delivered_evidence_records([original])[0]
    assert record['source_type'] == 'native_source_expansion' and record['chunk_id'] is None
    assert record['evidence_id'].startswith('native:') and record['source_sha256'] == 'a'*64
    assert record['position']['page'] == 9 and record['parent_chunk_id'] == 'parent'
    assert 'chunk_id' not in original


def test_expanded_neighbor_does_not_reach_final_model_message(monkeypatch, tmp_path):
    # Historical A-arm regression; the default E2 chat strategy is tested separately.
    monkeypatch.setenv('RAG_CHAT_CONTEXT_STRATEGY', 'ORIGINAL_CONTENT')
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events.jsonl'))
    monkeypatch.setattr(chat, 'Session', FakeSession)
    monkeypatch.setattr(spreadsheet_grounding, 'prepare_spreadsheet_grounding', lambda *args: None)
    seed = SimpleNamespace(id='seed', parent_id='parent', file_id='source', original_index=0,
                           content='seed without target', page_number=None, content_type='text',
                           image_path=None, heading_level=None, heading_text=None)
    sibling = SimpleNamespace(id='neighbor', parent_id='parent', file_id='source', original_index=1,
                              content='GOLD_IN_NEIGHBOUR')
    class Result:
        def __init__(self, rows): self.rows = rows
        def all(self): return self.rows
    class ExpansionSession:
        def __init__(self): self.calls = 0
        def get(self, model, key): return seed if key == 'seed' else None
        def exec(self, query):
            self.calls += 1
            return Result([seed, sibling] if self.calls == 1 else [])
    expansion_events = []
    monkeypatch.setattr(rerank, 'event', lambda name, **data: expansion_events.append((name, data)))
    selected = rerank.expand_context([{'id': 'seed', 'file_id': 'source', 'file_name': 'source.txt',
                                       'kb_id': 'kb', 'content': seed.content, 'score': .8}],
                                     ExpansionSession(), window_size=1)[0]
    expansion = next(data for name, data in expansion_events if name == 'retrieval.context_expansion')
    assert expansion['sibling_ids'] == ['seed', 'neighbor']
    assert expansion['included_sibling_ids'] == ['seed', 'neighbor']
    group = next(data for name, data in expansion_events if name == 'rag.candidate_transition')
    assert group['included_evidence_ids'] == ['seed', 'neighbor']
    response = SimpleNamespace(id='fake', model='fake', usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='Only seed evidence. [1]'), finish_reason='stop')])
    create = AsyncMock(return_value=response)
    llm = SimpleNamespace(model='fake', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = llm
    service._retrieve_with_new_service = AsyncMock(return_value=([selected], {'retrieval_status':'SUCCESS'}))
    observations = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **data: observations.append((name, data)))
    monkeypatch.setattr(grounded_generation, 'event', lambda name, **data: observations.append((name, data)))
    async def run():
        return [x async for x in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content='What is the target?')], ['kb'], 5, 0,
            None, False, 0, None, context_window=1)]
    sse = asyncio.run(run())
    assert 'GOLD_IN_NEIGHBOUR' in selected['content']
    create.assert_awaited_once()
    messages = create.await_args.kwargs['messages']
    assert 'seed without target' in str(messages)
    assert 'GOLD_IN_NEIGHBOUR' not in str(messages)
    citation = next(d['citations'][0] for kind, d in sse if kind == 'rag_result')
    assert citation['content'] == 'seed without target'
    assert next(d['content'] for kind, d in sse if kind == 'answer_chunk') == 'Only seed evidence. [1]'
    assert sse[-1][0] == 'done'
    delivery = [d for name, d in observations if name == 'context.delivery']
    selection = next(d for d in delivery if d['delivery_target'] == 'citation_selection')
    assert selection['is_final_model_delivery'] is False
    change = selection['changes'][0]
    assert change['reason_code'] == 'replaced_by_original_content'
    assert change['selected_content_sha256'] == hashlib.sha256(selected['content'].encode()).hexdigest()
    assert change['delivered_content_sha256'] == hashlib.sha256(selected['original_content'].encode()).hexdigest()
    final = next(d for d in delivery if d['delivery_target'] == 'provider_request')
    assert final['is_final_model_delivery'] is True
    evidence = [{'rank': 1, 'evidence_id':'seed', 'chunk_id':'seed', 'parent_chunk_id':None,
                 'file_id':'source', 'content':'seed without target', 'source_sha256':None,
                 'source_type':'indexed_chunk', 'position':{'page':None, 'table_bbox':None,
                                                            'sheet':None, 'row':None, 'cell':None}}]
    assert final['output']['content_sha256'] == hashlib.sha256(content_bytes(evidence)).hexdigest()
