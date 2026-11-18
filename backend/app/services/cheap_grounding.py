"""Opt-in cheap grounded-answer contract; pure packing with no model actions.

Contract text is frozen v8.2. Selection is the caller's responsibility; this
module never routes, imports experiment scripts, or changes runtime settings.
"""
from copy import deepcopy
import hashlib
import json
import os
from app.services.grounded_request_budget import pack_grounded_request, request_chars

VERSION = 'cheap-contract-budget-v8.1'
_MARKER = '_cheap_budget_original_index'


def enabled():
    return os.getenv('RAG_CHEAP_GROUNDING_ENABLED', 'false').strip().lower() == 'true'


CONTRACT_VERSION = 'cheap-grounding-contract-v8.2'
CONTRACT_TEXT = (
    '回答契约：以当前问题及历史中明确的用户要求为范围，只回答所问内容。'
    '逐项覆盖所要求的字段、条件、单位和比较对象；简洁不等于省略必要答案。'
    '事实依据沿用本次提供的来源正文；历史仅用于理解指代和要求，不作为新的来源证据。'
    '某一子项缺少依据时，仅说明该子项无法从所给资料确认，并回答其余有依据的部分；'
    '不要把局部缺失扩大为全部无资料，也不要据此断言事实不存在。'
    '引用只使用本次资料已有的原样来源标签（如 [1] 或 [E1]），且紧随该来源确实支持的结论；'
    '不要沿用历史编号或编造、错配标签。'
    '除非用户要求，不增加建议、延伸背景或其他细节；保留回答所需的解释与限定。'
)
CONTRACT_SHA256 = hashlib.sha256(CONTRACT_TEXT.encode('utf-8')).hexdigest()


def transform_messages(messages):
    """Copy input; extend the first system string or prepend one system message."""
    if not isinstance(messages, list) or not messages:
        raise ValueError('messages must be a nonempty list')
    if any(not isinstance(m, dict) or not isinstance(m.get('role'), str)
           or 'content' not in m for m in messages):
        raise ValueError('expected prepared message dictionaries with role/content')
    if any(isinstance(m.get('content'), str) and CONTRACT_TEXT in m['content']
           for m in messages):
        raise ValueError('contract already present')
    result = deepcopy(messages)
    if result[0]['role'] == 'system' and isinstance(result[0]['content'], str):
        result[0]['content'] += '\n\n' + CONTRACT_TEXT
    else:
        result.insert(0, {'role': 'system', 'content': CONTRACT_TEXT})
    return result


def text_sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def provider_body_chars(messages, provider_fields):
    """Exact default-json serialization, matching the experimental harness."""
    if 'messages' in provider_fields:
        raise ValueError('provider_fields must exclude messages')
    return len(json.dumps(dict(provider_fields, messages=messages), ensure_ascii=False, default=str))


def prepare_cheap_request(messages, citations, *, provider_fields, limit=32000):
    """Return (messages, active citations, audit), preserving whole source bodies.

    provider_fields must be the exact dispatched body excluding messages. No
    question/Gold/ID selection occurs. An irreducible overflow raises ValueError.
    Source-block packing requires a first system string. History-only requests
    retain baseline messages and receive no grounded contract.
    """
    has_system = bool(messages and messages[0].get('role') == 'system'
                      and isinstance(messages[0].get('content'), str))
    if not has_system and citations:
        raise ValueError('source-block packing requires initial system string')
    if not citations:
        return deepcopy(messages), [], dict(version=VERSION,
            eligibility='NOT_APPLICABLE_HISTORY_ONLY', contract_applied=False,
            request_chars=request_chars(messages),
            provider_body_chars=provider_body_chars(messages, provider_fields),
            inactive_citations=[], omitted_citations=[], omitted_history=[],
            source_bodies_cropped=False)
    original = deepcopy(messages)
    text = original[0]['content'] if has_system else ''
    # Also validates absence of an already applied contract.
    overlay = transform_messages(original)
    spans = []
    active = []
    inactive = []
    for i, cite in enumerate(citations):
        if _MARKER in cite:
            raise ValueError('reserved citation marker collision')
        body = cite.get('content')
        alias = cite.get('source_alias') or i + 1
        header = f"[{alias}] 来源：{cite.get('fileName', '未知')} {cite.get('location', '')}\n"
        block = header + body if isinstance(body, str) else None
        start = text.find(block) if block else -1
        if start < 0:
            inactive.append(dict(original_index=i, chunk_id=cite.get('chunk_id'),
                                 reason='ORIGINAL_BLOCK_ABSENT'))
            continue
        if text.find(block, start + 1) >= 0:
            raise ValueError('ambiguous repeated original source block')
        end = start + len(block)
        if any(start < b and end > a for a, b, _ in spans):
            raise ValueError('overlapping original source blocks')
        spans.append((start, end, i))
        active.append(dict(deepcopy(cite), **{_MARKER: i}))

    if not active:
        raise ValueError('no active source blocks; grounded eligibility unproven')

    def build(history, selected):
        keep = {c[_MARKER] for c in selected}
        rebuilt = text
        for start, end, i in sorted(spans, reverse=True):
            if i not in keep:
                rebuilt = rebuilt[:start] + rebuilt[end:]
        first = deepcopy(original[0])
        first['content'] = rebuilt
        return transform_messages([first] + deepcopy(history))

    # With messages substituted identically, this overhead is independent of
    # selected evidence/history. Include both body and production message limit.
    overhead = (provider_body_chars(overlay, provider_fields)
                - request_chars(overlay))
    effective_limit = min(limit, limit - overhead)
    history, selected, packed, summary = pack_grounded_request(
        original[1:] if has_system else original, active, build, limit=effective_limit)
    final_chars = request_chars(packed)
    body_chars = provider_body_chars(packed, provider_fields)
    if final_chars > limit or body_chars > limit:
        raise ValueError('irreducible protected request exceeds input budget')
    retained = {c[_MARKER] for c in selected}
    omitted = [dict(original_index=i, chunk_id=citations[i].get('chunk_id'),
                    block_sha256=text_sha(text[a:b]),
                    body_sha256=text_sha(citations[i]['content']))
               for a, b, i in spans if i not in retained]
    offset = 1 if has_system else 0
    omitted_history = [dict(original_index=i + offset,
        message_sha256=text_sha(json.dumps(original[i + offset], ensure_ascii=False,
                                           sort_keys=True)))
        for i in summary['omitted_history_message_indices']]
    assert original == messages
    assert final_chars == summary['request_chars']
    assert not omitted and not omitted_history or packed != overlay
    if not omitted and not omitted_history:
        assert packed == overlay
    cleaned = [{k: v for k, v in c.items() if k != _MARKER} for c in selected]
    audit = dict(version=VERSION, contract_version=CONTRACT_VERSION,
        eligibility='ELIGIBLE_GROUNDED', contract_applied=True,
        contract_sha256=CONTRACT_SHA256, request_chars=final_chars,
        provider_body_chars=body_chars, provider_overhead_chars=overhead,
        effective_limit=effective_limit, limit=limit, inactive_citations=inactive,
        active_citations=len(active), retained_citations=len(selected),
        omitted_citations=omitted, omitted_history=omitted_history,
        source_bodies_cropped=False, pack_summary=summary,
        input_messages_sha256=text_sha(json.dumps(original, ensure_ascii=False, sort_keys=True)),
        output_messages_sha256=text_sha(json.dumps(packed, ensure_ascii=False, sort_keys=True)))
    return packed, cleaned, audit
