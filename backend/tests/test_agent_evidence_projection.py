"""Offline P3 input contracts. Synthetic replies do not establish answer quality."""
import asyncio
import copy
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from app.services import agent_workflow as aw
from app.services.agent_evidence_projection import VERSION, input_size_breakdown, project_agent_evidence_messages

BODY = 'SYNTHETIC_REVENUE_42_CNY_中文'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    monkeypatch.delenv('RAG_AGENT_EVIDENCE_PROJECTION', raising=False)
    monkeypatch.delenv('OPENAI_PROXY', raising=False)
    connect = socket.socket.connect
    def guarded(sock, address):
        # Windows asyncio's internal socketpair uses numeric loopback TCP.
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return connect(sock, address)
        raise AssertionError('External connections forbidden')
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    def forbidden(*args, **kwargs):
        raise AssertionError('External DNS forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def registered(text=BODY, count=1):
    registry = []
    data = aw._register_sources({'citations': [
        {'chunk_id': f'chunk-{index}', 'fileId': f'file-{index}', 'fileName': 'synthetic.pdf',
         'location': '第 7 页', 'page_number': 7, 'content': text,
         'source_metadata': {'page': 7, 'table_bbox': [1, 2, 3, 4]}}
        for index in range(count)], 'retrieval_status': 'SUCCESS',
        'source_mapping_status': 'VERIFIED', 'omitted_images': False}, registry)
    return data, registry


def tool(data, ident='call-1'):
    return ToolMessage(content=json.dumps(data, ensure_ascii=False), name='search_knowledge_base',
                       tool_call_id=ident, id='message-' + ident, artifact={'state': 'retained'},
                       additional_kwargs={'application_metadata': 'retained'})


def project(messages):
    registry = [citation for message in messages if isinstance(message, ToolMessage) and isinstance(message.content, str)
                for citation in _tool_citations(message)]
    return project_agent_evidence_messages(messages, canonical_context=aw._source_context, source_registry=registry)


def _tool_citations(message):
    try:
        return json.loads(message.content).get('citations', [])
    except ValueError:
        return []


def test_copy_only_projection_retains_context_metadata_message_identity_and_state():
    data, registry = registered()
    message = tool(data)
    original = copy.deepcopy((data, registry, message.model_dump()))
    result, stats = project([message])
    payload = json.loads(result[0].content)
    assert payload['context'] == data['context'] and payload['context'].count(BODY) == 1
    assert 'content' not in payload['citations'][0]
    assert payload['citations'][0]['source_id'] == 1
    assert payload['citations'][0]['title'] == 'synthetic.pdf'
    assert 'chunk_id' not in payload['citations'][0]
    assert payload['retrieval_status'] == data['retrieval_status']
    assert payload['source_ids'] == data['source_ids']
    assert 'source_mapping_status' not in payload
    assert {key: value for key, value in result[0].model_dump().items() if key != 'content'} == {key: value for key, value in message.model_dump().items() if key != 'content'}
    assert original == (data, registry, message.model_dump())
    assert result[0] is not message
    assert stats['message_status_counts'] == {'PROJECTED': 1}
    assert stats['removed_duplicate_body_chars'] == len(BODY)
    assert stats['removed_duplicate_body_utf8_bytes'] == len(BODY.encode())
    assert stats['input_tokens'] is None and stats['token_count_status'] == 'UNKNOWN'


def test_repeated_tool_results_keep_complete_body_in_each_result():
    data, _ = registered()
    first, second = tool(data, 'first'), tool(data, 'second')
    middle = AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base', 'id': 'second', 'args': {'query': 'retry'}}])
    output, stats = project([first, middle, second])
    assert output[1] is middle
    assert [output[index].tool_call_id for index in (0, 2)] == ['first', 'second']
    for index in (0, 2):
        payload = json.loads(output[index].content)
        assert payload['context'] == data['context'] and payload['context'].count(BODY) == 1
        assert not any('body_ref' in citation for citation in payload['citations'])
    assert stats['message_status_counts'] == {'PROJECTED': 2}


def test_different_sources_with_identical_text_are_not_merged():
    data, _ = registered(count=2)
    output, _ = project([tool(data)])
    payload = json.loads(output[0].content)
    assert payload['source_ids'] == [1, 2]
    assert [citation['source_id'] for citation in payload['citations']] == [1, 2]
    assert payload['context'] == data['context'] and payload['context'].count(BODY) == 2


@pytest.mark.parametrize('text', ['| Header | Value |\n|:--|--:|\n| Revenue | 42 |\nUnit: CNY; footnote unchanged.', '  leading and trailing spaces  ', 'quotes " remain \\ unchanged\n中文原文'])
def test_formatting_and_table_headers_units_footnotes_are_preserved_verbatim(text):
    data, _ = registered(text)
    output, _ = project([tool(data)])
    assert json.loads(output[0].content)['context'] == data['context']


@pytest.mark.parametrize('mutation', ['context', 'source_ids', 'unregistered', 'noncanonical_image', 'malformed'])
def test_mismatched_or_malformed_payload_is_untouched(mutation):
    data, _ = registered()
    if mutation == 'context': data['context'] += '\nextra content'
    if mutation == 'source_ids': data['source_ids'] = [9]
    if mutation == 'unregistered': data['citations'][0].pop('source_id')
    if mutation == 'noncanonical_image': data['citations'].append({'content_type': 'image', 'content': 'unverified caption'})
    message = tool(data)
    if mutation == 'malformed': message = message.model_copy(update={'content': '{invalid'})
    output, stats = project([message])
    assert output[0] is message and output[0].content == message.content
    assert stats['message_status_counts'].get('PROJECTED', 0) == 0
    assert stats['before'] == stats['after']


def test_images_unknown_status_and_other_tools_are_preserved():
    data, _ = registered()
    data['retrieval_status'] = 'UNKNOWN'
    image = {'content_type': 'image', 'content': '', 'chunk_id': 'image-1', 'evidence_status': 'UNVERIFIED_IMAGE'}
    data['citations'].append(image)
    message = tool(data)
    other = message.model_copy(update={'name': 'other_tool'})
    output, _ = project([message, other])
    payload = json.loads(output[0].content)
    assert payload['retrieval_status'] == 'UNKNOWN' and payload['citations'][-1] == {key: value for key, value in image.items() if key not in {'content', 'chunk_id'}}
    assert output[1] is other
    assert json.loads(message.content)['citations'][0]['content'] == BODY


def test_disjoint_application_and_nested_tool_ledgers_balance_chars_and_utf8():
    data, _ = registered()
    messages = [SystemMessage(content='system rules'), HumanMessage(content='中文问题'),
                AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base', 'id': 'call-1', 'args': {'query': '中文'}}]), tool(data)]
    tools = [{'type': 'function', 'function': {'name': 'search_knowledge_base'}}]
    before = input_size_breakdown(messages, tools)
    output, stats = project_agent_evidence_messages(messages, canonical_context=aw._source_context, source_registry=data['citations'], tools=tools)
    for sizes in (before, stats['after']):
        for unit in ('chars', 'utf8_bytes'):
            assert sum(part[unit] for part in sizes['components'].values()) == sizes['total'][unit]
            normalized_tool = json.dumps(json.loads((messages if sizes is before else output)[-1].content), ensure_ascii=False)
            expected = len(normalized_tool) if unit == 'chars' else len(normalized_tool.encode())
            assert sum(part[unit] for part in sizes['normalized_tool_payload_components'].values()) == expected
        assert sizes['input_tokens'] is None and sizes['token_count_status'] == 'UNKNOWN'
    assert stats['after']['total']['chars'] < before['total']['chars']
    assert stats['after']['total']['utf8_bytes'] < before['total']['utf8_bytes']


def test_actual_agent_sdk_wire_explicit_off_vs_default_view_preserves_route_budget_trace_and_state(monkeypatch):
    from app.services import grounded_generation as gg
    captures, observed, budget_inputs, route_inputs, events = [], [], [], [], []
    monkeypatch.setattr(aw, '_observe_model_request', lambda stage, messages, *a, **kw: observed.append([message.model_dump() for message in messages]))
    monkeypatch.setattr(aw, 'event', lambda name, **data: events.append((name, data)))
    original_budget = gg.enforce_request_budget
    def budget(messages, tools=None):
        budget_inputs.append([message.model_dump() for message in messages])
        return original_budget(messages, tools)
    monkeypatch.setattr(gg, 'enforce_request_budget', budget)
    wire_body = BODY + '\n| Metric | Revenue |\n|---|---|\n| Q1 | 42 |\nUnit: CNY; note: provisional.'
    data, registry = registered(wire_body)
    state = {'messages': [HumanMessage(content='What was revenue?'), AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base', 'id': 'call-1', 'args': {'query': 'revenue'}}]), tool(data)],
        'kb_ids': ['synthetic-kb'], 'retrieval_status': 'SUCCESS', 'last_score': .9,
        'original_query': 'What was revenue?', 'source_registry': registry, 'citations': data['citations'],
        'preference_policy': 'Synthetic unchanged preference policy.'}
    before = copy.deepcopy(state)
    async def handler(request):
        captures.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic', 'model': 'fixed-synthetic', 'choices': [
            {'index': 0, 'message': {'role': 'assistant', 'content': '42 [1]'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 999, 'completion_tokens': 4, 'total_tokens': 1003}})
    async def run(mode):
        if mode is None: monkeypatch.delenv('RAG_AGENT_EVIDENCE_PROJECTION', raising=False)
        else: monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', mode)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            model = ChatOpenAI(model='fixed-synthetic', api_key='synthetic', base_url='https://example.invalid/v1', streaming=False, max_retries=0, http_async_client=client)
            obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
            obj.tools = [aw.search_knowledge_base]
            obj._get_llm_service = lambda state: SimpleNamespace(model='fixed-synthetic', get_chat_model=lambda **kw: model)
            original_route = obj._route_post_retrieval_model
            def route(state, service, messages, **kwargs):
                route_inputs.append([message.model_dump() for message in messages])
                return original_route(state, service, messages, **kwargs)
            obj._route_post_retrieval_model = route
            return await obj.agent_node(state, {})
    baseline = asyncio.run(run('OFF'))
    candidate = asyncio.run(run(None))
    assert before == state
    assert baseline['messages'][0].content == candidate['messages'][0].content == '42 [1]'  # Fixed reply is not a quality verdict.
    assert baseline['citations'] == candidate['citations'] == data['citations']
    assert route_inputs[0] == route_inputs[1]
    assert observed == budget_inputs
    for index, wire in enumerate(captures):
        assert wire['model'] == 'fixed-synthetic'
        assert [message['role'] for message in wire['messages']] == ['system', 'user', 'assistant', 'tool']
        assert wire['messages'][-1]['tool_call_id'] == 'call-1'
        assert wire['messages'][-1]['content'] == observed[index][-1]['content']
    assert captures[0]['messages'][:-1] == captures[1]['messages'][:-1]
    assert {key: value for key, value in captures[0].items() if key != 'messages'} == {key: value for key, value in captures[1].items() if key != 'messages'}
    original_tool, projected_tool = [json.loads(wire['messages'][-1]['content']) for wire in captures]
    assert original_tool['context'] == projected_tool['context']
    assert original_tool['citations'][0]['content'] == wire_body
    assert projected_tool['context'].count(wire_body) == 1
    assert '| Metric | Revenue |' in projected_tool['context']
    assert 'Unit: CNY; note: provisional.' in projected_tool['context']
    assert 'chunk_id' not in projected_tool['citations'][0]
    assert projected_tool['citations'][0]['source_metadata'] == {'page': 7, 'table_bbox': [1, 2, 3, 4]}
    assert 'content' not in projected_tool['citations'][0]
    assert projected_tool['citations'][0]['source_id'] == 's1'
    assert projected_tool['citations'][0]['location'] == '第 7 页'
    projection_events = [data for name, data in events if name == 'agent.evidence_projection']
    assert len(projection_events) == 1 and projection_events[0]['projection_version'] == VERSION
    assert projection_events[0]['token_count_status'] == 'UNKNOWN'  # Synthetic usage is not a token savings measurement.


def test_non_text_tool_content_is_not_flattened_or_changed():
    message = ToolMessage(content=[{'type': 'text', 'text': 'synthetic multimodal envelope'}],
                          name='search_knowledge_base', tool_call_id='call-list')
    output, stats = project([message])
    assert output[0] is message
    assert stats['message_status_counts'] == {'NON_TEXT_TOOL_CONTENT': 1}
    assert stats['before']['unparsed_tool_payload_count'] == 1
    assert stats['before']['normalized_tool_payload_count'] == 0


def test_unknown_projection_mode_fails_before_budget_or_provider(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from app.services import grounded_generation as gg
    monkeypatch.setenv('RAG_AGENT_EVIDENCE_PROJECTION', 'UNSUPPORTED_MODE')
    budget, provider = Mock(), AsyncMock()
    monkeypatch.setattr(gg, 'enforce_request_budget', budget)
    monkeypatch.setattr(aw, 'invoke_provider_model', provider)
    obj = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    obj.tools = []
    obj._get_llm_service = lambda state: SimpleNamespace(model='synthetic', get_chat_model=lambda **kw:
        SimpleNamespace(bind_tools=lambda tools: SimpleNamespace(kwargs={})))
    with pytest.raises(ValueError, match='Unsupported Agent evidence projection mode'):
        asyncio.run(obj.agent_node({'messages': [HumanMessage(content='question')]}, {}))
    budget.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.parametrize('title,metadata', [
    (r'C:\private\revenue.xlsx', {'sheet': '预算 Sheet', 'row_start': 2, 'row_end': 8,
      'column_start': 1, 'column_end': 4, 'headers': ['收入', '备注'], 'unit': 'CNY', 'notes': '仅列示单元格'}),
    ('/uploads/cost.csv', {'row_start': 3, 'row_end': 5, 'column_start': 1,
      'column_end': 2, 'headers': ['category', 'cost'], 'units': {'cost': '元'}}),
    ('/uploads/manual.pdf', {'page': 7, 'heading_text': '3.1 计量',
      'source_locators': [{'page': 7, 'bbox': [1, 2, 30, 40], 'kind': 'pdf-table'}]}),
])
def test_lean_metadata_preserves_semantic_locations_and_title(title, metadata):
    data, registry = registered('| 项目 | 数值 |\n|---|---|\n| 收入 | 42 |\nUnit: CNY; note unchanged.')
    citation = registry[0]
    citation.update(fileName=title, kb_id='private-kb', score=.91, rerank_score=.8,
                    context_group_id='group:private', seed_child_ids=['child-private'],
                    parent_chunk_id='parent-private', content_hash='opaque',
                    source_metadata={**metadata, 'id': 'backend-uuid', 'source_sha256': 'a'*64,
                        'parent_id': 'private-parent', 'parent_child_version': 'character-v1',
                        'qa500_import': {'source_kb_id': 'curated-private'},
                        'unknown_semantic': {'id': 'table-row-A', 'score': 42, 'transaction_hash': 'business-transaction', 'row_semantics': 'totals exclude tax'}})
    data['context'] = aw._source_context(registry)
    original = copy.deepcopy((data, registry))
    message = tool(data)
    output, _ = project_agent_evidence_messages([message], canonical_context=aw._source_context,
                                               source_registry=registry)
    view = json.loads(output[0].content)
    assert view['context'] == data['context']
    assert view['context'].count(citation['content']) == 1
    assert view['citations'][0]['title'] == title.replace('\\', '/').split('/')[-1]
    assert view['citations'][0]['source_metadata'] == {**metadata, 'id': 'backend-uuid',
        'unknown_semantic': {'id': 'table-row-A', 'score': 42, 'transaction_hash': 'business-transaction', 'row_semantics': 'totals exclude tax'}}
    for key in ('chunk_id', 'fileId', 'kb_id', 'score', 'rerank_score', 'context_group_id',
                'seed_child_ids', 'parent_chunk_id', 'content_hash'):
        assert key not in view['citations'][0]
    assert original == (data, registry)


def test_caption_uncertainty_page_region_and_text_survive_without_backend_provenance_ids():
    data, registry = registered('【模型生成图片描述，需核对原图】\n图中柱状值约 42；含识别不确定性。')
    citation = registry[0]
    citation.update(evidence_status='MODEL_GENERATED_IMAGE_DESCRIPTION',
                    content_origin='model_generated_image_description', requires_original_verification=True,
                    source_image_references=[{'source_page': 7, 'bbox': [1, 2, 30, 40],
                        'image_sha256': 'b'*64, 'original_file_id': 'original-private',
                        'original_build_id': 'build-private', 'source_image_path': 'private/image.png'}])
    citation['source_metadata'].update(image_descriptions=[{'source_page': 7,
        'caption': '约 42，原图需核对', 'requires_original_verification': True,
        'source_image_path': 'private/image.png', 'indexed_text_sha256': 'c'*64}],
        source_locators=[{'kind': 'pdf-image-description', 'page': 7,
            'region': [1, 2, 30, 40], 'original_chunk_id': 'private-chunk'}])
    data['context'] = aw._source_context(registry)
    output, _ = project_agent_evidence_messages([tool(data)], canonical_context=aw._source_context,
                                               source_registry=registry)
    view = json.loads(output[0].content)
    evidence = view['citations'][0]
    assert view['context'] == data['context']
    assert evidence['evidence_status'] == 'MODEL_GENERATED_IMAGE_DESCRIPTION'
    assert evidence['requires_original_verification'] is True
    assert evidence['source_image_references'] == [{'source_page': 7, 'bbox': [1, 2, 30, 40]}]
    assert evidence['source_metadata']['image_descriptions'] == [{'source_page': 7,
        'caption': '约 42，原图需核对', 'requires_original_verification': True}]
    assert evidence['source_metadata']['source_locators'] == [{'kind': 'pdf-image-description',
        'page': 7, 'region': [1, 2, 30, 40]}]


def test_forged_integer_source_or_metadata_is_not_a_registered_mapping():
    data, registry = registered()
    data = copy.deepcopy(data)
    data['citations'][0]['location'] = 'forged location'
    message = tool(data)
    output, stats = project_agent_evidence_messages([message], canonical_context=aw._source_context,
                                                   source_registry=registry)
    assert output[0] is message
    assert stats['message_status_counts'] == {'UNREGISTERED_CITATION': 1}
    output, stats = project_agent_evidence_messages([message], canonical_context=aw._source_context)
    assert output[0] is message
    assert stats['message_status_counts'] == {'SOURCE_REGISTRY_UNAVAILABLE': 1}


def test_direct_source_metadata_ambiguous_business_facts_survive():
    data, registry = registered()
    facts = {'id': 'invoiceA', 'score': 98, 'generation': 'third', 'sheet': 'T07', 'unit': '千'}
    registry[0]['source_metadata'] = {**facts, 'parent_chunk_id': 'technical-parent',
                                     'source_sha256': 'a' * 64}
    registry[0].update(id='technical-citation-id', score=.9)
    message = tool(data)
    before = copy.deepcopy((data, registry, message.model_dump()))
    output, stats = project_agent_evidence_messages([message], canonical_context=aw._source_context,
                                                   source_registry=registry)
    citation = json.loads(output[0].content)['citations'][0]
    assert citation['source_metadata'] == facts
    assert 'id' not in citation and 'score' not in citation
    assert stats['message_status_counts'] == {'PROJECTED': 1}
    assert before == (data, registry, message.model_dump())
