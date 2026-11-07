"""Buffered generation using only the actual selected, source-linked evidence."""
import asyncio
import os
import re
from urllib.parse import urlsplit
from app.core.config import get_settings
from app.services.observability import event, model_response, snapshot, capture, observe_model_history
from app.services.llm import provider_generation, _provider_id, _response_usage, _update_generation, _completion_status, is_unknown_provider_error


def grounded_generation_enabled():
    return os.getenv('RAG_GROUNDED_GENERATION', 'true').lower() == 'true'


class RoutingBlocked(RuntimeError):
    """Routing is configured but the server has not authorized a safe call."""


def routing_is_off():
    """A trusted server marker disables routing for the next call, no restart."""
    marker = os.getenv('RAG_MODEL_ROUTER_OFF_FILE', '').strip()
    return (os.getenv('RAG_MODEL_ROUTER_MODE', 'OFF').strip().upper() == 'OFF'
            or bool(marker and os.path.exists(marker)))


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
    extra_body = getattr(service, '_router_extra_body', None)
    return model.bind(extra_body=extra_body) if extra_body is not None else model


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
    import json
    rows = [m if isinstance(m, dict) else m.model_dump() for m in messages]
    request_chars = len(json.dumps({'messages': rows, 'tools': tools or []}, ensure_ascii=False, default=str))
    request_limit = get_settings().GROUNDED_REQUEST_MAX_CHARS
    if request_chars > request_limit:
        event('context.budget_exceeded', request_chars=request_chars, limit_chars=request_limit,
              degradation_reason='FINAL_REQUEST_OVER_CHAR_BUDGET')
        raise ValueError('FINAL_REQUEST_OVER_CHAR_BUDGET')
    return request_chars


async def generate_grounded_candidate(llm, messages, citations, *, model_id=None, spreadsheet_context=None, pdf_context=None, quality_check=None, retrieval_status="SUCCESS", preference_policy=''):
    """Return content, finish reason and usage; the caller checks completion."""
    evidence = '\n\n'.join(
        f"[{i}] 来源：{c.get('fileName', '未知')} {c.get('location', '')}\n{c['content']}"
        for i, c in enumerate(citations, 1)
    )
    prompt = build_grounded_prompt(evidence, addendum=getattr(llm, '_grounded_prompt_addendum', ''))
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
    llm, target, route, router_config = _route_generation(
        llm, model_id, request_messages, citations, retrieval_status=retrieval_status, return_decision=True)
    if preference_policy:
        request_messages[0] = {'role': 'system', 'content': prompt + '\n\n' + preference_policy}
    # Enforce the final serialized request, including source labels, history,
    # supplementary proofs and preference prompt. This is a character budget,
    # not a claim about any provider's tokenizer or output-token allowance.
    enforce_request_budget(request_messages)
    observe_model_history(request_messages)
    identity_status = 'UNKNOWN'
    async def _generate_once(current_llm, target):
        nonlocal identity_status
        event('memory.delivery', stage='grounded_answer', preference_policy_applied=bool(preference_policy))
        extra_body = getattr(current_llm, '_router_extra_body', None)
        provider_parameters = {'extra_body': extra_body} if extra_body is not None else {}
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
            response = await current_llm._get_client().chat.completions.create(
                model=target, messages=request_messages, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
                temperature=0.7, stream=False, **provider_parameters,
            )
            attempt['provider_request_id'] = _provider_id(response)
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
    async def generate_once(current_llm, target):
        nonlocal calls_used
        escalations = calls_used
        calls_used += 1
        try:
            return await _generate_once(current_llm, target)
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
    return result


async def generate_no_results_fallback(llm, question):
    """Return (text, strategy, finish_reason, usage) for confirmed NO_RESULTS.

    The caller must authorize the provider call and must never invoke this for
    failed/unknown retrieval, omitted images, or existing usable evidence.
    Provider errors propagate unchanged; no retry is performed.
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
    with provider_generation('no_results_fallback', target, messages, max_tokens=settings.LLM_ANSWER_MAX_TOKENS, temperature=0.7) as (attempt, observation):
        response = await llm._get_client().chat.completions.create(
            model=target, messages=messages, max_tokens=settings.LLM_ANSWER_MAX_TOKENS,
            temperature=0.7, stream=False)
        attempt['provider_request_id'] = _provider_id(response)
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
