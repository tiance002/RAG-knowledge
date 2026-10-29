"""Source-bound table expansion; all providers and database access are mocked."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import fitz
from app.models import DocumentChunk, FileDocument
from app.services import pdf_table_grounding as grounding
from app.services import source_registry

SOURCE = Path(__file__).resolve().parents[2] / 'var/p0-observability/p1-live-review/cn-audit-2024.pdf'
QUESTION = '审计署2024年度“住房保障支出”在总收支表与财政拨款收支表中分别是多少万元？请按这两个口径的顺序回答。'


@pytest.fixture
def source_case(monkeypatch):
    if not SOURCE.exists(): pytest.skip('saved source unavailable')
    digest = sha256(SOURCE.read_bytes()).hexdigest()
    chunk = SimpleNamespace(file_id='file', content='actual retrieved chunk')
    doc = SimpleNamespace(id='file', kb_id='kb', local_path=str(SOURCE))
    session = MagicMock()
    session.get.side_effect = lambda model, ident: (chunk if model is DocumentChunk and ident == 'chunk'
                         else doc if model is FileDocument and ident == 'file' else None)
    citation = {'chunk_id':'chunk', 'fileId':'file', 'kb_id':'kb', 'content':chunk.content}
    monkeypatch.setattr(source_registry, 'trusted_source_hash', lambda p: digest if p == str(SOURCE) else None)
    return session, citation, digest


def test_real_native_table_expansion_retains_parent_and_geometry(source_case):
    session, citation, digest = source_case
    result = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    assert result is not None and result.source_sha256 == digest
    assert result.parent_chunk_id == 'chunk' and result.file_id == 'file'
    assert [(t['page'], t['value']) for t in result.tables] == [(10, '14,009.54'), (15, '13,898.15')]
    assert result.tables[1]['column_headers'][1][5] == '合计'
    assert result.tables[1]['column_headers'][1][6].replace('\n', '') == '一般公共预算财政拨款'
    assert all(t['field_bbox'] and t['value_bbox'] and t['source_note'] for t in result.tables)
    assert [c['index'] for c in result.supplementary_citations] == [2, 3]
    assert all(c['source_type'] == 'source_native_table_expansion' and c['parent_chunk_id'] == 'chunk'
               and 'chunk_id' not in c for c in result.supplementary_citations)
    assert '原表说明' in result.statement and '[2]' in result.statement and '[3]' in result.statement


def test_scope_and_source_digest_fail_closed(source_case, monkeypatch):
    session, citation, _ = source_case
    assert grounding.prepare_pdf_table_grounding(session, [citation], ['other'], QUESTION) is None
    assert grounding.prepare_pdf_table_grounding(session, [{**citation, 'content':'forged'}], ['kb'], QUESTION) is None
    assert grounding.prepare_pdf_table_grounding(session, [{**citation, 'kb_id':'other'}], ['kb'], QUESTION) is None
    monkeypatch.setattr(source_registry, 'trusted_source_hash', lambda _: '0' * 64)
    assert grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION) is None


@pytest.mark.parametrize('question', [
    QUESTION.replace('总收支表', '其他收支表'),
    QUESTION.replace('财政拨款收支表', '总收支表'),
    QUESTION.replace('2024年度', '2023年度'),
    QUESTION.replace('住房保障支出', '住房保障收入'),
    QUESTION.replace('万元', '元'),
    QUESTION + '另请说明今年的变化。',
])
def test_unsupported_or_mismatched_question_is_not_evidence(source_case, question):
    session, citation, _ = source_case
    assert grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], question) is None


def test_ambiguous_total_column_cannot_use_equal_component_value(source_case, monkeypatch):
    session, citation, _ = source_case
    original = fitz.Page.find_tables
    def altered(page, *args, **kwargs):
        finder = original(page, *args, **kwargs)
        if page.number != 14: return finder
        table = finder.tables[0]
        class Altered:
            bbox = table.bbox
            rows = table.rows
            def extract(self):
                data = table.extract()
                data[1][5], data[1][6] = data[1][6], data[1][5]
                return data
        return SimpleNamespace(tables=[Altered()])
    monkeypatch.setattr(fitz.Page, 'find_tables', altered)
    assert grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION) is None


def test_expansion_validation_requires_complete_statement_and_exact_records(source_case):
    from app.services.grounded_validation import validate_grounded_answer
    session, citation, _ = source_case
    context = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    expanded = [citation] + grounding.build_pdf_supplementary_citations(context, [citation])
    assert len(expanded) == 3
    assert all('chunk_id' not in x and x['sourceUrl'].startswith('/api/v1/files/file/source?kb_id=kb&sha256=') for x in expanded[1:])
    check = lambda text, c=expanded, q=QUESTION, proof=context: validate_grounded_answer(text, c, finish_reason='stop', question=q, pdf_context=proof)
    assert check(context.statement).status == 'VERIFIED'
    assert check(context.statement.replace('14,009.54', '13,898.15')).status != 'VERIFIED'
    assert check(context.statement.split('[2]')[0] + '[2]').status != 'VERIFIED'
    assert check(context.statement, q=QUESTION.replace('2024', '2023')).status != 'VERIFIED'
    assert check(context.statement, proof=None).status != 'VERIFIED'
    assert check(context.statement, proof={'statement': context.statement}).status != 'VERIFIED'
    forged = [expanded[0], {**expanded[1], 'source_metadata': {**expanded[1]['source_metadata'], 'value': '1'}}, expanded[2]]
    assert check(context.statement, c=forged).status != 'VERIFIED'
    swapped = [expanded[0], expanded[2], expanded[1]]
    assert check(context.statement, c=swapped).status != 'VERIFIED'
    forged_parent = [{**citation, 'fileId':'other'}, *expanded[1:]]
    assert check(context.statement, c=forged_parent).status != 'VERIFIED'


def test_generation_receives_full_expansion_before_one_call(source_case):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.grounded_generation import generate_grounded_candidate
    session, citation, _ = source_case
    context = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    citations = [citation] + grounding.build_pdf_supplementary_citations(context, [citation])
    response = SimpleNamespace(id='local', model='test', usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=context.statement), finish_reason='stop')])
    create = AsyncMock(return_value=response)
    llm = SimpleNamespace(model='test', _get_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    assert asyncio.run(generate_grounded_candidate(llm, [{'role':'user','content':QUESTION}], citations, pdf_context=context))[0] == context.statement
    assert create.await_count == 1
    prompt = create.await_args.kwargs['messages'][0]['content']
    assert context.statement not in prompt
    assert '逐字照录' not in prompt
    for item in citations:
        assert item['content'] in prompt


def test_mocked_chat_sends_expansion_before_generation_and_preserves_raw(source_case, monkeypatch):
    from test_normal_grounded_answer import run
    from app.services import chat
    session, citation, _ = source_case
    context = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    monkeypatch.setattr(grounding, 'prepare_pdf_table_grounding', lambda *args: context)
    monkeypatch.setenv('RAG_PDF_TABLE_GROUNDING', 'true')
    traces = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **values: traces.append((name, values)))
    results = [{'id':'chunk', 'file_id':'file', 'file_name':'audit.pdf', 'kb_id':'kb', 'content':citation['content']}]
    events, call = run(monkeypatch, context.statement, results=results, question=QUESTION)
    assert call.await_count == 1 and call.await_args.kwargs['pdf_context'] is context
    sent = call.await_args.args[2]
    assert len(sent) == 3 and sent[0]['content'] == citation['content']
    assert events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
    updates = [d for k,d in events if k == 'rag_result']
    assert len(updates) == 2 and len(updates[0]['citations']) == 1 and len(updates[1]['citations']) == 3
    trace = next(v for n,v in traces if n == 'qa.source_expansion')
    assert trace['original_count'] == 1 and trace['added_count'] == 2 and trace['status'] == 'VERIFIED'
    assert '14,009.54' not in str(trace)
    incomplete, call = run(monkeypatch, context.statement.split('[2]')[0] + '[2]', results=results, question=QUESTION)
    assert call.await_count == 1 and incomplete[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'


def test_short_two_scope_answer_binds_proof_and_displays_exact_notes(source_case, monkeypatch):
    from app.services.grounded_validation import validate_grounded_answer
    from test_normal_grounded_answer import run
    session, citation, _ = source_case
    context = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    expanded = [citation] + grounding.build_pdf_supplementary_citations(context, [citation])
    # Same answer shape as the saved live candidate, with indices generated by
    # this one-parent fixture rather than reusing the historical [20]/[21].
    candidate = ('审计署2024年度“住房保障支出”在总收支表中为14,009.54万元。[2]\n'
                 '审计署2024年度“住房保障支出”在财政拨款收支表中为13,898.15万元。[3]')
    check = lambda text, cs=expanded, q=QUESTION: validate_grounded_answer(text, cs, finish_reason='stop', question=q, pdf_context=context)
    assert check(candidate).status == 'VERIFIED'
    assert check(candidate.replace('\n', ' \n  ')).status == 'VERIFIED'
    assert check(context.statement).status == 'VERIFIED'
    invalid = [candidate.replace('14,009.54', '13,898.15'),
               candidate.replace('住房保障支出', '住房保障收入'),
               candidate.replace('2024年度', '2023年度'),
               candidate.replace('万元', '元'),
               candidate.replace('财政拨款收支表', '总收支表'),
               candidate.replace('[2]', '[3]', 1),
               '\n'.join(reversed(candidate.splitlines())),
               candidate.splitlines()[0], candidate + '此外，费用增加了。[3]',
               candidate.replace('总收支表', '全部收支表', 1)]
    for text in invalid:
        assert check(text).status != 'VERIFIED'
    altered = [expanded[0], {**expanded[1], 'sourceUrl':'/forged'}, expanded[2]]
    assert check(candidate, cs=altered).status != 'VERIFIED'
    monkeypatch.setattr(grounding, 'prepare_pdf_table_grounding', lambda *args: context)
    monkeypatch.setenv('RAG_PDF_TABLE_GROUNDING', 'true')
    results = [{'id':'chunk', 'file_id':'file', 'file_name':'audit.pdf', 'kb_id':'kb', 'content':citation['content']}]
    events, provider = run(monkeypatch, candidate, results=results, question=QUESTION)
    assert provider.await_count == 1 and events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
    output = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    assert candidate in output
    assert output == candidate
    for table in context.tables:
        assert table['source_note'] not in output
    assert grounding.render_pdf_expansion_answer(context, context.statement) == context.statement


def test_shared_subject_two_clause_binding_and_deterministic_source_notes(source_case, monkeypatch):
    from app.services.grounded_validation import validate_grounded_answer
    from test_normal_grounded_answer import run
    session, citation, _ = source_case
    context = grounding.prepare_pdf_table_grounding(session, [citation], ['kb'], QUESTION)
    expanded = [citation] + grounding.build_pdf_supplementary_citations(context, [citation])
    candidate = ('审计署2024年度“住房保障支出”在总收支表中为14,009.54万元，'
                 '在财政拨款收支表中为13,898.15万元。[2][3]')
    check = lambda text, cs=expanded: validate_grounded_answer(text, cs, finish_reason='stop', question=QUESTION, pdf_context=context)
    assert check(candidate).status == 'VERIFIED'
    assert check(candidate.replace('，', '，\n')).status == 'VERIFIED'
    invalid = [candidate.replace('[2][3]', '[3][2]'),
               candidate.replace('[2][3]', '[2]'),
               candidate.replace('[2][3]', '[2][2]'),
               candidate.replace('14,009.54', '13,898.15'),
               candidate.replace('13,898.15', '14,009.54'),
               candidate.replace('总收支表中为', '总收支表中不为'),
               candidate.replace('财政拨款收支表', '总收支表'),
               candidate.replace('住房保障支出', '住房保障收入'),
               candidate.replace('2024年度', '2023年度'),
               candidate.replace('万元', '元'),
               candidate.replace('在财政拨款收支表', '尤其在财政拨款收支表'),
               candidate + '两者完全一致。[2]',
               candidate.replace('审计署', '教育部'),
               candidate.replace('，在财政拨款收支表中为13,898.15万元', ''),
               candidate.replace('在总收支表中为14,009.54万元，在财政拨款收支表中为13,898.15万元',
                                 '在财政拨款收支表中为13,898.15万元，在总收支表中为14,009.54万元')]
    for text in invalid:
        assert check(text).status != 'VERIFIED'
    forged = [expanded[0], {**expanded[1], 'source_metadata': {**expanded[1]['source_metadata'], 'value': '1'}}, expanded[2]]
    assert check(candidate, forged).status != 'VERIFIED'
    monkeypatch.setattr(grounding, 'prepare_pdf_table_grounding', lambda *args: context)
    monkeypatch.setenv('RAG_PDF_TABLE_GROUNDING', 'true')
    results = [{'id':'chunk', 'file_id':'file', 'file_name':'audit.pdf', 'kb_id':'kb', 'content':citation['content']}]
    events, provider = run(monkeypatch, candidate, results=results, question=QUESTION)
    assert provider.await_count == 1 and events[-1][1]['answer_mode'] == 'GROUNDED_GENERATION'
    answer = next(data['content'] for kind, data in events if kind == 'answer_chunk')
    assert candidate in answer
    assert answer == candidate
    for table in context.tables:
        assert table['source_note'] not in answer
