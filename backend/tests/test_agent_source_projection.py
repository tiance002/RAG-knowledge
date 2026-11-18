import json
from langchain_core.messages import ToolMessage, HumanMessage
from app.services.agent_evidence_projection import encode_agent_sources
from app.services.agent_context_budget import consolidate_agent_context, request_chars
from app.services.agent_source_registry import RequestSourceRegistry


def context(sources):
    return '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}" for c in sources)


def fixture_sources():
    return [{'source_id': 1, 'chunk_id': 'private-uuid-a', 'fileId': 'file-a', 'content': 'Revenue 42 (in thousands).', 'fileName': 'report.xlsx'},
            {'source_id': 2, 'chunk_id': 'private-uuid-b', 'fileId': 'file-b', 'content': 'Literal [s9] is part of this source.', 'fileName': 'manual.txt'}]


def tool(sources, ident='call'):
    return ToolMessage(content=json.dumps({'context': context(sources), 'source_ids': [c['source_id'] for c in sources],
        'citations': [{'source_id': c['source_id'], 'title': c['fileName']} for c in sources]}),
        name='search_knowledge_base', tool_call_id=ident)


def test_provider_view_aliases_and_output_restore_preserve_canonical_sources():
    sources=fixture_sources(); message=tool(sources); original=message.content
    received,stats=encode_agent_sources([message],sources)
    wire=json.loads(received[0].content)
    assert wire['source_ids']==['s1','s2']
    assert wire['citations'][0]['source_id']=='s1'
    assert 'private-uuid' not in received[0].content
    assert all(c['content'] in wire['context'] for c in sources)
    assert message.content==original and sources[0]['source_id']==1
    text,diag=RequestSourceRegistry(sources).restore_known_aliases('42 [s1]; other [s2]; unknown [s9]',[1])
    assert text=='42 [1]; other [s2]; unknown [s9]'
    assert diag['restored_alias_count']==1 and diag['undelivered_alias_count']==1 and diag['unknown_alias_count']==1


def test_final_encoded_size_is_the_budget_measurement():
    sources=fixture_sources(); messages=[HumanMessage(content='Explain revenue'),tool(sources)]
    measure=lambda rows:request_chars(encode_agent_sources(rows,sources)[0])
    selected,receipt=consolidate_agent_context(messages,source_registry=sources,canonical_context=context,
        limit_chars=3000,original_query='Explain revenue',measure_messages=measure)
    wire,_=encode_agent_sources(selected,sources)
    assert receipt['request_chars']==request_chars(wire)<=3000
    assert receipt['source_ids']==[1,2]


def test_cross_round_duplicate_body_only_delivered_once_with_aliases():
    sources=fixture_sources(); messages=[HumanMessage(content='Explain revenue'),tool(sources,'old'),tool(sources,'new')]
    selected,receipt=consolidate_agent_context(messages,source_registry=sources,canonical_context=context,limit_chars=4000)
    wire,_=encode_agent_sources(selected,sources)
    assert sum(json.loads(m.content)['context'].count(sources[0]['content']) for m in wire if isinstance(m,ToolMessage))==1
    assert [m.tool_call_id for m in wire if isinstance(m,ToolMessage)]==['old','new']


def test_read_result_does_not_leak_requested_internal_chunk_identity():
    from app.services.agent_evidence_projection import project_agent_evidence_messages
    sources=fixture_sources()
    data={'context':context(sources),'source_ids':[1,2],'citations':sources,'requested_chunk_id':'private-uuid-a'}
    message=ToolMessage(content=json.dumps(data),name='read_knowledge_context',tool_call_id='read')
    projected,_=project_agent_evidence_messages([message],canonical_context=context,source_registry=sources)
    wire,_=encode_agent_sources(projected,sources)
    payload=json.loads(wire[0].content)
    assert 'requested_chunk_id' not in payload
    assert 'private-uuid-a' not in wire[0].content
    assert all(c['content'] in payload['context'] for c in sources)
