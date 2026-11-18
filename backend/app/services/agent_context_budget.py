"""Request-local Agent delivery: whole registered bodies, one copy per request.

This is an invocation view, never an edit to tool history or source registry.
The serialized application character limit includes dialogue and tool schemas;
it is not a tokenizer estimate. Unverifiable tool payloads stay fail-closed.
"""
import json

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

EVIDENCE_TOOLS = frozenset({'search_knowledge_base', 'read_knowledge_context', 'read_tool_result'})


def history_as_sent(messages, *, original_query, retain_retrieval_history=False):
    """WeKnora's HistoryAsSent policy, applied only to prior turns."""
    if retain_retrieval_history:
        return list(messages)
    active = next((i for i in range(len(messages) - 1, -1, -1)
                   if isinstance(messages[i], HumanMessage)
                   and messages[i].content == original_query), 0)
    marker = '[Previous retrieval result omitted — knowledge base may have changed. Please perform a fresh search.]'
    return [message.model_copy(update={'content': marker})
            if index < active and isinstance(message, ToolMessage) and message.name in EVIDENCE_TOOLS
            else message for index, message in enumerate(messages)]


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def request_chars(messages, tools=None):
    return len(_encoded({'messages': [m.model_dump() for m in messages], 'tools': tools or []}))


def _parent_key(source):
    if source.get('evidence_relation') != 'parent' and source.get('chunk_type') != 'parent':
        return None
    group = source.get('context_group') or {}
    return (source.get('fileId') or source.get('file_id'),
            source.get('selected_parent_id') or source.get('parent_id')
            or source.get('parent_chunk_id') or group.get('selected_parent_id')
            or source.get('chunk_id'))


def consolidate_agent_context(messages, *, source_registry, canonical_context,
                              tools=None, limit_chars=32000, max_parents=6,
                              original_query=None, measure_messages=None):
    """Select whole sources across all rounds, retaining every protocol pair.

    Most recent tool rounds get first choice; within a round retrieval ranking
    stays intact. Source IDs remain stable, so dropped sources leave gaps.
    Historical dialogue is removed only in complete old turns when necessary.
    """
    measure = measure_messages or (lambda rows: request_chars(rows, tools))
    registry = {c.get('source_id'): c for c in source_registry or []
                if isinstance(c, dict) and type(c.get('source_id')) is int}
    output, payloads, candidates, deferred, indices = list(messages), {}, [], [], {}
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage) or message.name not in EVIDENCE_TOOLS:
            continue
        try:
            data = json.loads(message.content)
            if data.get('tool_result_status') == 'SAVED_INCOMPLETE':
                indices[index] = data
                for item in data.get('sources') or []:
                    alias = item.get('source_id') if isinstance(item, dict) else None
                    if isinstance(alias, str) and alias.startswith('s') and alias[1:].isdigit():
                        sid = int(alias[1:]) if len(alias) < 22 else None
                        if sid in registry and sid not in deferred:
                            deferred.append(sid)
                continue
            ids = data.get('source_ids')
            citations = data.get('citations')
            if not isinstance(ids, list) or not isinstance(citations, list):
                continue
            selected = [registry[sid] for sid in ids]
            text_citations = [c for c in citations if c.get('content_type') != 'image']
            if (len(set(ids)) != len(ids) or [c.get('source_id') for c in text_citations] != ids
                    or data.get('context') != canonical_context(selected)):
                continue
            payloads[index] = (data, {c['source_id']: c for c in text_citations})
        except (ValueError, TypeError, KeyError, AttributeError):
            continue

    # The empty shells reserve the exact history/schema/protocol overhead.
    assignments = {index: [] for index in payloads}
    omitted = []

    def render():
        for index, (data, by_id) in payloads.items():
            ids = assignments[index]
            all_ids = data['source_ids']
            updated = {**data, 'context': canonical_context([registry[sid] for sid in ids]),
                       'citations': [by_id[sid] for sid in ids]
                           + [c for c in data['citations'] if c.get('content_type') == 'image'],
                       'source_ids': ids}
            if 'delivered_source_ids' in updated:
                updated['delivered_source_ids'] = ids
            if ids != all_ids:
                updated['context_delivery_status'] = 'CONSOLIDATED'
                updated['available_source_ids'] = all_ids
                updated['delivery_note'] = 'Only source_ids have evidence bodies in this message; other sources may be delivered in another tool message or omitted by the request budget.'
            output[index] = messages[index].model_copy(update={'content': _encoded(updated)})

    render()
    trimmed = 0
    # Only old conversation, before this request's actual user question, is
    # eligible. Retry instructions and all tool call/result pairs remain intact.
    active = next((i for i in range(len(messages)-1, -1, -1)
                   if isinstance(messages[i], HumanMessage)
                   and messages[i].content == original_query), None)
    pending_ids = {sid for data, _ in payloads.values() for sid in data['source_ids']}
    evidence_reserve = min(sum(len(registry[sid].get('content') or '') for sid in pending_ids), limit_chars // 2)
    compacted_indices = 0
    # Index previews are recoverable navigation, never evidence. Under pressure
    # compact the oldest cards before discarding conversation or whole sources.
    for index, data in indices.items():
        if measure([m for m in output if m is not None]) <= limit_chars - evidence_reserve:
            break
        cards = data.get('sources') or []
        count = data.get('total_sources', len(cards))
        compact = {**data, 'sources': [
            {key: card[key] for key in ('source_id', 'title', 'body_chars') if key in card}
            for card in cards[:2] if isinstance(card, dict)],
            'index_compaction_status': 'PREVIEWS_OMITTED_RECOVERABLE',
            'total_sources': count, 'next_source_offset': 2 if count > 2 else None,
            'delivered_source_ids': []}
        output[index] = messages[index].model_copy(update={'content': _encoded(compact)})
        compacted_indices += 1
    if active is not None:
        starts = [i for i in range(active) if isinstance(messages[i], HumanMessage)]
        for offset, start in enumerate(starts):
            if measure([m for m in output if m is not None]) <= limit_chars - evidence_reserve:
                break
            end = starts[offset+1] if offset+1 < len(starts) else active
            turn = messages[start:end]
            calls = {call.get('id') for m in turn for call in (getattr(m, 'tool_calls', None) or [])}
            results = {m.tool_call_id for m in turn if isinstance(m, ToolMessage)}
            if calls != results:
                continue
            notes = '\n'.join(f'{m.type}: {m.content[:160]}' for m in turn
                if m.type in {'human', 'ai'} and isinstance(m.content, str) and m.content)
            for index in range(start, end):
                output[index] = None
                trimmed += 1
            output[start] = SystemMessage(content='[Extractive conversation notes; incomplete; not original evidence]\n' + notes[:480])

    seen, parents = set(), set()
    for index in reversed(payloads):
        for sid in payloads[index][0]['source_ids']:
            if sid in seen:
                continue
            seen.add(sid)
            candidates.append((index, sid))
    for index, sid in candidates:
        parent = _parent_key(registry[sid])
        if parent is not None and parent not in parents and len(parents) >= max_parents:
            omitted.append(sid)
            continue
        assignments[index].append(sid)
        render()
        if measure([m for m in output if m is not None]) > limit_chars:
            assignments[index].pop()
            omitted.append(sid)
            render()
        else:
            if parent is not None:
                parents.add(parent)
    delivered = [sid for ids in assignments.values() for sid in ids]
    omitted.extend(sid for sid in deferred if sid not in delivered and sid not in omitted)
    omitted.extend(sid for sid in registry if sid not in delivered and sid not in omitted)
    result = [m for m in output if m is not None]
    return result, {'source_ids': delivered, 'omitted_source_ids': omitted,
                    'delivery_status': 'PARTIAL' if omitted else 'DELIVERED',
                    'unique_parent_count': len(parents), 'max_parents': max_parents,
                    'request_chars': measure(result), 'limit_chars': limit_chars,
                    'trimmed_history_messages': trimmed,
                    'deferred_source_ids': deferred,
                    'compacted_result_indices': compacted_indices,
                    'status': 'WITHIN_BUDGET' if measure(result) <= limit_chars else 'OVER_BUDGET'}
