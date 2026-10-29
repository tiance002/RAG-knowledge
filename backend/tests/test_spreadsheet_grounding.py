import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models import DocumentChunk, FileDocument
from app.services.grounded_validation import validate_grounded_answer
from app.services.grounded_generation import generate_grounded_candidate
from app.services.spreadsheet_grounding import prepare_spreadsheet_grounding


@pytest.fixture
def real_context(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    path = root / 'var/p0-observability/p1-live-review/excel-1.xlsx'
    response = root / 'var/p0-observability/p1-live-review/excel-response.sse'
    if not path.exists() or not response.exists():
        pytest.skip('saved source and citation unavailable')
    line = next(x for x in response.read_text(encoding='utf8').splitlines() if x.startswith('data: {"citations"'))
    citation = json.loads(line[6:])['citations'][0]
    chunk = SimpleNamespace(file_id=citation['fileId'], content=citation['content'])
    doc = SimpleNamespace(id=citation['fileId'], kb_id=citation['kb_id'], local_path=str(path))
    session = MagicMock()
    session.get.side_effect = lambda model, ident: chunk if model is DocumentChunk and ident == citation['chunk_id'] else doc if model is FileDocument and ident == citation['fileId'] else None
    manifest = {'schema': 'verified-source-files/v1', 'sources': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
    mf = tmp_path / 'sources.json'
    mf.write_text(json.dumps(manifest), encoding='utf8')
    monkeypatch.setenv('RAG_SOURCE_MANIFEST', str(mf))
    monkeypatch.setenv('RAG_SOURCE_MANIFEST_SHA256', hashlib.sha256(mf.read_bytes()).hexdigest())
    question = 'How many public libraries are recorded for Alabama in table T01?'
    context = prepare_spreadsheet_grounding(session, [citation], ['office50-v1'], question)
    return session, citation, question, context


def test_real_source_grounded_answer_and_exact_qualifiers(real_context):
    session, citation, question, context = real_context
    assert context is not None
    assert context.proof['row'] == 11 and context.proof['value_col'] == 2
    assert 'fiscal year 2018' in context.statement and 'administrative entity' in context.statement
    assert 'NOTE:' in context.statement
    check = lambda text, q=question: validate_grounded_answer(text, [citation], finish_reason='stop', question=q, spreadsheet_context=context)
    assert check(context.statement).status == 'VERIFIED'
    assert check(context.statement.replace('2018', '2019')).status != 'VERIFIED'
    assert check(context.statement.replace('219', '218')).status != 'VERIFIED'
    assert check(context.statement.split('. 1A', 1)[0] + '. [1]').status != 'VERIFIED'
    assert check(context.statement, question.replace('public libraries', 'public library outlets')).status != 'VERIFIED'
    assert check(context.statement, question.replace('T01', 'T02')).status != 'VERIFIED'
    assert validate_grounded_answer(context.statement, [citation], finish_reason='stop', question=question).status != 'VERIFIED'
    assert validate_grounded_answer(context.statement, [citation], finish_reason='stop', question=question,
                                    spreadsheet_context={'statement': context.statement}).status != 'VERIFIED'


def test_registry_is_required_and_pinned(real_context, monkeypatch):
    session, citation, question, context = real_context
    monkeypatch.delenv('RAG_SOURCE_MANIFEST_SHA256')
    assert prepare_spreadsheet_grounding(session, [citation], ['office50-v1'], question) is None
    monkeypatch.setenv('RAG_SOURCE_MANIFEST_SHA256', '0' * 64)
    assert prepare_spreadsheet_grounding(session, [citation], ['office50-v1'], question) is None


def test_generation_receives_source_qualifiers(real_context):
    _, citation, question, context = real_context
    response = SimpleNamespace(id='local', model='test', usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=context.statement), finish_reason='stop')])
    create = AsyncMock(return_value=response)
    llm = SimpleNamespace(model='test', _get_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    output = asyncio.run(generate_grounded_candidate(llm, [{'role':'user','content':question}], [citation], spreadsheet_context=context))
    assert output[0] == context.statement and create.await_count == 1
    prompt = create.await_args.kwargs['messages'][0]['content']
    assert context.statement not in prompt and citation['content'] in prompt
    assert context.proof['footnotes']['1']['text'] in prompt
    assert context.proof['notes'][0] in prompt
