"""Opt-in character-based parent/child graphs; never infer historical links."""
from dataclasses import dataclass
import hashlib
import re
from uuid import UUID, uuid4


IMAGE_DESCRIPTION_ORIGIN = 'model_generated_image_description'
IMAGE_DESCRIPTION_LABEL = '[图片描述（模型生成，需核对原图）]'
IMAGE_DESCRIPTION_DERIVATION = 'image-caption-text-v1'


def derive_image_description_chunk(image, caption, *, source_sha256, image_sha256,
                                   model, prompt_sha256, max_tokens, effect_key=None):
    """Separate derived text from the immutable original image placeholder.

    The locator identifies the image region; it is never a word/byte offset in
    the PDF. Legacy parsers without a bbox retain honest page-only provenance.
    """
    from app.services.parser import ParsedChunk
    if image.content_type != 'image' or not image.image_path or type(image.page_number) is not int or image.page_number < 1:
        raise ValueError('IMAGE_DESCRIPTION_SOURCE_INVALID')
    if not isinstance(caption, str) or not caption.strip():
        raise ValueError('IMAGE_DESCRIPTION_EMPTY')
    if (any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
            for value in (source_sha256, image_sha256, prompt_sha256))
            or not isinstance(model, str) or not model.strip()
            or isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 128 <= max_tokens <= 32768):
        raise ValueError('IMAGE_DESCRIPTION_IDENTITY_INVALID')
    body = caption.strip()
    if body.startswith(IMAGE_DESCRIPTION_LABEL):
        body = body[len(IMAGE_DESCRIPTION_LABEL):].lstrip()
    if not body:
        raise ValueError('IMAGE_DESCRIPTION_EMPTY')
    original = dict((image.metadata or {}).get('source_locator') or {})
    if original.get('page', image.page_number) != image.page_number:
        raise ValueError('IMAGE_DESCRIPTION_PAGE_MISMATCH')
    if original.get('source_sha256', source_sha256) != source_sha256:
        raise ValueError('IMAGE_DESCRIPTION_SOURCE_MISMATCH')
    descriptor = dict(status='COMPLETED', derivation=IMAGE_DESCRIPTION_DERIVATION,
        image_sha256=image_sha256, model=model, prompt_sha256=prompt_sha256,
        max_tokens=max_tokens, source_image_path=image.image_path,
        source_page=image.page_number, effect_key=effect_key,
        caption_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(),
        requires_original_verification=True)
    locator = {**original, 'source_sha256': source_sha256, 'page': image.page_number,
        'kind': 'pdf-image-description', 'derived_from_kind': original.get('kind', 'pdf-image'),
        'text_origin': IMAGE_DESCRIPTION_ORIGIN,
        'geometry_status': 'IMAGE_REGION' if original.get('bbox') else 'PAGE_ONLY'}
    return ParsedChunk(IMAGE_DESCRIPTION_LABEL + '\n' + body, image.page_number, 'text',
        metadata={'content_origin': IMAGE_DESCRIPTION_ORIGIN, 'image_description': descriptor,
            'source_locator': locator, 'heading_text': IMAGE_DESCRIPTION_LABEL,
            'source_offset_status': 'UNKNOWN'})


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
        derived = (meta.get('content_origin') == IMAGE_DESCRIPTION_ORIGIN and
            (meta.get('image_description') or {}).get('derivation') == IMAGE_DESCRIPTION_DERIVATION)
        if piece.content_type != "text" or (meta.get('content_origin') == IMAGE_DESCRIPTION_ORIGIN and not derived):
            result.append(piece)
            continue
        prefix = IMAGE_DESCRIPTION_LABEL + '\n' if derived else ''
        body = piece.content
        if derived:
            if not body.startswith(prefix):
                raise ValueError('IMAGE_DESCRIPTION_LABEL_MISSING')
            body = body[len(prefix):]
            if config.parent_chunk_size <= len(prefix):
                raise ValueError('IMAGE_DESCRIPTION_PARENT_SIZE_TOO_SMALL')
        parent_size = config.parent_chunk_size - len(prefix)
        parent_overlap = min(config.parent_chunk_overlap, parent_size - 1)
        for start, end in split_spans(body, parent_size, parent_overlap):
            parent_id = str(uuid4())
            parent_body = body[start:end]
            parent_text = prefix + parent_body
            # Legacy temporary IDs are deliberately discarded for this newly parsed graph.
            shared = {k: v for k, v in meta.items() if k not in ("id", "parent_id", "chunk_type", "start_offset", "end_offset")}
            shared["parent_child_version"] = "character-v1"
            shared["source_offset_status"] = "UNKNOWN"
            parent_shared = dict(shared)
            if derived:
                parent_shared['image_description'] = {**shared['image_description'],
                    'indexed_text_sha256': hashlib.sha256(parent_text.encode('utf-8')).hexdigest()}
            result.append(ParsedChunk(parent_text, piece.page_number, "text", piece.image_path,
                {**parent_shared, "id": parent_id, "parent_id": None, "chunk_type": "parent"}, count_tokens(parent_text)))
            for cstart, cend in split_spans(parent_body, config.child_chunk_size, config.child_chunk_overlap):
                text = parent_body[cstart:cend]
                child_shared = dict(shared)
                if derived:
                    child_shared['image_description'] = {**shared['image_description'],
                        'indexed_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                result.append(ParsedChunk(text, piece.page_number, "text", piece.image_path,
                    {**child_shared, "id": str(uuid4()), "parent_id": parent_id, "chunk_type": "child",
                     "parent_local_start": len(prefix)+cstart, "parent_local_end": len(prefix)+cend}, count_tokens(text)))
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
