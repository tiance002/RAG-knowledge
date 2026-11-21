"""Isolated exact-body prompt candidate; offline transport and receipt checks."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import cheap_grounding as cheap
from app.services import grounded_generation as gg
from app.services.grounded_request_budget import PreparedGroundedRequest, request_chars


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('RAG_CHEAP_GROUNDING_ENABLED', 'false')
    monkeypatch.setattr(gg, 'event', lambda *args, **kwargs: None)
    monkeypatch.setattr(gg.get_settings(), 'GROUNDED_REQUEST_MAX_CHARS', 32000)
    llm = SimpleNamespace(model='Qwen/Qwen3.5-4B')
    body = 'Exact source text with quotes " and newline\n' * 20
    citations = [dict(source_alias='E1', content=body, fileName='first', location='page 1'),
                 dict(source_alias='E8', content=body, fileName='second', location='page 9')]
    messages = [dict(role='user', content='previous'),
                dict(role='assistant', content='history'), dict(role='user', content='question')]
    return llm, messages, citations


def test_ordinary_preserves_provenance_history_policy_and_routing(setup, monkeypatch):
    llm, messages, citations = setup
    registered = Mock()
    monkeypatch.setattr('app.services.conversation_context.register_request_sources', registered)
    baseline = gg._build_grounded_request(llm, messages, citations, register=False)[1]
    prepared = gg.prepare_grounded_request(llm, messages, citations, preference_policy='policy')
    assert prepared.routing_messages == baseline
    assert prepared.route_resolution is None
    assert prepared.citations == citations
    registered.assert_called_once_with(prepared.citations)
    assert prepared.original_messages[0]['content'] == baseline[0]['content'] + '\n\npolicy'
    assert prepared.messages[1:] == messages
    prompt = prepared.messages[0]['content']
    assert prompt.count(citations[0]['content']) == 1
    for citation in citations:
        assert f"[{citation['source_alias']}] 来源：{citation['fileName']} {citation['location']}\n" in prompt
    assert '正文与来源 [E1] 完全相同' in prompt
    assert prompt.endswith('policy')
    summary = prepared.summary
    assert summary['request_chars'] == request_chars(prepared.messages)
    assert summary['request_chars'] == (summary['fixed_request_chars']
        + summary['history_overhead_chars'] + summary['evidence_with_labels_chars'])
    assert summary['logical_evidence_body_chars'] == sum(len(c['content']) for c in citations)
    assert summary['evidence_body_chars'] == summary['logical_evidence_body_chars']
    receipt = summary['duplicate_body_compaction']
    assert receipt['saved_request_chars'] == request_chars(prepared.original_messages) - request_chars(prepared.messages)
    assert receipt['source_alias_map'] == [{'source_alias': 'E8', 'same_body_as': 'E1'}]


def test_cheap_route_cached_provider_receipt_and_expensive_fallback(setup, monkeypatch):
    llm, messages, citations = setup
    monkeypatch.setenv('RAG_CHEAP_GROUNDING_ENABLED', 'true')
    route = SimpleNamespace(selected_role='CHEAP', mode='DYNAMIC')
    resolver = Mock(return_value=(llm, llm.model, route, object()))
    monkeypatch.setattr(gg, '_route_generation', resolver)
    monkeypatch.setattr(gg, '_router_outcome', lambda *a, **k: None)
    original_input = deepcopy((messages, citations))
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    resolver.assert_called_once()
    assert resolver.call_args.args[2] is prepared.routing_messages
    assert resolver.call_args.args[3] == citations
    assert prepared.route_resolution == resolver.return_value
    assert prepared.citations == citations
    assert (messages, citations) == original_input
    assert cheap.CONTRACT_TEXT in prepared.messages[0]['content']
    assert cheap.CONTRACT_TEXT not in prepared.original_messages[0]['content']
    fields = dict(model=llm.model, max_tokens=gg.get_settings().LLM_ANSWER_MAX_TOKENS,
                  temperature=0.7, stream=False)
    assert prepared.summary['provider_argument_chars'] == cheap.provider_body_chars(prepared.messages, fields)
    expensive = SimpleNamespace(model='expensive')
    cheap_call = AsyncMock(return_value=SimpleNamespace(model=llm.model, usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'), finish_reason='stop')]))
    expensive_call = AsyncMock(return_value=SimpleNamespace(model='expensive', usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'), finish_reason='stop')]))
    llm._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=cheap_call)))
    expensive._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=expensive_call)))
    monkeypatch.setattr(gg, '_role_service', lambda *a: expensive)
    monkeypatch.setattr('app.services.model_router.decide_escalation', lambda *a, **k:
        SimpleNamespace(should_retry=True, selected_model='expensive', reason='TEST'))
    result = asyncio.run(gg.generate_grounded_candidate(llm, messages, citations,
        prepared_request=prepared, quality_check=lambda _: 'FAIL'))
    assert cheap_call.await_args.kwargs['messages'] is prepared.messages
    assert expensive_call.await_args.kwargs['messages'] is prepared.original_messages
    assert result.final_request_budget['request_chars'] == request_chars(prepared.original_messages)
    assert 'duplicate_body_compaction' not in result.final_request_budget
    resolver.assert_called_once()


def raw_prepared(citations, text):
    messages = [dict(role='system', content=text)]
    return PreparedGroundedRequest(messages, citations,
        dict(request_chars=request_chars(messages), evidence_body_chars=100,
             evidence_with_labels_chars=100), messages)


@pytest.mark.parametrize('problem', ['absent', 'repeated', 'overlap', 'alias', 'nontext'])
def test_ambiguous_blocks_fail_closed(setup, problem):
    llm, messages, citations = setup
    text = gg._build_grounded_request(llm, messages, citations, register=False)[0]
    if problem == 'absent':
        text = text.replace('[E8] 来源：', '[other] 来源：')
    elif problem == 'repeated':
        text += '\n' + text
    elif problem == 'overlap':
        inner = '[E8] 来源：second page 9\n' + citations[1]['content']
        citations[0]['content'] = inner
        text = '[E1] 来源：first page 1\n' + inner
    elif problem == 'alias':
        citations[1]['source_alias'] = 'E1'
    else:
        citations[1]['content'] = None
    prepared = raw_prepared(citations, text)
    snapshot = deepcopy(prepared)
    assert gg._compact_duplicate_source_bodies(prepared) is prepared
    assert prepared == snapshot


@pytest.mark.parametrize('kind', ['short', 'whitespace', 'unique'])
def test_no_savings_or_nonexact_bodies_are_noops(setup, kind):
    llm, messages, citations = setup
    if kind == 'short':
        for c in citations:
            c['content'] = 'x'
    else:
        citations[1]['content'] += ' ' if kind == 'whitespace' else 'different'
    baseline = gg._build_grounded_request(llm, messages, citations, register=False)[1]
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert prepared.messages == baseline
    assert 'duplicate_body_compaction' not in prepared.summary
    assert prepared.original_messages is None


def test_structured_protected_context_is_unchanged(setup):
    llm, messages, citations = setup
    proof = SimpleNamespace(citation_index=1, proof=dict(title='title', headers=[],
        label='row', value=1, notes=[], source='workbook', footnotes={}))
    baseline = gg._build_grounded_request(llm, messages, citations, proof, register=False)[1]
    prepared = gg.prepare_grounded_request(llm, messages, citations, spreadsheet_context=proof)
    assert prepared.messages == baseline
    assert prepared.citations == citations
    assert 'duplicate_body_compaction' not in prepared.summary


def test_stream_dispatch_uses_compacted_prompt_and_original_routing(setup, monkeypatch):
    llm, messages, citations = setup
    resolver = Mock(return_value=(llm, llm.model, None, None))
    monkeypatch.setattr(gg, '_route_generation', resolver)
    sent = []
    async def stream(**kwargs):
        sent.append(kwargs['messages'])
        yield 'answer [E1]'
        kwargs['on_final']({'finish_reason': 'stop'})
    llm.generate_stream = stream
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    async def collect():
        return [part async for part in gg.stream_grounded_candidate(llm, messages, citations,
            prepared_request=prepared)]
    assert asyncio.run(collect())
    assert sent == [prepared.messages]
    resolver.assert_not_called()  # Existing streaming lane requires routing OFF.
    assert prepared.routing_messages == gg._build_grounded_request(
        llm, messages, citations, register=False)[1]
    assert request_chars(sent[0]) == prepared.summary['request_chars']
