"""No provider calls: validate Agent routing boundary and preserved OFF path."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.services import agent_workflow, grounded_generation


def test_off_does_not_resolve_bindings(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setattr(grounded_generation, '_route_generation', lambda *a, **k: (_ for _ in ()).throw(AssertionError('OFF')))
    llm = SimpleNamespace(model='baseline')
    assert agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)._route_post_retrieval_model({}, llm, []) is llm


def test_failed_retrieval_never_promotes_model(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setattr(grounded_generation, '_route_generation', lambda *a, **k: (_ for _ in ()).throw(AssertionError('retrieval failure')))
    llm = SimpleNamespace(model='baseline')
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    for status in ('NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR', 'UNKNOWN'):
        state = {'kb_ids': ['kb'], 'retrieval_status': status, 'citations': [{'content': 'fragment'}]}
        assert instance._route_post_retrieval_model(state, llm, []) is llm


def test_success_uses_original_question_and_actual_messages(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    calls = []
    selected = SimpleNamespace(model='cheap')
    def route(*args, **kwargs):
        calls.append((args, kwargs))
        return selected, 'cheap'
    monkeypatch.setattr(grounded_generation, '_route_generation', route)
    state = {'kb_ids': ['kb'], 'retrieval_status': 'SUCCESS', 'citations': [{'content': 'source'}], 'original_query': 'original'}
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    assert instance._route_post_retrieval_model(state, SimpleNamespace(model='baseline'), [SimpleNamespace(type='human', content='rewrite hint')]) is selected
    assert calls[0][1]['question'] == 'original'
    assert calls[0][0][2] == [{'role': 'user', 'content': 'rewrite hint'}]


@pytest.mark.parametrize('quality,role,used,answer,finish,expected', [
    ('REFERENCE_RANGE', 'CHEAP', 0, 'invalid [2]', 'stop', 2),
    ('REFERENCE_RANGE', 'CHEAP', 0, 'valid [1]', 'stop', 1),
    ('REFERENCE_RANGE', 'CHEAP', 0, 'no references', 'stop', 1),
    ('REFERENCE_RANGE', 'EXPENSIVE', 0, 'invalid [2]', 'stop', 1),
    ('REFERENCE_RANGE', 'CHEAP', 1, 'invalid [2]', 'stop', 1),
    ('REFERENCE_RANGE', 'CHEAP', 0, 'invalid [2]', 'length', 1),
    ('NONE', 'CHEAP', 0, 'invalid [2]', 'stop', 1),
])
def test_agent_reference_range_escalation(monkeypatch, quality, role, used, answer, finish, expected):
    from app.services.model_router import RouterConfig
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', quality)
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    instance.tools = []
    def fake_llm(name):
        bound = MagicMock()
        bound.bind_tools.return_value = bound
        return SimpleNamespace(model=name, get_chat_model=lambda **kwargs: bound)
    cheap, expensive = fake_llm('cheap'), fake_llm('expensive')
    route = SimpleNamespace(mode='DYNAMIC', selected_role=role)
    config = RouterConfig(mode='DYNAMIC')
    instance._get_llm_service = lambda state: cheap
    instance._route_post_retrieval_model = lambda *args, **kwargs: (cheap, route, config)
    monkeypatch.setattr(grounded_generation, '_role_service', lambda role, expected: expensive)
    first = AIMessage(content=answer, response_metadata={'finish_reason': finish, 'model_name': 'cheap'})
    second = AIMessage(content='still invalid [9]', response_metadata={'finish_reason': 'stop'})
    invoke = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    monkeypatch.setattr(agent_workflow, '_finalize_knowledge_answer', lambda state, response, include_mode: (state['citations'], 'GROUNDED_GENERATION'))
    state = {'messages': [HumanMessage(content='q')], 'citations': [{'content': 'evidence'}],
             'router_escalations_used': used}
    result = asyncio.run(instance.agent_node(state, {}))
    assert invoke.await_count == expected
    if expected == 2:
        assert invoke.await_args_list[0].args[1] == invoke.await_args_list[1].args[1]
        assert result['router_escalations_used'] == 1


def test_agent_tool_call_is_not_a_final_answer(monkeypatch):
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    instance.tools = []
    model = MagicMock()
    model.bind_tools.return_value = model
    llm = SimpleNamespace(model='cheap', get_chat_model=lambda **kwargs: model)
    instance._get_llm_service = lambda state: llm
    instance._route_post_retrieval_model = lambda *args, **kwargs: (
        llm, SimpleNamespace(mode='DYNAMIC', selected_role='CHEAP'), object())
    response = AIMessage(content='invalid [2]', response_metadata={'finish_reason': 'stop'},
                         tool_calls=[{'name': 'search_knowledge_base', 'args': {}, 'id': 'call1'}])
    outcomes = []
    monkeypatch.setattr(grounded_generation, 'event', lambda name, **kwargs: outcomes.append((name, kwargs)))
    invoke = AsyncMock(return_value=response)
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    result = asyncio.run(instance.agent_node({'messages': [HumanMessage(content='q')],
                                              'citations': [{'content': 'evidence'}]}, {}))
    assert invoke.await_count == 1
    assert result['messages'][0].tool_calls
    assert next(data['status'] for name, data in outcomes if name == 'router.outcome') == 'TOOL_CALL'



def test_routed_agent_binding_passes_thinking_without_changing_off_model():
    model = MagicMock()
    bound_tools = model.bind_tools.return_value
    llm = SimpleNamespace(get_chat_model=lambda **kwargs: model)
    assert grounded_generation._routed_chat_model(llm, []) is bound_tools
    bound_tools.bind.assert_not_called()
    llm._router_extra_body = {'enable_thinking': False}
    assert grounded_generation._routed_chat_model(llm, []) is bound_tools.bind.return_value
    bound_tools.bind.assert_called_once_with(extra_body={'enable_thinking': False})



def test_agent_real_router_uses_server_role_and_exact_post_retrieval_context(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'CHEAP_ONLY')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    selected = SimpleNamespace(model='Qwen/Qwen3.5-4B')
    bindings = []
    monkeypatch.setattr(grounded_generation, '_role_service',
                        lambda role, expected: bindings.append((role, expected)) or selected)
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    state = {'kb_ids': ['kb'], 'retrieval_status': 'SUCCESS', 'original_query': 'q',
             'citations': [{'content': 'source evidence', 'chunk_id': 'c1'}]}
    routed, decision, config = instance._route_post_retrieval_model(
        state, SimpleNamespace(model='baseline'), [HumanMessage(content='q')], return_decision=True)
    assert routed is selected
    assert bindings == [('CHEAP', 'Qwen/Qwen3.5-4B')]
    assert decision.selected_model == selected.model
    assert config.mode == 'CHEAP_ONLY'


@pytest.mark.parametrize('outcome', ['UNKNOWN', 'MISMATCH', 'TIMEOUT'])
def test_agent_uncertain_identity_or_provider_failure_never_replays(monkeypatch, outcome):
    from app.services.model_router import RouterConfig
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    instance = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    instance.tools = []
    llm = SimpleNamespace(model='cheap', get_chat_model=lambda **kwargs: MagicMock())
    instance._get_llm_service = lambda state: llm
    instance._route_post_retrieval_model = lambda *args, **kwargs: (
        llm, SimpleNamespace(mode='DYNAMIC', selected_role='CHEAP'), RouterConfig(mode='DYNAMIC'))
    metadata = {'finish_reason': 'stop'}
    if outcome == 'MISMATCH':
        metadata['model_name'] = 'other'
    response = AIMessage(content='invalid [2]', response_metadata=metadata)
    invoke = AsyncMock(return_value=response, side_effect=TimeoutError('uncertain') if outcome == 'TIMEOUT' else None)
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    monkeypatch.setattr(agent_workflow, '_finalize_knowledge_answer', lambda *args, **kwargs: ([], 'EXTRACTIVE_FALLBACK'))
    monkeypatch.setattr(grounded_generation, '_role_service', lambda *args: pytest.fail('no replay'))
    state = {'messages': [HumanMessage(content='q')], 'citations': [{'content': 'source'}]}
    if outcome == 'TIMEOUT':
        with pytest.raises(TimeoutError):
            asyncio.run(instance.agent_node(state, {}))
    else:
        asyncio.run(instance.agent_node(state, {}))
    assert invoke.await_count == 1
