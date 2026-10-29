"""Buffered generation using only the actual selected, source-linked evidence."""
import os
from app.core.config import get_settings
from app.services.observability import event, model_response, snapshot
from app.services.llm import provider_attempt, _provider_id


def grounded_generation_enabled():
    return os.getenv('RAG_GROUNDED_GENERATION', 'true').lower() == 'true'


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
    event('grounded.request', model=target, prompt=snapshot(request_messages), evidence_count=len(citations))
    with provider_attempt('grounded_answer', target) as attempt:
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
        # Candidate text is never sent to the telemetry backend. Explicitly
        # enabled snapshots use the existing private local snapshot store.
        event('grounded.candidate', candidate=snapshot(content))
        return content, getattr(choice, 'finish_reason', None), usage
