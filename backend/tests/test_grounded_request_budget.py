import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.grounded_request_budget import pack_grounded_request, request_chars
from app.services import grounded_generation as generation


def render(history, citations):
    return [{'role':'system','content':'Instructions\n'+'\n'.join(f"[{i}] {c['content']}" for i,c in enumerate(citations,1))}] + history


def test_complete_old_turns_removed_before_evidence_without_input_mutation():
    messages=[{'role':'system','content':'mandatory policy'},
              {'role':'user','content':'old topic'},{'role':'assistant','content':'unrelated '*1000},
              {'role':'user','content':'which project?'},{'role':'assistant','content':'Project B'},
              {'role':'user','content':'What is its cost, in thousands?'}]
    citations=[{'chunk_id':'a','content':'Project B: cost 12 thousand; only if approved.'},
               {'chunk_id':'b','content':'Except emergencies, do not include unpaid commitments.'}]
    original=copy.deepcopy((messages,citations))
    history,sources,request,summary=pack_grounded_request(messages,citations,render,limit=700)
    assert sources==citations and (messages,citations)==original
    assert history==[messages[0]]+messages[3:]
    assert request_chars(request)<=700 and summary['omitted_history_turns']==1
    assert summary['omitted_citations']==[] and summary['source_bodies_cropped'] is False
    assert sum(summary[k] for k in ('fixed_request_chars','history_overhead_chars','evidence_with_labels_chars'))==summary['request_chars']


def test_exact_json_boundary_counts_quotes_backslashes_unicode():
    messages=[{'role':'user','content':'中文 question "\\\n'}]
    sources=[{'content':'10万元，除非取消。 "\\\n'}]
    size=request_chars(render(messages,sources))
    _,selected,request,summary=pack_grounded_request(messages,sources,render,limit=size)
    assert request_chars(request)==size and selected==sources and summary['omitted_history_turns']==0
    _,selected,request,summary=pack_grounded_request(messages,sources,render,limit=size-1)
    assert selected==sources and request_chars(request)==size  # protected original, final guard must refuse


@pytest.mark.parametrize('content', [None, 7, {'unexpected': 'body'}])
def test_nontext_body_counts_no_text_without_mutating_evidence_or_request_budget(content):
    messages = [{'role': 'user', 'content': 'Current question'}]
    sources = [{'content': content}, {'content': '真实 text'}]
    original = copy.deepcopy(sources)
    _, selected, request, summary = pack_grounded_request(messages, sources, render, limit=2000)
    assert selected == sources == original
    assert summary['evidence_body_chars'] == len('真实 text')
    assert summary['request_chars'] == request_chars(render(messages, sources)) == request_chars(request)


def test_neighbor_removed_before_ranked_seed_and_whole_bodies_stay_intact():
    messages=[{'role':'user','content':'Compare A and B'}]
    sources=[{'chunk_id':'a','content':'A: 12 kg; not 12 dollars.'},
             {'chunk_id':'b','content':'B: 14 kg, if condition X.'},
             {'chunk_id':'neighbor','content':'unrelated context '*200,'evidence_relation':'neighbor'}]
    _,selected,_,summary=pack_grounded_request(messages,sources,render,limit=400)
    assert selected==sources[:2]
    assert summary['omitted_citations']==[{'original_index':2,'chunk_id':'neighbor','file_id':None,'reason':'FINAL_REQUEST_BUDGET'}]


def test_sheet_and_native_proofs_protected_even_when_low_ranked():
    sources=[{'chunk_id':'a','content':'first'}, {'chunk_id':'b','content':'noise '*300},
             {'chunk_id':'sheet','content':'Column: cost, units: thousands; 12'},
             {'chunk_id':'pdf','content':'Except: 4','source_type':'native_source_expansion'}]
    _,selected,request,summary=pack_grounded_request([{'role':'user','content':'cost?'}],sources,render,limit=400,protected_indices=[2])
    assert [c['chunk_id'] for c in selected]==['a','sheet','pdf'] and request_chars(request)<=400
    assert summary['omitted_citations'][0]['chunk_id']=='b'


def test_latest_huge_history_removed_before_required_seed_sources():
    messages=[{'role':'user','content':'old'},{'role':'assistant','content':'x'*5000},{'role':'user','content':'Compare current evidence'}]
    sources=[{'content':'A: 12'},{'content':'B: 14'}]
    history,selected,_,_=pack_grounded_request(messages,sources,render,limit=400)
    assert history==messages[-1:] and selected==sources


@pytest.mark.parametrize('streamed',[False,True])
def test_actual_provider_receives_packed_context_and_complete_current_question(monkeypatch, streamed):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE','OFF')
    monkeypatch.setattr(generation.get_settings(),'GROUNDED_REQUEST_MAX_CHARS',1800)
    create=AsyncMock()
    response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'),finish_reason='stop')],usage=None)
    llm=SimpleNamespace(model='offline',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    captured=[]
    async def stream(**kwargs):
        captured.append(kwargs['messages']);kwargs['on_final']({'finish_reason':'stop','usage':{}})
        yield 'answer [1]'
    llm.generate_stream=stream
    messages=[{'role':'user','content':'old'},{'role':'assistant','content':'x'*5000},
              {'role':'user','content':'current question with unit kg'}]
    sources=[{'chunk_id':'a','content':'if valid, 12 kg; otherwise 0 kg.'}]
    async def run():
        if streamed:
            return [x async for x in generation.stream_grounded_candidate(llm,messages,sources,preference_policy='concise')]
        create.return_value=response
        return await generation.generate_grounded_candidate(llm,messages,sources,preference_policy='concise')
    asyncio.run(run())
    sent=captured[0] if streamed else create.await_args.kwargs['messages']
    assert request_chars(sent)<=1800
    assert sent[-1]==messages[-1] and sources[0]['content'] in sent[0]['content'] and 'concise' in sent[0]['content']
    assert not any(m.get('content')=='x'*5000 for m in sent)


def test_irreducible_question_or_policy_rejected_before_provider(monkeypatch):
    monkeypatch.setattr(generation.get_settings(),'GROUNDED_REQUEST_MAX_CHARS',100)
    create=AsyncMock();llm=SimpleNamespace(model='offline',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(ValueError,match='FINAL_REQUEST_OVER_CHAR_BUDGET'):
        asyncio.run(generation.generate_grounded_candidate(llm,[{'role':'user','content':'x'*200}],[{'content':'proof'}]))
    create.assert_not_awaited()


@pytest.mark.parametrize('streamed',[False,True])
def test_final_sse_citations_match_actual_provider_after_evidence_selection(monkeypatch,streamed):
    from app.services import chat
    from app.schemas import Message
    monkeypatch.setenv('RAG_GROUNDED_GENERATION','true')
    monkeypatch.setenv('RAG_GROUNDED_STREAM_ENABLED','true' if streamed else 'false')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE','OFF')
    monkeypatch.setattr(generation.get_settings(),'GROUNDED_REQUEST_MAX_CHARS',2000)
    class Session:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(chat,'Session',Session)
    sent=[]
    async def create(**kwargs):
        sent.append(kwargs['messages'])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='12 kg [1]'),finish_reason='stop')],usage=None)
    async def stream(**kwargs):
        sent.append(kwargs['messages']);yield '12 kg [1]'
        kwargs['on_final']({'finish_reason':'stop','usage':{}})
    llm=SimpleNamespace(model='offline',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),generate_stream=stream)
    service=chat.ChatService.__new__(chat.ChatService)
    service.llm_service=llm;service._resolved_llm=lambda model:llm
    service._runtime_snapshot=lambda:SimpleNamespace(max_chat_history_rounds=20)
    bodies=['A: 12 kg, unless cancelled.','B: applies only in 2026.','noise '*1000]
    service._retrieve_with_new_service=AsyncMock(return_value=([
        dict(id=str(i),file_id='f'+str(i),file_name='report'+str(i),content=body,score=1-i*.1)
        for i,body in enumerate(bodies)],{'retrieval_status':'SUCCESS'}))
    async def collect():
        return [e async for e in service._chat_stream_normal([Message(role='user',content='Compare A and B')],['kb'],5,0,None,False,0,None)]
    events=asyncio.run(collect())
    assert events[-1][0]=='done' and not any(k=='error' for k,v in events)
    final=next(v for k,v in reversed(events) if k=='rag_result')
    assert [c['content'] for c in final['citations']]==bodies[:2]
    assert final['request_budget']['delivered_citation_count']==2 and len(final['request_budget']['omitted_citations'])==1
    assert request_chars(sent[0])<=2000 and all(body in sent[0][0]['content'] for body in bodies[:2])
    assert bodies[2] not in sent[0][0]['content'] and len(sent)==1
