"""Buffered generation using only the actual selected, source-linked evidence."""
import asyncio
from app.services.request_stage_timing import benchmark_timed
import os
import re
from urllib.parse import urlsplit
from app.core.config import get_settings
from app.services.observability import event, model_response, snapshot, capture, observe_model_history
from app.services.llm import provider_generation, _provider_id, _response_usage, _update_generation, _completion_status, is_unknown_provider_error, attach_provider_usage, GenerationInputBudgetExceeded
from app.services.llm import retry_answer_call
from app.services.grounded_request_budget import PreparedGroundedRequest, pack_grounded_request, request_chars


def grounded_generation_enabled():
    return os.getenv('RAG_GROUNDED_GENERATION', 'true').lower() == 'true'


class RoutingBlocked(RuntimeError):
    """Routing is configured but the server has not authorized a safe call."""


class GroundedGenerationResult(tuple):
    """Three-value result with an optional final-call budget receipt.

    Existing callers retain tuple unpacking. The initial request receipt remains
    separate; a caller can publish this additional receipt before answer text.
    """
    def __new__(cls, content, finish_reason, usage, *, final_request_budget):
        result = super().__new__(cls, (content, finish_reason, usage))
        result.final_request_budget = final_request_budget
        return result


def routing_is_off():
    """A trusted server marker disables routing for the next call, no restart."""
    marker = os.getenv('RAG_MODEL_ROUTER_OFF_FILE', '').strip()
    return (os.getenv('RAG_MODEL_ROUTER_MODE', 'OFF').strip().upper() == 'OFF'
            or bool(marker and os.path.exists(marker)))


def route_direct_history_generation(llm, model_id, messages, question):
    """Bounded arithmetic follow-up preference using actual conversation only.

    This path has no retrieved evidence and cannot establish retrieval success.
    Ordinary direct chat and fixed router modes retain their original service.
    """
    baseline = (llm, model_id or llm.model)
    if routing_is_off():
        return baseline
    from app.services.model_router import (RouterConfig, quality_first_candidate, QUALITY_FIRST_VERSION,
                                           qwen_risk_v4, RISK_V4_VERSION, qwen_risk_v5, RISK_V5_VERSION,
                                           qwen_risk_v6, RISK_V6_VERSION, RISK_E_VERSION)
    config = RouterConfig.from_env()
    if config.mode != 'DYNAMIC' or config.selection_policy not in ('QUALITY_FIRST_V3', 'RISK_V4', 'RISK_V5', 'RISK_V6', 'RISK_E'):
        return baseline
    # Only assistant messages preceding the current user turn are history.
    current = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=len(messages))
    has_history = any(m.get('role') == 'assistant' and isinstance(m.get('content'), str)
                      and m['content'].strip() for m in messages[:current])
    candidate = quality_first_candidate(question)
    risk = None
    if config.selection_policy in ('RISK_V4', 'RISK_V5', 'RISK_V6', 'RISK_E'):
        risk = (qwen_risk_v6(question, [], messages) if config.selection_policy in ('RISK_V6', 'RISK_E')
                else qwen_risk_v5(question, [], messages) if config.selection_policy == 'RISK_V5'
                else qwen_risk_v4(question, [], messages[:current]))
        eligible = (has_history and risk['status'] == 'ESTIMATED'
                    and risk['signals'].get('arithmetic', False)
                    and risk['score_points'] / 100 >= config.threshold)
        matched_rules = risk['matched_rules']
        version = {'RISK_V4': RISK_V4_VERSION, 'RISK_V5': RISK_V5_VERSION,
                   'RISK_V6': RISK_V6_VERSION, 'RISK_E': RISK_E_VERSION}[config.selection_policy]
    else:
        eligible = has_history and 'DERIVED_NUMERIC_OPERATION' in candidate['risk']
        matched_rules = candidate['risk']
        version = QUALITY_FIRST_VERSION
    if not eligible:
        return baseline
    if os.getenv('RAG_ROUTER_ALLOW_MODEL_CALLS', '').strip().lower() != 'true':
        event('router.blocked', stage='direct_history', evidence_scope='HISTORY_ONLY',
              evidence_count=0, reason='MODEL_CALLS_NOT_AUTHORIZED')
        raise RoutingBlocked('Router model calls are not authorized')
    enforce_request_budget(messages)
    selected = _role_service('EXPENSIVE', config.expensive_model)
    selected._router_direct_history = True
    event('router.decision', stage='direct_history', evidence_scope='HISTORY_ONLY',
          evidence_count=0, role='EXPENSIVE', model=config.expensive_model,
          reason='DIRECT_HISTORY_DERIVED_NUMERIC_OPERATION', matched_rules=matched_rules,
          rule_version=version, status='ESTIMATED', calibration='UNCALIBRATED',
          risk_points=risk['score_points'] if risk else None,
          interpretation='ESTIMATED_RISK_NOT_PROBABILITY' if risk else 'ESTIMATED_BENEFIT_NOT_PROBABILITY')
    return selected, selected.model


def _reference_range_quality(answer, citation_count):
    """Check explicit numeric references only; absence supplies no verdict."""
    refs = re.findall(r'\[(\d+)\]', answer)
    if not refs:
        return 'UNKNOWN'
    return 'FAIL' if any(int(ref) < 1 or int(ref) > citation_count for ref in refs) else 'PASS'


def _role_service(role, expected_model):
    """Resolve a server-owned binding, never a caller-provided model ID."""
    from sqlmodel import Session
    from app.core.database import engine
    from app.models import CustomModel, ModelType
    from app.services.llm import LLMService

    binding_id = os.getenv('RAG_ROUTER_' + role + '_MODEL_ID', '').strip()
    if not binding_id:
        raise RoutingBlocked('Router model binding is not configured')
    with Session(engine) as session:
        binding = session.get(CustomModel, binding_id)
        if (binding is None or not binding.is_active or binding.model_type != ModelType.LLM
                or binding.model_name != expected_model or not binding.api_key or not binding.base_url):
            raise RoutingBlocked('Router model binding is invalid')
        parsed = urlsplit(binding.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise RoutingBlocked('Router model endpoint is invalid')
        service = LLMService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
        thinking = os.getenv('RAG_ROUTER_' + role + '_ENABLE_THINKING')
        if thinking is not None and thinking.strip():
            if thinking.strip().lower() not in ('true', 'false'):
                raise RoutingBlocked('Router enable_thinking must be true or false')
            service._router_extra_body = {'enable_thinking': thinking.strip().lower() == 'true'}
        return service


def _routed_chat_model(service, tools):
    model = service.get_chat_model(streaming=False).bind_tools(tools)
    parameters = _provider_parameters(service, service.model)
    return model.bind(**parameters) if parameters else model


def _provider_parameters(service, target):
    extra_body = getattr(service, '_router_extra_body', None)
    resolver = getattr(service, 'provider_request_parameters', None)
    if resolver is not None:
        return resolver(target, extra_body=extra_body)
    return {'extra_body': extra_body} if extra_body is not None else {}


def _routed_response_identity(response, requested_model, *, stage):
    # A configured alias is a request, not proof of the responding model.
    metadata = getattr(response, 'response_metadata', None) or {}
    actual = getattr(response, 'model', None) or metadata.get('model_name') or metadata.get('model')
    actual = actual if isinstance(actual, str) and actual.strip() else None
    status = 'UNKNOWN' if actual is None else 'MATCH' if actual == requested_model else 'MISMATCH'
    event('router.provider_identity', stage=stage, requested_model_id=requested_model,
          provider_model_id=actual, status=status)
    return status


def _router_outcome(route, model, *, finish=None, content=None, escalations=0, error=None, stage='grounded_answer', selected_role=None, tool_calls=False):
    if route is None:
        return
    if error is not None:
        status = ('CANCELLED' if isinstance(error, (asyncio.CancelledError, GeneratorExit))
                  else 'UNKNOWN' if is_unknown_provider_error(error) else 'PROVIDER_FAILURE')
    elif tool_calls:
        status = 'TOOL_CALL'
    else:
        status = ('TRUNCATED' if finish == 'length' else 'UNKNOWN' if finish != 'stop'
                  else 'COMPLETED' if isinstance(content, str) and content.strip() else 'EMPTY')
    event('router.outcome', stage=stage, status=status, role=selected_role or ('EXPENSIVE' if escalations else route.selected_role),
          model=model, retry_count=escalations, cost={'status': 'UNKNOWN'})

def _route_generation(llm, model_id, messages, citations, *, question=None, retrieval_status="SUCCESS", return_decision=False):
    """Select a final-answer model without altering the evidence or prompt."""
    mode = os.getenv('RAG_MODEL_ROUTER_MODE', 'OFF').strip().upper()
    if routing_is_off():
        result = (llm, model_id or llm.model)
        return (*result, None, None) if return_decision else result
    if retrieval_status != 'SUCCESS' or not any(isinstance(c.get('content'), str) and c['content'].strip() for c in citations):
        event('router.blocked', reason='NO_USABLE_RETRIEVAL')
        raise RoutingBlocked('Router requires usable retrieval evidence')
    # This is an explicit operational switch, not a monthly budget ledger or
    # source-level privacy authorization. The deployment must supply those.
    if os.getenv('RAG_ROUTER_ALLOW_MODEL_CALLS', '').strip().lower() != 'true':
        event('router.blocked', reason='MODEL_CALLS_NOT_AUTHORIZED')
        raise RoutingBlocked('Router model calls are not authorized')
    from app.services.model_router import RouterConfig, decide_route
    config = RouterConfig.from_env()
    if (getattr(config, 'escalate_on_truncated', False) or getattr(config, 'escalate_on_provider_fallback', False)
            or getattr(config, 'escalate_on_unsupported', False)):
        raise RoutingBlocked('V1 runtime does not implement optional truncation/provider/unsupported retry policies')
    if question is None:
        question = next((m.get('content', '') for m in reversed(messages)
                         if m.get('role') == 'user'), '')
    decision = decide_route(question, citations, messages=messages, retrieval_status=retrieval_status, config=config,
                            baseline_model=model_id or llm.model)
    event('router.decision', **decision.to_payload())
    if decision.selected_role == 'BASELINE':
        result = (llm, model_id or llm.model)
        return (*result, decision, config) if return_decision else result
    if decision.selected_role not in ('CHEAP', 'EXPENSIVE'):
        raise RoutingBlocked('Router did not select an authorized model')
    selected = _role_service(decision.selected_role, decision.selected_model)
    result = (selected, selected.model)
    return (*result, decision, config) if return_decision else result


def delivered_evidence_records(citations):
    """Observation identities distinguish native proofs from indexed chunks."""
    output = []
    for index, citation in enumerate(citations, 1):
        source = citation.get('source_metadata') or {}
        native = citation.get('source_type') in ('source_native_table_expansion', 'native_source_expansion')
        chunk_id = None if native else citation.get('chunk_id') or citation.get('id')
        source_sha = citation.get('source_sha256') or source.get('source_sha256')
        evidence_id = (('native:' + str(source_sha) + ':' + str(source.get('page')) + ':' + str(index))
                       if source_sha and source.get('page') is not None else None) if native else chunk_id
        output.append({'rank': index, 'evidence_id': evidence_id, 'chunk_id': chunk_id,
                       'parent_chunk_id': citation.get('parent_chunk_id'), 'file_id': citation.get('fileId') or citation.get('file_id'),
                       'content': citation.get('content', ''), 'source_sha256': source_sha,
                       'source_type': 'native_source_expansion' if native else 'indexed_chunk' if chunk_id else 'other',
                       'position': {k: source.get(k) for k in ('page', 'table_bbox', 'sheet', 'row', 'cell')}})
        if citation.get('context_group_id'):
            output[-1].update(context_group_id=citation['context_group_id'],
                              context_group_ids=citation.get('context_group_ids', []),
                              evidence_relation=citation.get('evidence_relation'),
                              seed_evidence_ids=citation.get('seed_evidence_ids', []),
                              content_hash=citation.get('content_hash'))
    return output


def build_grounded_prompt(evidence, *, addendum=''):
    """Build the ordinary answer prompt; an optional factory-owned addition is explicit.

    Empty additions preserve the existing prompt byte for byte. This hook does
    not select evidence, resolve preferences, route models or validate answers.
    """
    prompt = ('请根据以下参考资料回答用户问题，可以做与问题有关的综合解释。新增具体事实、单位或归因应有资料依据，推断请说明依据。关键结论注明对应的来源编号，例如 [1]；资料不足时说明缺少的部分，不把常识补充当作资料结论。\n\n资料：\n' + evidence)
    return prompt + '\n\n' + addendum if addendum else prompt


def enforce_request_budget(messages, tools=None):
    """Count complete application messages/tool schemas in Unicode characters.

    Provider framing/tokenization is not known here; the provider token limit is
    an additional constraint. Fail before a paid call rather than lose evidence.
    """
    measured = request_chars(messages, tools)
    request_limit = get_settings().GROUNDED_REQUEST_MAX_CHARS
    if measured > request_limit:
        event('context.budget_exceeded', request_chars=measured, limit_chars=request_limit,
              degradation_reason='FINAL_REQUEST_OVER_CHAR_BUDGET')
        raise GenerationInputBudgetExceeded(measured, request_limit)
    return measured


def _build_grounded_request(llm, messages, citations, spreadsheet_context=None, *, register=True):
    if register:
        from app.services.conversation_context import register_request_sources
        register_request_sources(citations)
    evidence = '\n\n'.join(
        f"[{c.get('source_alias') or i}] 来源：{c.get('fileName', '未知')} {c.get('location', '')}\n{c['content']}"
        for i, c in enumerate(citations, 1)
    )
    prompt = build_grounded_prompt(evidence, addendum=getattr(llm, '_grounded_prompt_addendum', ''))
    prompt += '\n历史问答仅帮助理解对话与用户要求，不是独立的原文证据。事实与引用以本次提供的来源正文为依据；历史来源编号不能直接沿用。'
    if spreadsheet_context is not None:
        proof = spreadsheet_context.proof
        footnotes = '\n'.join(item['text'] for item in proof['footnotes'].values())
        prompt += ('\n\n工作簿来源上下文：\n' + proof['title']
                   + '\n列头：' + ' '.join(value for _, _, value in proof['headers'])
                   + '\n行：' + str(proof['label']) + '；数值：' + str(proof['value'])
                   + '\n脚注：' + footnotes + '\n说明：' + '\n'.join(proof['notes'])
                   + '\n来源：' + proof['source'])
    # PDF expansion content, headers and notes are already present in citations.
    request_messages = [{'role': 'system', 'content': prompt}] + list(messages)
    return prompt, request_messages


def prepare_grounded_request(llm, messages, citations, *, spreadsheet_context=None, preference_policy='', model_id=None, retrieval_status='SUCCESS'):
    """One exact budget shared by buffered/streamed generation and citation SSE."""
    def build(history, evidence):
        prompt, request = _build_grounded_request(llm, history, evidence, spreadsheet_context, register=False)
        if preference_policy:
            request[0] = {'role': 'system', 'content': prompt + '\n\n' + preference_policy}
        return request
    protected = ([spreadsheet_context.citation_index - 1] if spreadsheet_context is not None else [])
    history, selected, request, summary = pack_grounded_request(messages, citations, build,
        limit=get_settings().GROUNDED_REQUEST_MAX_CHARS, protected_indices=protected)
    enforce_request_budget(request)
    # Display preferences must not affect model-routing features.
    _, routing = _build_grounded_request(llm, history, selected, spreadsheet_context, register=False)
    prepared = PreparedGroundedRequest(request, selected, summary, routing)
    from app.services.cheap_grounding import enabled
    if enabled():
        prepared = _resolve_cheap_grounding(prepared, llm, model_id, retrieval_status)
    event('context.request_budget', **prepared.summary)
    from app.services.conversation_context import register_request_sources
    register_request_sources(prepared.citations)
    return prepared


def _resolve_cheap_grounding(prepared, llm, model_id, retrieval_status):
    """Route once before SSE, retaining original inputs for expensive retries."""
    from dataclasses import replace
    from app.services import cheap_grounding
    resolution = _route_generation(llm, model_id, prepared.routing_messages,
        prepared.citations, retrieval_status=retrieval_status, return_decision=True)
    selected_llm, target, route, config = resolution
    cached = replace(prepared, route_resolution=resolution, original_messages=prepared.messages)
    configured_name = os.getenv('RAG_ROUTER_CHEAP_MODEL', 'Qwen/Qwen3.5-4B').strip()
    eligible, binding_reason = _explicit_cheap_binding(selected_llm, target, route,
                                                      model_id, configured_name)
    if not (prepared.citations and eligible):
        return replace(cached, summary={**prepared.summary,
            'cheap_grounding_status': 'NOT_APPLICABLE_NOT_EXPLICIT_CHEAP',
            'cheap_grounding_reason': binding_reason if prepared.citations else 'NO_GROUNDED_EVIDENCE'})
    fields = dict(model=target, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
                  temperature=0.7, stream=False, **_provider_parameters(selected_llm, target))
    try:
        request, citations, audit = cheap_grounding.prepare_cheap_request(
            prepared.messages, prepared.citations, provider_fields=fields,
            limit=get_settings().GROUNDED_REQUEST_MAX_CHARS)
    except ValueError as error:
        return replace(cached, summary={**prepared.summary,
            'cheap_grounding_status': 'CONTRACT_SKIPPED_UNSAFE_PACKING',
            'cheap_grounding_reason': str(error)})
    if audit['omitted_citations']:
        return replace(cached, summary={**prepared.summary,
            'cheap_grounding_status': 'CONTRACT_SKIPPED_WOULD_DROP_EVIDENCE'})
    summary = {**audit['pack_summary'], 'cheap_grounding_status': 'CONTRACT_APPLIED',
        'cheap_grounding_binding': binding_reason,
        'cheap_grounding_contract_sha256': cheap_grounding.CONTRACT_SHA256,
        'cheap_grounding_omitted_history': audit['omitted_history'],
        'provider_argument_chars': audit['provider_body_chars'],
        'original_budget': prepared.summary}
    enforce_request_budget(request)
    return replace(cached, messages=request, citations=citations, summary=summary)


def _explicit_cheap_binding(service, target, route, requested_id, configured_name):
    """Positive binding checks only; credentials never leave this comparison."""
    if target != configured_name or getattr(service, 'model', None) != configured_name:
        return False, 'TARGET_IS_NOT_CONFIGURED_CHEAP_MODEL'
    if route is not None and route.selected_role == 'CHEAP':
        # _route_generation obtains this service through validated _role_service.
        return True, 'ROUTER_VALIDATED_CHEAP_BINDING'
    if route is not None and route.selected_role != 'BASELINE':
        return False, 'ROUTE_IS_NOT_CHEAP_OR_BASELINE'
    binding_id = os.getenv('RAG_ROUTER_CHEAP_MODEL_ID', '').strip()
    if not binding_id:
        return False, 'CHEAP_BINDING_NOT_CONFIGURED'
    try:
        binding = _role_service('CHEAP', configured_name)
    except Exception:
        return False, 'CHEAP_BINDING_UNCONFIRMED'
    if getattr(binding, 'model', None) != target:
        return False, 'CHEAP_BINDING_TARGET_MISMATCH'
    if requested_id == binding_id:
        return True, 'EXPLICIT_CONFIGURED_CHEAP_BINDING_ID'
    # A model name alone cannot distinguish tenants/endpoints sharing a name.
    fields = ('base_url', 'api_key')
    if all(isinstance(getattr(service, k, None), str) and getattr(service, k)
           and getattr(service, k) == getattr(binding, k, None) for k in fields):
        return True, 'RESOLVED_SERVICE_MATCHES_CONFIGURED_CHEAP_BINDING'
    return False, 'BASELINE_CHEAP_BINDING_AMBIGUOUS'


@benchmark_timed('grounded_generation')
async def generate_grounded_candidate(llm, messages, citations, *, model_id=None, spreadsheet_context=None, pdf_context=None, quality_check=None, retrieval_status="SUCCESS", preference_policy='', prepared_request=None):
    """Return content, finish reason and usage; the caller checks completion."""
    prepared = prepared_request or prepare_grounded_request(llm, messages, citations,
        spreadsheet_context=spreadsheet_context, preference_policy=preference_policy,
        model_id=model_id, retrieval_status=retrieval_status)
    request_messages, citations = prepared.messages, prepared.citations
    llm, target, route, router_config = prepared.route_resolution or _route_generation(
        llm, model_id, prepared.routing_messages, citations, retrieval_status=retrieval_status, return_decision=True)
    # Enforce the final serialized request, including source labels, history,
    # supplementary proofs and preference prompt. This is a character budget,
    # not a claim about any provider's tokenizer or output-token allowance.
    enforce_request_budget(request_messages)
    observe_model_history(request_messages)
    identity_status = 'UNKNOWN'
    async def _generate_once(current_llm, target):
        nonlocal identity_status
        event('memory.delivery', stage='grounded_answer', preference_policy_applied=bool(preference_policy))
        provider_parameters = _provider_parameters(current_llm, target)
        event('grounded.request', model=target, prompt=snapshot(request_messages), evidence_count=len(citations))
        delivered_ids = [r['evidence_id'] for r in delivered_evidence_records(citations)]
        event('context.delivery', delivery_target='provider_request', is_final_model_delivery=True,
              delivery_status='PREPARED_FOR_PROVIDER', delivered_evidence_ids=delivered_ids,
              output=capture(delivered_evidence_records(citations), content_role='context_sent'))
        event('grounded.request.sent', input=capture(request_messages, content_role='provider_request'),
              model=target, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS, temperature=0.7, **provider_parameters)
        with provider_generation('grounded_answer', target, request_messages, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS, temperature=0.7, **provider_parameters) as (attempt, observation):
            if observation is not None:
                observation.update(delivered_evidence_ids=delivered_ids,
                    context_group_ids=list(dict.fromkeys(c['context_group_id'] for c in citations if c.get('context_group_id'))))
            from app.services.conversation_context import record_provider_request
            record_provider_request(request_messages, 'grounded_answer', citations)
            response = await current_llm._get_client().chat.completions.create(
                model=target, messages=request_messages, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
                temperature=0.7, stream=False, **provider_parameters,
            )
            attempt['provider_request_id'] = _provider_id(response)
            attach_provider_usage(attempt, response)
            if route is not None:
                identity_status = _routed_response_identity(response, target, stage='grounded_answer')
                _update_generation(observation, requested_model_id=target, provider_model_id=getattr(response, 'model', None),
                                   model_identity_status=identity_status)
            model_response(response, role='grounded_answer', model=target)
            choice = response.choices[0]
            usage = getattr(response, 'usage', None)
            usage = usage.model_dump() if hasattr(usage, 'model_dump') else usage
            content = choice.message.content or ''
            if observation is not None:
                _update_generation(observation, output=capture(content, content_role='provider_response'),
                                   usage=_response_usage(response), finish_reason=getattr(choice, 'finish_reason', None),
                                   execution_status=_completion_status(getattr(choice, 'finish_reason', None)))
            # Candidate text is never sent to the telemetry backend. Explicitly
            # enabled snapshots use the existing private local snapshot store.
            event('grounded.candidate', candidate=snapshot(content))
            event('grounded.response', output=capture(content, content_role='provider_response'),
                  finish_reason=getattr(choice, 'finish_reason', None), model=target)
            return content, getattr(choice, 'finish_reason', None), usage

    calls_used = 0
    final_request_budget = None
    async def generate_once(current_llm, target):
        nonlocal calls_used
        escalations = calls_used
        calls_used += 1
        try:
            return await retry_answer_call(lambda: _generate_once(current_llm, target), stage='grounded_answer')
        except BaseException as error:
            _router_outcome(route, target, escalations=escalations, error=error)
            raise

    result = await generate_once(llm, target)
    quality_status = 'NOT_RUN'
    if (quality_check is None and route is not None and route.mode == 'DYNAMIC'
            and route.selected_role == 'CHEAP'
            and os.getenv('RAG_ROUTER_QUALITY_CHECK', 'NONE').strip().upper() == 'REFERENCE_RANGE'):
        quality_check = lambda answer: _reference_range_quality(answer, len(citations))
    if (route is not None and route.selected_role == 'CHEAP' and quality_check is not None
            and result[1] == 'stop' and result[0].strip() and identity_status == 'MATCH'):
        try:
            quality_status = quality_check(result[0])
            if quality_status not in ('PASS', 'FAIL', 'UNKNOWN'):
                quality_status = 'UNKNOWN'
        except Exception:
            quality_status = 'UNKNOWN'
    if route is not None:
        from app.services.model_router import decide_escalation
        escalation = decide_escalation(route, completion_status=('COMPLETED' if result[1] == 'stop' and result[0].strip() else 'INCOMPLETE'),
                                       quality_status=quality_status, retries_used=0, config=router_config)
        event('router.quality', status=quality_status, reason=escalation.reason)
        if escalation.should_retry and quality_status == 'FAIL' and route.selected_role == 'CHEAP':
            event('router.escalation', status='QUALITY_ESCALATION', reason=escalation.reason,
                  model=escalation.selected_model, retry_count=1, cost={'status': 'UNKNOWN'})
            upgraded = _role_service('EXPENSIVE', escalation.selected_model)
            if prepared.original_messages is not None:
                request_messages = prepared.original_messages
                enforce_request_budget(request_messages)
                observe_model_history(request_messages)
                from app.services.cheap_grounding import provider_body_chars
                final_request_budget = {
                    **prepared.summary.get('original_budget', prepared.summary),
                    'request_chars': request_chars(request_messages),
                    'cheap_grounding_status': 'EXPENSIVE_RETRY_ORIGINAL_REQUEST',
                    'request_budget_phase': 'FINAL_PROVIDER_REQUEST',
                    'provider_model': upgraded.model,
                    'provider_argument_chars': provider_body_chars(request_messages,
                        dict(model=upgraded.model, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
                             temperature=0.7, stream=False,
                             **_provider_parameters(upgraded, upgraded.model)))}
                event('context.request_budget', **final_request_budget)
            retry = await generate_once(upgraded, upgraded.model)
            # Report both calls only when both usage records are available;
            # missing usage is unknown rather than free.
            first_usage, second_usage = result[2], retry[2]
            usage = None
            if isinstance(first_usage, dict) and isinstance(second_usage, dict):
                keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
                usage = {k: first_usage[k] + second_usage[k] for k in keys
                         if all(type(record.get(k)) is int and record[k] >= 0
                                for record in (first_usage, second_usage))} or None
            result = (retry[0], retry[1], usage)
    _router_outcome(route, upgraded.model if calls_used > 1 else target,
                    finish=result[1], content=result[0], escalations=calls_used - 1)
    if final_request_budget is not None:
        return GroundedGenerationResult(*result, final_request_budget=final_request_budget)
    return result


def grounded_stream_enabled():
    return os.getenv('RAG_GROUNDED_STREAM_ENABLED', 'false').strip().lower() == 'true' and routing_is_off()


async def stream_grounded_candidate(llm, messages, citations, *, model_id=None,
                                    spreadsheet_context=None, pdf_context=None, preference_policy='', prepared_request=None):
    """Yield provider deltas and explicit completion; caller owns terminal SSE."""
    from contextlib import aclosing
    from app.services.llm import ProviderStreamError
    prepared = prepared_request or prepare_grounded_request(llm, messages, citations,
        spreadsheet_context=spreadsheet_context, preference_policy=preference_policy,
        model_id=model_id)
    request_messages, citations = prepared.messages, prepared.citations
    enforce_request_budget(request_messages)
    if prepared.route_resolution is not None:
        llm, target, _, _ = prepared.route_resolution
    else:
        target = model_id or llm.model
    delivered = delivered_evidence_records(citations)
    event('memory.delivery', stage='grounded_answer', preference_policy_applied=bool(preference_policy))
    event('grounded.request', model=target, prompt=snapshot(request_messages), evidence_count=len(citations))
    event('context.delivery', delivery_target='provider_request', is_final_model_delivery=True,
          delivery_status='PREPARED_FOR_PROVIDER', delivered_evidence_ids=[r['evidence_id'] for r in delivered],
          output=capture(delivered, content_role='context_sent'))
    event('grounded.request.sent', input=capture(request_messages, content_role='provider_request'),
          model=target, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS, temperature=0.7)
    final = {}
    pieces = []
    stream = llm.generate_stream(messages=request_messages, model_id=target,
                                 max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
                                 stage='grounded_answer', on_final=final.update,
                                 generation_metadata={'delivered_evidence_ids': [r['evidence_id'] for r in delivered],
                                     'context_group_ids': list(dict.fromkeys(c['context_group_id'] for c in citations if c.get('context_group_id')))})
    try:
        async with aclosing(stream):
            async for chunk in stream:
                pieces.append(chunk)
                yield 'chunk', chunk
    except ProviderStreamError as exc:
        if exc.provider_status != 'TRUNCATED':
            raise
    content = ''.join(pieces)
    event('grounded.candidate', candidate=snapshot(content))
    event('grounded.response', output=capture(content, content_role='provider_response'),
          finish_reason=final.get('finish_reason'), model=target)
    yield 'complete', final


async def generate_no_results_fallback(llm, question):
    return await retry_answer_call(lambda: _generate_no_results_fallback_once(llm, question),
                                   stage='no_results_fallback')


async def _generate_no_results_fallback_once(llm, question):
    """Return (text, strategy, finish_reason, usage) for confirmed NO_RESULTS.

    The caller must authorize the provider call and must never invoke this for
    failed/unknown retrieval, omitted images, or existing usable evidence.
    Propagate provider errors to the caller's bounded retry policy.
    """
    settings = get_settings()
    strategy = settings.FALLBACK_STRATEGY.strip().lower()
    fixed = settings.FALLBACK_RESPONSE.strip() or "未在所选知识库中找到可用于回答的相关资料。"
    if strategy == 'fixed':
        return fixed, 'fixed', None, None
    if strategy != 'model':
        raise ValueError('FALLBACK_STRATEGY must be fixed or model')
    messages = [
        {'role': 'system', 'content': (settings.FALLBACK_PROMPT + '\n知识库未检索到证据。明确区分通用知识与知识库资料，不得声称知识库支持任何结论；如果用户要求仅依据知识库回答，请说明无法证实，不得编造知识库内容。')},
        {'role': 'user', 'content': question},
    ]
    target = llm.model
    provider_parameters = _provider_parameters(llm, target)
    with provider_generation('no_results_fallback', target, messages, max_tokens=settings.LLM_ANSWER_MAX_TOKENS, temperature=0.7) as (attempt, observation):
        response = await llm._get_client().chat.completions.create(
            model=target, messages=messages, max_tokens=settings.LLM_ANSWER_MAX_TOKENS,
            temperature=0.7, stream=False, **provider_parameters)
        attempt['provider_request_id'] = _provider_id(response)
        attach_provider_usage(attempt, response)
        choice = response.choices[0]
        content = choice.message.content or ''
        finish = getattr(choice, 'finish_reason', None)
        usage = getattr(response, 'usage', None)
        usage = usage.model_dump() if hasattr(usage, 'model_dump') else usage
        if observation is not None:
            _update_generation(observation, usage=_response_usage(response), finish_reason=finish,
                               execution_status=_completion_status(finish))
        if finish != 'stop' or not content.strip():
            return fixed, 'model', 'empty' if finish == 'stop' and not content.strip() else finish, usage
        return ('未在知识库中找到相关资料；以下为模型基于通用知识的回答：\n\n' + content.strip(),
                'model', finish, usage)
