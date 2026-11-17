import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.schemas import Message, MessageRole
from app.services import chat, grounded_generation


class FakeSession:
    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


def run(monkeypatch, candidate, reason='stop', error=None, results=None, question='根据知识库回答'):
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setattr(chat, 'Session', FakeSession)
    call = AsyncMock(side_effect=error, return_value=(candidate, reason, {'total_tokens': 10}))
    monkeypatch.setattr(grounded_generation, 'generate_grounded_candidate', call)
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = SimpleNamespace()
    if results is None:
        results = [{'id': 'c1', 'file_id': 'f1', 'file_name': 'report', 'page_number': 10,
                    'content': '2023年收入：100万元\n2023年支出：200万元', 'score': 0.9}]
    service._retrieve_with_new_service = AsyncMock(return_value=(results, {'retrieval_status': 'SUCCESS'}))
    async def collect():
        return [item async for item in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content=question)], ['kb'], 5, 0,
            None, False, 0, None)]
    return asyncio.run(collect()), call


def test_verified_answer_is_buffered_and_keeps_citations(monkeypatch):
    events, call = run(monkeypatch, '2023年收入为100万元。[1] 2023年支出为200万元。[1]')
    assert call.await_count == 1
    answer = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    assert '2023年收入为100万元。[1]' in answer
    assert next(data for kind, data in events if kind == 'done')['answer_mode'] == 'GROUNDED_GENERATION'
    assert next(data for kind, data in events if kind == 'rag_result')['citations'][0]['chunk_id'] == 'c1'


@pytest.mark.parametrize('candidate,reason', [
    ('2023年收入为100万元。[1]', 'length'), ('', 'stop'), ('可用答案', None),
])
def test_incomplete_or_unknown_candidate_falls_back(monkeypatch, candidate, reason):
    events, _ = run(monkeypatch, candidate, reason)
    assert next(data for kind, data in events if kind == 'done')['answer_mode'] == 'EXTRACTIVE_FALLBACK'


@pytest.mark.parametrize('candidate', ['2023年收入为200万元。[1]', '2023年收入为100万元。[2]',
                                       '这是一段不同表述，且尚未经过事实核验。'])
def test_complete_answer_is_not_blocked_by_content_rules(monkeypatch, candidate):
    from app.services import grounded_validation
    monkeypatch.setattr(grounded_validation, 'validate_grounded_answer', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not run')))
    events, call = run(monkeypatch, candidate)
    assert call.await_count == 1
    assert next(data['content'] for kind, data in events if kind == 'answer_chunk') == candidate
    assert events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'


def test_unknown_generation_is_terminal_without_replay(monkeypatch):
    events, call = run(monkeypatch, '', error=TimeoutError('uncertain'))
    assert call.await_count == 1
    assert events[-1][0] == 'error' and events[-1][1]['status'] == 'UNKNOWN'
    assert not any(kind in ('answer_chunk', 'done') for kind, _ in events)


def test_empty_truncated_generation_is_reported_even_with_successful_fallback(monkeypatch):
    events, call = run(monkeypatch, '', reason='length')
    assert call.await_count == 1
    done = next(data for kind, data in events if kind == 'done')
    assert done['answer_mode'] == 'EXTRACTIVE_FALLBACK'
    assert done['generation_status'] == 'TRUNCATED'


def test_extractive_fallback_is_bounded_but_citations_remain_complete(monkeypatch):
    results = [{'id': f'c{i}', 'file_id': f'f{i}', 'content': '原文' * 300} for i in range(5)]
    events, _ = run(monkeypatch, '', results=results)
    answer = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    citations = next(data['citations'] for kind, data in events if kind == 'rag_result')
    assert len(citations) == 5 and all(len(c['content']) == 600 for c in citations)
    assert len(answer) < 300
    assert '目前无法确认问题的答案' in answer
    assert '以上仅展示部分摘录' in answer
    assert '[1]' in answer and '[2]' not in answer


def test_spreadsheet_location_uses_sheet_not_invented_page(monkeypatch):
    results = [{'id': 'c1', 'file_id': 'f1', 'file_name': 'tables.xlsx', 'page_number': 1,
                'content': '[工作表: T01]\n| State | Libraries |\n|:---|:---|\n| Alabama | 219 |'}]
    events, _ = run(monkeypatch, '', results=results)
    citation = next(data['citations'][0] for kind, data in events if kind == 'rag_result')
    assert citation['location'] == '工作表: T01'


def test_unknown_non_pdf_location_is_not_page_one(monkeypatch):
    results = [{'id': 'c1', 'file_id': 'f1', 'file_name': 'report.docx', 'page_number': 1,
                'content': 'A fact.'}]
    events, _ = run(monkeypatch, '', results=results)
    citation = next(data['citations'][0] for kind, data in events if kind == 'rag_result')
    assert citation['location'] == '未知位置'


def test_mixed_image_and_text_uses_only_text_and_discloses_image(monkeypatch):
    results = [
        {'id': 'text', 'content': '2023年收入：100万元', 'file_id': 'f1'},
        {'id': 'image', 'content': '图片描述：收入999万元', 'content_type': 'image', 'file_id': 'f2'},
        {'id': 'mixed', 'content': '图片描述：收入999万元 [图片: kb/chart.png]', 'file_id': 'f3'},
    ]
    events, call = run(monkeypatch, '2023年收入为100万元。[1]', results=results)
    assert call.await_count == 1
    evidence = call.await_args.args[2]
    assert [item['chunk_id'] for item in evidence] == ['text']
    assert '999万元' not in str(evidence)
    answer = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    assert '2023年收入为100万元。[1]' in answer
    assert '另有图片资料，其内容需要进一步核验' in answer
    assert events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'


@pytest.mark.parametrize('content', ['图片描述：收入999万元', None])
def test_image_only_never_generates_or_uses_caption(monkeypatch, content):
    results = [{'id': 'image', 'content': content, 'content_type': 'image', 'file_id': 'f2'}]
    events, call = run(monkeypatch, '收入999万元。[1]', results=results)
    call.assert_not_awaited()
    assert next(data['citations'] for kind, data in events if kind == 'rag_result') == []
    answer = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    assert '图片内容需要进一步核验' in answer
    assert '999万元' not in answer
    assert events[-1][1]['answer_mode'] == 'INSUFFICIENT_EVIDENCE'


def test_spreadsheet_prepared_before_single_generation_and_provenance(monkeypatch):
    from app.services import spreadsheet_grounding
    proof = {'sheet': 'T01', 'row': 11, 'label_col': 1, 'value_col': 2,
             'source_sha256': 'a' * 64, 'label': 'Example', 'value': 12,
             'fiscal_year': '2020', 'headers': [(5, 2, 'Number'), (6, 2, 'of public'), (7, 2, 'libraries1')],
             'footnotes': {'1': {'text': '1Administrative entities.', 'row': 12, 'column': 1}},
             'notes': ['NOTE: Data are estimates.']}
    statement, _ = spreadsheet_grounding._statement(proof, 1)
    context = spreadsheet_grounding.SpreadsheetGrounding(1, proof, statement)
    prepare = MagicMock(return_value=context)
    monkeypatch.setattr(spreadsheet_grounding, 'prepare_spreadsheet_grounding', prepare)
    traces = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **values: traces.append((name, values)))
    source = '[工作表: T01]\n| table |\n|---|\n| Example | 12 |'
    results = [{'id': 'c1', 'file_id': 'f1', 'file_name': 'table.xlsx', 'kb_id': 'kb', 'content': source}]
    q = 'How many public libraries are recorded for Example in table T01?'
    events, call = run(monkeypatch, statement, results=results, question=q)
    assert prepare.call_count == call.await_count == 1
    assert call.await_args.kwargs['spreadsheet_context'] is context
    assert events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
    updates = [data for kind, data in events if kind == 'rag_result']
    assert updates[-1]['citations'][0]['content'] == source
    assert updates[-1]['citations'][0]['source_metadata']['spreadsheet_table_verification']['footnotes'] == proof['footnotes']
    trace = next(v for name, v in traces if name == 'qa.source_verification')
    assert trace['status'] == 'VERIFIED' and trace['evidence_count'] == 1 and trace['duration_ms'] >= 0
    assert statement not in str(trace)
    bad_events, call = run(monkeypatch, statement.replace('2020', '2021'), results=results, question=q)
    assert call.await_count == 1 and bad_events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
