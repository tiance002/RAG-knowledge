"""Copy-only Agent input projection; stored evidence/state remain authoritative.

The candidate removes citations.content only when the same exact bodies are
already present in that ToolMessage's canonical registered context. It never
deduplicates across messages or source identities. Size ledgers describe
application serialization, not provider framing or tokenizer measurements.
"""
import json
from collections import Counter

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

VERSION = "CITATION_BODY_DEDUP_V1"


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
                "retrieval_tool_content" if isinstance(message, ToolMessage) and message.name == "search_knowledge_base" else
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


def _project_tool(message, canonical_context):
    if not isinstance(message.content, str):
        return message, "NON_TEXT_TOOL_CONTENT", 0, 0
    try:
        data = json.loads(message.content)
        if not isinstance(data, dict) or not isinstance(data.get("citations"), list):
            return message, "INVALID_TOOL_PAYLOAD", 0, 0
        selected = []
        for citation in data["citations"]:
            if not isinstance(citation, dict):
                return message, "INVALID_CITATION", 0, 0
            if citation.get("content_type") == "image":
                # Registered image locators remain unverified; do not change them.
                if citation.get("content") not in ("", None):
                    return message, "NON_CANONICAL_IMAGE", 0, 0
                continue
            source_id, content = citation.get("source_id"), citation.get("content")
            if (not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0
                    or not citation.get("chunk_id") or not isinstance(content, str) or not content):
                return message, "UNREGISTERED_CITATION", 0, 0
            selected.append(citation)
        if not selected:
            return message, "NO_TEXT_EVIDENCE", 0, 0
        if data.get("source_ids") != [citation["source_id"] for citation in selected]:
            return message, "SOURCE_IDS_MISMATCH", 0, 0
        if data.get("context") != canonical_context(selected):
            return message, "CONTEXT_MISMATCH", 0, 0
        projected_citations = [({key: value for key, value in citation.items() if key != "content"}
                                if citation.get("content_type") != "image" else dict(citation))
                               for citation in data["citations"]]
        result = {**data, "citations": projected_citations}
        chars = sum(len(citation["content"]) for citation in selected)
        byte_count = sum(len(citation["content"].encode("utf-8")) for citation in selected)
        return message.model_copy(update={"content": json.dumps(result, ensure_ascii=False)}), "PROJECTED", chars, byte_count
    except (TypeError, ValueError, KeyError):
        return message, "INVALID_TOOL_PAYLOAD", 0, 0


def project_agent_evidence_messages(messages, *, canonical_context, tools=None):
    """Return an invocation copy; each tool result retains its own complete context."""
    projected = []
    statuses = Counter()
    removed_chars = removed_bytes = 0
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == "search_knowledge_base":
            result, status, chars, byte_count = _project_tool(message, canonical_context)
            statuses[status] += 1
            removed_chars += chars
            removed_bytes += byte_count
            projected.append(result)
        else:
            projected.append(message)
    return projected, {"projection_version": VERSION, "message_status_counts": dict(statuses),
        "removed_body_chars": removed_chars, "removed_body_utf8_bytes": removed_bytes,
        "before": input_size_breakdown(messages, tools), "after": input_size_breakdown(projected, tools),
        "input_tokens": None, "token_count_status": "UNKNOWN"}
