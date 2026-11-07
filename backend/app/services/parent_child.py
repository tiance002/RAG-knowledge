"""Opt-in character-based parent/child graphs; never infer historical links."""
from dataclasses import dataclass
import re
from uuid import UUID, uuid4


@dataclass(frozen=True)
class ParentChildConfig:
    parent_chunk_size: int = 4096
    parent_chunk_overlap: int = 0
    child_chunk_size: int = 384
    child_chunk_overlap: int = 48

    def validate(self):
        for size, overlap in ((self.parent_chunk_size, self.parent_chunk_overlap),
                              (self.child_chunk_size, self.child_chunk_overlap)):
            if isinstance(size, bool) or isinstance(overlap, bool) or not isinstance(size, int) or not isinstance(overlap, int):
                raise ValueError("PARENT_CHILD_CONFIGURATION_INVALID")
            if size <= 0 or not 0 <= overlap < size:
                raise ValueError("PARENT_CHILD_CONFIGURATION_INVALID")
        if self.child_chunk_size > self.parent_chunk_size:
            raise ValueError("CHILD_SIZE_EXCEEDS_PARENT")
        return self


def split_spans(text, size, overlap):
    """Exact spans in this extracted text. Prefer heading/paragraph/sentence ends.

    These offsets are local to the extracted text, NOT source-file byte offsets.
    No normalization/stripping changes the original content.
    """
    start = 0
    boundaries = [m.end() for m in re.finditer(r"\n(?=#{1,6}\s)|\n\s*\n|[。！？.!?](?:\s|$)", text)]
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            preferred = [p for p in boundaries if start + max(size // 2, overlap + 1) <= p <= end]
            if preferred:
                end = preferred[-1]
        yield start, end
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def build_parent_child_chunks(parsed_chunks, config=None, count_tokens=None):
    """Keep parser pieces as conservative structure boundaries; special blocks stay standalone.

    Source offsets remain unknown because parser pieces may contain generated heading
    context or come from non-linear formats. Child local spans are metadata only.
    """
    from app.services.parser import ParsedChunk
    config = (config or ParentChildConfig()).validate()
    count_tokens = count_tokens or (lambda text: 0)
    result = []
    for piece in parsed_chunks:
        meta = dict(piece.metadata or {})
        if piece.content_type != "text" or meta.get("content_origin") == "model_generated_image_description":
            result.append(piece)
            continue
        for start, end in split_spans(piece.content, config.parent_chunk_size, config.parent_chunk_overlap):
            parent_id = str(uuid4())
            parent_text = piece.content[start:end]
            # Legacy temporary IDs are deliberately discarded for this newly parsed graph.
            shared = {k: v for k, v in meta.items() if k not in ("id", "parent_id", "chunk_type", "start_offset", "end_offset")}
            shared["parent_child_version"] = "character-v1"
            shared["source_offset_status"] = "UNKNOWN"
            result.append(ParsedChunk(parent_text, piece.page_number, "text", piece.image_path,
                {**shared, "id": parent_id, "parent_id": None, "chunk_type": "parent"}, count_tokens(parent_text)))
            for cstart, cend in split_spans(parent_text, config.child_chunk_size, config.child_chunk_overlap):
                text = parent_text[cstart:cend]
                result.append(ParsedChunk(text, piece.page_number, "text", piece.image_path,
                    {**shared, "id": str(uuid4()), "parent_id": parent_id, "chunk_type": "child",
                     "parent_local_start": cstart, "parent_local_end": cend}, count_tokens(text)))
    return result


def remap_and_validate_graph(chunks_data, file_id):
    """Assign fresh staged IDs and validate only explicitly typed new graphs.

    Fresh IDs prevent a failed replacement write from touching active vectors.
    Historical ordinary chunks keep their untrusted parent metadata unchanged.
    """
    rows = [dict(c) for c in chunks_data]
    typed = [r for r in rows if r.get("chunk_type") in ("parent", "child")]
    ids = [r.get("id") for r in typed]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("PARENT_CHILD_IDS_INVALID")
    by_id = {r.get("id"): r for r in typed}
    for row in typed:
        try:
            UUID(row["id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("PARENT_CHILD_IDS_INVALID") from exc
        if row.get("file_id", file_id) != file_id:
            raise ValueError("PARENT_CHILD_FILE_MISMATCH")
        if row["chunk_type"] == "parent":
            if row.get("parent_id"):
                raise ValueError("PARENT_CANNOT_HAVE_PARENT")
        else:
            parent = by_id.get(row.get("parent_id"))
            if not parent or parent.get("chunk_type") != "parent":
                raise ValueError("PARENT_CHUNK_MISSING_OR_INVALID")
            if parent.get("file_id", file_id) != file_id:
                raise ValueError("PARENT_CHILD_FILE_MISMATCH")
            if row.get("content", "") not in parent.get("content", ""):
                raise ValueError("CHILD_NOT_IN_PARENT")
    mapping = {r["id"]: str(uuid4()) for r in typed}
    for row in rows:
        old_id = row.get("id")
        row["id"] = mapping.get(old_id, str(uuid4()))
        if row.get("chunk_type") == "child":
            row["parent_id"] = mapping[row["parent_id"]]
    return rows
