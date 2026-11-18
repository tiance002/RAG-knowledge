"""Normalize provider completion evidence without using token ratios as proof."""
import json

OUTPUT_TRUNCATED = 'OUTPUT_TRUNCATED'
INCOMPLETE_STRUCTURED_OUTPUT = 'INCOMPLETE_STRUCTURED_OUTPUT'
INCOMPLETE_PATCH = 'INCOMPLETE_PATCH'
MODEL_STOPPED = 'MODEL_STOPPED'
NON_STRATEGY_OUTPUT_FAILURES = frozenset({
    OUTPUT_TRUNCATED, INCOMPLETE_STRUCTURED_OUTPUT, INCOMPLETE_PATCH, MODEL_STOPPED,
    'API_ERROR', 'TIMEOUT', 'UNKNOWN',
})


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _count(value):
    return value if type(value) is int and value >= 0 else None


def inspect(receipt, *, combine_text=False):
    """Return metadata and a parsed envelope. Raw receipt stays private."""
    transport = receipt.get('transport_status')
    response = _object(receipt.get('response'))
    usage = _object(response.get('usage'))
    chat = _list(response.get('choices'))
    first = _object(chat[0]) if chat else {}
    finish = first.get('finish_reason') if chat else response.get('finish_reason')
    status = response.get('status')
    reason = _object(response.get('incomplete_details') or response.get('finish_details')).get('reason')
    finish, status, reason = (value if isinstance(value, str) else None for value in (finish, status, reason))
    total = _count(usage.get('output_tokens', usage.get('completion_tokens')))
    details = _object(usage.get('output_tokens_details', usage.get('completion_tokens_details')))
    reasoning = _count(details.get('reasoning_tokens'))
    visible = total - reasoning if total is not None and reasoning is not None and reasoning <= total else None
    if chat:
        content = _object(first.get('message')).get('content')
        parts = [content] if isinstance(content, str) else []
    else:
        parts = [part['text'] for message in _list(response.get('output'))
                 if isinstance(message, dict) and message.get('type') == 'message'
                 and message.get('phase') in (None, 'final_answer')
                 for part in _list(message.get('content'))
                 if isinstance(part, dict) and part.get('type') == 'output_text'
                 and isinstance(part.get('text'), str)]
    if combine_text and parts:
        parts = [''.join(parts)]
    message_received = any(parts)
    value = None
    if len(parts) == 1:
        try:
            value = json.loads(parts[0])
        except (ValueError, RecursionError):
            pass
    if transport == 'TIMEOUT':
        outcome = 'TIMEOUT'
    elif transport == 'HTTP_ERROR' or status == 'failed':
        outcome = 'API_ERROR'
    elif transport != 'RECEIVED':
        outcome = 'UNKNOWN'
    elif finish == 'length' or (status == 'incomplete' and reason == 'max_output_tokens'):
        outcome = OUTPUT_TRUNCATED
    elif finish == 'content_filter' or (status == 'incomplete' and reason == 'content_filter'):
        outcome = MODEL_STOPPED
    elif status == 'incomplete':
        outcome = 'UNKNOWN'
    elif status == 'completed' or finish == 'stop':
        outcome = 'COMPLETE' if isinstance(value, dict) else MODEL_STOPPED if not message_received else INCOMPLETE_STRUCTURED_OUTPUT
    else:
        outcome = 'UNKNOWN'
    safe = {
        'output_classification': outcome,
        'provider_response_status': status if status in {'completed','incomplete','failed','cancelled','queued','in_progress'} else None,
        'provider_finish_reason': finish if finish in {'stop','length','content_filter','tool_calls','function_call'} else None,
        'provider_incomplete_reason': reason if reason in {'max_output_tokens','content_filter'} else None,
        'input_tokens': _count(usage.get('input_tokens', usage.get('prompt_tokens'))),
        'output_tokens': total, 'reasoning_tokens': reasoning,
        'visible_output_tokens': 0 if transport == 'RECEIVED' and (chat or 'output' in response) and not message_received else None,
        'visible_output_tokens_upper': visible,
        'visible_message_received': message_received, 'structured_json_complete': isinstance(value, dict),
    }
    return safe, value


def patch_output_incomplete(envelope):
    if envelope.get('status') != 'OK':
        return False
    result = _object(envelope.get('result'))
    patch = _object(result.get('worker_patch'))
    return any(not isinstance(patch.get(key), str) or not patch[key].strip() for key in ('old', 'new'))
