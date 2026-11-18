"""Exact serialized ordinary-RAG request packing, without rewriting source text."""
from dataclasses import dataclass
import json


def request_chars(messages, tools=None):
    rows = [m if isinstance(m, dict) else m.model_dump() for m in messages]
    return len(json.dumps({'messages': rows, 'tools': tools or []}, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class PreparedGroundedRequest:
    messages: list
    citations: list
    summary: dict
    routing_messages: list
    route_resolution: object = None
    original_messages: list = None


def pack_grounded_request(messages, citations, build, *, limit, protected_indices=()):
    """Prefer full evidence and recent complete dialogue turns; never crop bodies.

    ``build`` includes ALL fixed prompts, supplementary proofs and preferences.
    The current question and all system instructions are mandatory. First remove
    oldest prior turns, retaining the latest referent turn where possible. Only
    then remove whole low-priority citations. First-ranked and verified native
    proof citations are protected. An irreducible request returns over-limit so
    the existing final guard can reject it before any provider call.
    """
    rows = [dict(m) for m in messages]
    sources = [dict(c) for c in citations]
    users = [i for i, m in enumerate(rows) if m.get('role') == 'user']
    current = users[-1] if users else len(rows)
    # Preserve every system/developer instruction, including late ones.
    fixed = {i for i, m in enumerate(rows) if m.get('role') in ('system', 'developer') or i >= current}
    starts = users[:-1]
    turns = [[j for j in range(i, users[n+1]) if j not in fixed] for n, i in enumerate(starts)]
    # Unattached leading assistant/tool messages are removable together.
    leading = [i for i in range(users[0] if users else len(rows)) if i not in fixed]
    if leading:
        turns.insert(0, leading)
    kept = set(range(len(rows))); selected = list(range(len(sources)))
    omitted_turns, omitted_sources = [], []

    def history():
        return [m for i, m in enumerate(rows) if i in kept]

    def render():
        return build(history(), [sources[i] for i in selected])

    original = request_chars(render())
    size = original
    while size > limit and len(turns) > 1:
        removed = turns.pop(0); kept.difference_update(removed); omitted_turns.append(removed)
        size = request_chars(render())
    protected = set(protected_indices)
    if sources:
        protected.add(0)
    protected.update(i for i, c in enumerate(sources) if c.get('source_type') in
                     ('source_native_table_expansion', 'native_source_expansion'))
    # Whole neighbors/history supplements are expendable before ranked hits.
    def discard_priority(i):
        c = sources[i]
        tier = (0 if c.get('evidence_relation') == 'neighbor' else
                1 if c.get('match_type') == 'history' else 2)
        return tier, -i

    candidates = sorted((i for i in selected if i not in protected), key=discard_priority)
    for i in [i for i in candidates if discard_priority(i)[0] < 2]:
        if size <= limit:
            break
        selected.remove(i); omitted_sources.append(i); size = request_chars(render())
    # If a huge latest historical turn prevents even protected evidence fitting,
    # remove that whole turn; the question and instructions still cannot change.
    while size > limit and turns:
        removed = turns.pop(0); kept.difference_update(removed); omitted_turns.append(removed)
        size = request_chars(render())
    # When history alone cannot solve overflow, use the current retriever order.
    # No Gold-driven selection, partial sentences, or replacement source bodies.
    for i in [i for i in candidates if discard_priority(i)[0] == 2]:
        if size <= limit:
            break
        selected.remove(i); omitted_sources.append(i); size = request_chars(render())
    final_messages = render()
    minimal_history = [m for i, m in enumerate(rows) if i in fixed]
    fixed_cost = request_chars(build(minimal_history, []))
    history_cost = request_chars(build(history(), [])) - fixed_cost
    summary = dict(rule_version='grounded-total-budget-v1', limit_chars=limit,
        original_request_chars=original, request_chars=request_chars(final_messages),
        fixed_request_chars=fixed_cost, history_overhead_chars=history_cost,
        evidence_with_labels_chars=request_chars(final_messages)-fixed_cost-history_cost,
        # Count actual text only; missing/non-text bodies must reach the
        # existing evidence gate without becoming invented string evidence.
        evidence_body_chars=sum(len(sources[i]['content']) for i in selected
                                if isinstance(sources[i].get('content'), str)),
        original_citation_count=len(sources), delivered_citation_count=len(selected),
        omitted_history_turns=len(omitted_turns),
        omitted_history_message_indices=[i for group in omitted_turns for i in group],
        omitted_citations=[dict(original_index=i, chunk_id=sources[i].get('chunk_id'),
            file_id=sources[i].get('fileId') or sources[i].get('file_id'),
            reason='FINAL_REQUEST_BUDGET') for i in omitted_sources],
        source_bodies_cropped=False)
    assert summary['request_chars'] == fixed_cost + history_cost + summary['evidence_with_labels_chars']
    return history(), [sources[i] for i in selected], final_messages, summary
