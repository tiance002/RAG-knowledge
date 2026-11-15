"""Copy-only Agent model evidence view; backend records remain authoritative.

Only verified registered tool context is rendered once for the model. Body text
is never selected, shortened or rewritten. Unknown semantic metadata survives;
only explicit backend bookkeeping fields are removed. No Gold data is read.
"""
import json
import re
from collections import Counter

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

VERSION = "AGENT_EVIDENCE_VIEW_V2"
EVIDENCE_TOOLS = frozenset({'search_knowledge_base', 'read_knowledge_context', 'read_tool_result'})


def persist_agent_tool_results(messages, *, store, canonical_context, source_registry, inline_chars=6000):
    """Replace only verified recoverable results in the invocation copy.

    The raw ToolMessages and the authoritative registry are never changed.
    Saving failure leaves the original result for whole-source budgeting.
    """
    output, saved, failed = [], 0, 0
    for message in messages:
        if (store is None or not isinstance(message, ToolMessage)
                or message.name not in EVIDENCE_TOOLS or message.name == 'read_tool_result'
                or not isinstance(message.content, str)
                or (len(message.content) <= inline_chars
                    and not message.additional_kwargs.get('historical_retrieval'))):
            output.append(message)
            continue
        try:
            view, status, _, _ = _project_tool(message, canonical_context, source_registry)
            if status != 'PROJECTED':
                output.append(message)
                continue
            if (not message.additional_kwargs.get('historical_retrieval')
                    and len(view.content) <= inline_chars):
                output.append(message)
                continue
            active_store = store() if callable(store) else store
            if active_store is None:
                output.append(message)
                failed += 1
                continue
            index = active_store.save(message.content)
            output.append(message.model_copy(update={'content': _encoded(index)}))
            saved += 1
        except (OSError, ValueError, TypeError, KeyError):
            output.append(message)
            failed += 1
    return output, {'saved_result_messages': saved, 'save_failed_messages': failed,
                    'inline_chars': inline_chars}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _size(text):
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def _add(target, key, value):
    measured = _size(value)
    bucket = target.setdefault(key, {"chars": 0, "utf8_bytes": 0})
    for unit in measured:
        bucket[unit] += measured[unit]


def input_size_breakdown(messages, tools=None):
    """Disjoint application-JSON ledger plus a separately nested tool ledger."""
    rows = [message.model_dump() for message in messages]
    total = _size(_encoded({"messages": rows, "tools": tools or []}))
    components = {}
    tool_details = {}
    measured_tools = unparsed_tools = 0
    for message, row in zip(messages, rows):
        kind = ("system_content" if isinstance(message, SystemMessage) else
                "retrieval_tool_content" if isinstance(message, ToolMessage) and message.name in EVIDENCE_TOOLS else
                "assistant_content" if isinstance(message, AIMessage) else
                "human_content" if message.type == "human" else "other_content")
        _add(components, kind, _encoded(row["content"]))
        if isinstance(message, AIMessage) and "tool_calls" in row:
            _add(components, "assistant_tool_calls", _encoded(row["tool_calls"]))
        if kind != "retrieval_tool_content":
            continue
        if not isinstance(message.content, str):
            unparsed_tools += 1
            continue
        try:
            data = json.loads(message.content)
            if not isinstance(data, dict):
                unparsed_tools += 1
                continue
            measured_tools += 1
            normalized = _size(_encoded(data))
            fields = {}
            if "context" in data:
                _add(fields, "context_value", _encoded(data["context"]))
            citations = data.get("citations")
            if isinstance(citations, list):
                bodies = [_encoded(citation["content"]) for citation in citations
                          if isinstance(citation, dict) and "content" in citation]
                for body in bodies:
                    _add(fields, "citation_body_values", body)
                citation_size = _size(_encoded(citations))
                body_size = fields.get("citation_body_values", {"chars": 0, "utf8_bytes": 0})
                fields["citation_metadata_and_syntax"] = {unit: citation_size[unit] - body_size[unit] for unit in citation_size}
            fields["other_fields_and_syntax"] = {
                unit: normalized[unit] - sum(bucket[unit] for bucket in fields.values()) for unit in normalized}
            for key, bucket in fields.items():
                target = tool_details.setdefault(key, {"chars": 0, "utf8_bytes": 0})
                for unit in bucket:
                    target[unit] += bucket[unit]
        except (TypeError, ValueError):
            # A malformed result still contributes to the complete application ledger.
            unparsed_tools += 1
            continue
    _add(components, "tool_schemas", _encoded(tools or []))
    components["message_protocol_and_envelope"] = {
        unit: total[unit] - sum(bucket[unit] for bucket in components.values()) for unit in total}
    return {"method": "APPLICATION_JSON_CHAR_UTF8_V1", "total": total, "components": components,
            "normalized_tool_payload_components": tool_details,
            "normalized_tool_payload_count": measured_tools, "unparsed_tool_payload_count": unparsed_tools,
            "nested_ledger_note": "Normalized tool payload components are nested within retrieval_tool_content; do not add them to the application components.",
            "input_tokens": None, "token_count_status": "UNKNOWN", "token_count_method": "model_tokenizer_not_configured"}


# These keys describe backend identity, retrieval bookkeeping or curation, not
# facts in evidence. Unknown keys are retained, including nested semantic facts.
_BACKEND_KEYS = frozenset("""
fileId file_id kb_id kb_ids kb_name chunk_id id parent_id parent_chunk_id
parent_local_start parent_local_end parent_child_version chunk_type
context_group_id context_group_ids context_groups seed_evidence_ids seed_child_ids
members member_ids evidence_relation source_mapping_status source_offset_status
score rerank_score rerank_status retrieval_score vector_score bm25_score
composite_score composite_score_status retrieval_base_score retrieval_normalization source_weight parent_selection selected_parent_id
build_id index_build_id active_build_id original_build_id original_file_id
original_chunk_id original_generation generation source_file_id source_kb_id
target_kb_id qa500_import qa500_source_identity provenance_schema
source_image_path source_path file_path manifest_path effect_key
indexed_page_number source_registry evidence_registry requested_chunk_id history_turn_id origin_reference_id source_content_hash
source_sha256 original_source_sha256 image_sha256 indexed_text_sha256
original_chunk_sha256 source_manifest_sha256 derived_document_sha256 prompt_sha256
content_hash
""".split())


_AMBIGUOUS_FACT_KEYS = frozenset({'id', 'score', 'generation', 'members', 'member_ids'})


_PROVENANCE_CONTAINERS = frozenset({'source_metadata', 'source_locators',
    'source_locator', 'source_image_references', 'image_descriptions'})


def _semantic_metadata(value, *, backend_container=True, metadata_container=False):
    """Strip known bookkeeping without interpreting unknown semantic objects.

    A nested unknown table can legitimately contain columns called id or score.
    Those facts must survive even though these keys mean backend bookkeeping in
    the known citation/provenance containers.
    """
    if isinstance(value, list):
        return [_semantic_metadata(item, backend_container=backend_container, metadata_container=metadata_container) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _semantic_metadata(item, backend_container=key in _PROVENANCE_CONTAINERS,
                                   metadata_container=key in _PROVENANCE_CONTAINERS)
            for key, item in value.items()
            if not backend_container or key not in _BACKEND_KEYS
            or (metadata_container and key in _AMBIGUOUS_FACT_KEYS)}


def _readable_title(citation):
    title = citation.get('fileName')
    if not isinstance(title, str) or not title:
        return None
    # Source paths are display titles only; never read a filesystem path here.
    return re.split(r"[\\/]", title)[-1] or title


def _project_tool(message, canonical_context, source_registry):
    if not isinstance(message.content, str):
        return message, "NON_TEXT_TOOL_CONTENT", 0, 0
    try:
        data = json.loads(message.content)
        if not isinstance(data, dict) or not isinstance(data.get("citations"), list):
            return message, "INVALID_TOOL_PAYLOAD", 0, 0
        if not isinstance(source_registry, list):
            return message, "SOURCE_REGISTRY_UNAVAILABLE", 0, 0
        selected = []
        for citation in data["citations"]:
            if not isinstance(citation, dict):
                return message, "INVALID_CITATION", 0, 0
            if citation.get("content_type") == "image":
                if citation.get("content") not in ("", None):
                    return message, "NON_CANONICAL_IMAGE", 0, 0
                continue
            source_id, content = citation.get("source_id"), citation.get("content")
            if (not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0
                    or not citation.get("chunk_id") or not isinstance(content, str) or not content):
                return message, "UNREGISTERED_CITATION", 0, 0
            # Exact mapping includes semantic metadata and provenance; matching an
            # integer alone cannot establish a valid registered evidence body.
            if not any(citation == entry for entry in source_registry):
                return message, "UNREGISTERED_CITATION", 0, 0
            selected.append(citation)
        if not selected:
            return message, "NO_TEXT_EVIDENCE", 0, 0
        if data.get("source_ids") != [citation["source_id"] for citation in selected]:
            return message, "SOURCE_IDS_MISMATCH", 0, 0
        if data.get("context") != canonical_context(selected):
            return message, "CONTEXT_MISMATCH", 0, 0
        projected_citations = []
        for citation in data['citations']:
            view = _semantic_metadata({key: value for key, value in citation.items()
                                       if key not in {'content', 'fileName'}})
            title = _readable_title(citation)
            if title:
                view['title'] = title
            projected_citations.append(view)
        # Preserve the complete canonical evidence context byte for byte. The
        # metadata title is readable without rewriting canonical source headers.
        context = data['context']
        result = {**_semantic_metadata({key: value for key, value in data.items()
                                      if key not in {'context', 'citations'}}),
                  'context': context, 'citations': projected_citations}
        chars = sum(len(citation["content"]) for citation in selected)
        byte_count = sum(len(citation["content"].encode("utf-8")) for citation in selected)
        return message.model_copy(update={"content": json.dumps(result, ensure_ascii=False)}), "PROJECTED", chars, byte_count
    except (TypeError, ValueError, KeyError, AttributeError):
        return message, "INVALID_TOOL_PAYLOAD", 0, 0


def project_agent_evidence_messages(messages, *, canonical_context, source_registry=None, tools=None):
    """Return an invocation copy; each tool result retains its own complete context."""
    projected = []
    statuses = Counter()
    removed_chars = removed_bytes = 0
    for message in messages:
        if isinstance(message, ToolMessage) and message.name in EVIDENCE_TOOLS:
            result, status, chars, byte_count = _project_tool(message, canonical_context, source_registry)
            statuses[status] += 1
            removed_chars += chars
            removed_bytes += byte_count
            projected.append(result)
        else:
            projected.append(message)
    return projected, {"projection_version": VERSION, "message_status_counts": dict(statuses),
        "removed_duplicate_body_chars": removed_chars, "removed_duplicate_body_utf8_bytes": removed_bytes,
        "before": input_size_breakdown(messages, tools), "after": input_size_breakdown(projected, tools),
        "input_tokens": None, "token_count_status": "UNKNOWN"}


def encode_agent_sources(messages, source_registry):
    """Encode a request-only source view using canonical source_id-derived aliases.

    The registry list remains authoritative. Bodies are copied byte for byte;
    only system-owned headers and structured reference fields change.
    """
    from app.services.agent_source_registry import RequestSourceRegistry
    registry = RequestSourceRegistry(source_registry or [])
    result, encoded, unavailable = [], 0, 0
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in EVIDENCE_TOOLS:
            result.append(message)
            continue
        try:
            data = json.loads(message.content)
            ids = data.get('source_ids')
            if not isinstance(ids, list):
                raise ValueError('no source list')
            sources = [registry.resolve(sid) for sid in ids]
            if any(source is None for source in sources):
                raise ValueError('unknown source')
            expected = '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}" for c in sources)
            if data.get('context') != expected:
                raise ValueError('noncanonical context')
            view = dict(data)
            view['context'] = '\n\n'.join(f"Document {registry.alias_for(c['source_id'])} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}" for c in sources)
            view['source_ids'] = [registry.alias_for(sid) for sid in ids]
            view['citations'] = [{**c, 'source_id': registry.alias_for(c['source_id'])}
                if isinstance(c, dict) and registry.resolve(c.get('source_id')) is not None else c
                for c in data.get('citations', [])]
            for key in ('available_source_ids', 'delivered_source_ids'):
                if key in view:
                    view[key] = [registry.alias_for(sid) for sid in view[key] if registry.resolve(sid) is not None]
            result.append(message.model_copy(update={'content': json.dumps(view, ensure_ascii=False)}))
            encoded += 1
        except (TypeError, ValueError, KeyError, AttributeError):
            result.append(message)
            unavailable += 1
    return result, {'encoded_tool_messages': encoded, 'unavailable_tool_messages': unavailable,
                    'alias_scheme': 'CANONICAL_SOURCE_ID_S_V1'}
