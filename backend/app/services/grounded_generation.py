"""Buffered generation using only the actual selected, source-linked evidence."""
import os
from app.core.config import get_settings
from app.services.observability import event, model_response, snapshot, capture, observe_model_history
from app.services.llm import provider_generation, _provider_id, _response_usage, _update_generation, _completion_status


def grounded_generation_enabled():
    return os.getenv('RAG_GROUNDED_GENERATION', 'true').lower() == 'true'


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


async def generate_grounded_candidate(llm, messages, citations, *, model_id=None, spreadsheet_context=None, pdf_context=None):
    """Return content, finish reason and usage; the caller checks completion."""
    evidence = '\n\n'.join(
        f"[{i}] 来源：{c.get('fileName', '未知')} {c.get('location', '')}\n{c['content']}"
        for i, c in enumerate(citations, 1)
    )
    prompt = ('请根据以下参考资料回答用户问题。资料不足时请如实说明，并适当引用来源。\n\n资料：\n' + evidence)
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
    target = model_id or llm.model
    observe_model_history(request_messages)
    event('grounded.request', model=target, prompt=snapshot(request_messages), evidence_count=len(citations))
    delivered_ids = [r['evidence_id'] for r in delivered_evidence_records(citations)]
    event('context.delivery', delivery_target='provider_request', is_final_model_delivery=True,
          delivery_status='PREPARED_FOR_PROVIDER', delivered_evidence_ids=delivered_ids,
          output=capture(delivered_evidence_records(citations), content_role='context_sent'))
    event('grounded.request.sent', input=capture(request_messages, content_role='provider_request'),
          model=target, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS, temperature=0.7)
    with provider_generation('grounded_answer', target, request_messages, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS, temperature=0.7) as (attempt, observation):
        if observation is not None:
            observation.update(delivered_evidence_ids=delivered_ids,
                context_group_ids=list(dict.fromkeys(c['context_group_id'] for c in citations if c.get('context_group_id'))))
        response = await llm._get_client().chat.completions.create(
            model=target, messages=request_messages, max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,
            temperature=0.7, stream=False,
        )
        attempt['provider_request_id'] = _provider_id(response)
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
