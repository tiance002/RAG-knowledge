"""One post-extraction entry point: ordinary OR parent-child, never both."""
from dataclasses import dataclass
import hashlib
import re
from uuid import uuid4

from app.services.go_legacy_splitter import split_legacy, Unit
from app.services.parsed_document import ParsedDocument


@dataclass(frozen=True)
class ChunkConfig:
    size: int = 512
    overlap: int = 80
    mode: str = 'legacy'
    parent_child: object = None
    separators: tuple = ('\n\n', '\n', '。')


def _metadata(spans, start, end, source_sha):
    hits = [s.block for s in spans if s.start < end and s.end > start]
    locators = []
    for block in hits:
        meta = block.metadata or {}
        values = meta.get('source_locators') or [meta.get('source_locator', {})]
        locators.extend(v for v in values if v and v not in locators)
    pages = sorted({b.page_number for b in hits if b.page_number is not None})
    locator = dict(locators[0]) if len(locators) == 1 else {'kind': 'document-span', 'regions': locators}
    if locators and all(loc.get('sheet') == locators[0].get('sheet') for loc in locators) and locators[0].get('sheet'):
        locator.update(sheet=locators[0]['sheet'], row_start=min(loc['row_start'] for loc in locators),
                       row_end=max(loc['row_end'] for loc in locators),column_start=1,
                       column_end=max(loc.get('column_end',1) for loc in locators))
    locator.update(source_sha256=source_sha, normalized_start=start, normalized_end=end)
    if pages:
        locator['pages'] = pages
        if len(pages) == 1:
            locator['page'] = pages[0]
    metadata = dict(source_locator=locator, source_locators=locators,
                    normalized_text_start=start, normalized_text_end=end,
                    source_offset_status='NORMALIZED_DOCUMENT_TEXT_ONLY', chunker_version='unified-legacy-v1')
    # Preserve origin and source-format attributes only when common to all hits.
    for key in ('content_origin', 'image_description', 'excel_parse_version', 'xlsx_first_row_as_header',
                'currency_format_status', 'heading_text', 'heading_level'):
        values = [(b.metadata or {}).get(key) for b in hits]
        if values and all(v == values[0] for v in values) and values[0] is not None:
            metadata[key] = values[0]
    formats = [f for b in hits for f in (b.metadata or {}).get('currency_formats', [])]
    if formats:
        metadata['currency_formats'] = formats
    generated = [(b.metadata or {}) for b in hits if (b.metadata or {}).get('content_origin') == 'model_generated_image_description']
    if generated:
        metadata['content_origin'] = 'model_generated_image_description'
        metadata['contains_original_text'] = len(generated) != len(hits)
        if 'image_description' not in metadata:
            metadata['image_descriptions'] = [m['image_description'] for m in generated if m.get('image_description')]
    return metadata, pages[0] if len(pages) == 1 else None


def split_parent_child(text, config):
    """WeKnora legacy SplitTextParentChild including parent omission and offsets."""
    config.validate()
    parents, children = [], []
    for parent in split_legacy(text, config.parent_chunk_size, config.parent_chunk_overlap):
        offset = parent.end - len(parent.content)
        subs = [(c, max(parent.start, offset + c.start), offset + c.end)
                for c in split_legacy(parent.content, config.child_chunk_size, config.child_chunk_overlap)]
        subs = [(c, start, end) for c, start, end in subs if end > start]
        index = -1
        if len(subs) > 1 or (len(subs) == 1 and subs[0][0].content != parent.content):
            index = len(parents)
            parents.append(parent)
        children.extend((c, start, end, index) for c, start, end in subs)
    return parents, children


def chunk_document(document, config, *, count_tokens=len, semantic_split=None):
    from app.services.parser import ParsedChunk
    text, spans = document.text_and_spans()
    if config.size <= 0 or not 0 <= config.overlap < config.size:
        raise ValueError('CHUNK_CONFIGURATION_INVALID')
    if any((b.metadata or {}).get('chunker_version') for b in document.blocks):
        raise ValueError('DOCUMENT_ALREADY_CHUNKED')
    if config.parent_child and config.mode in ('semantic', 'no_chunk', 'no_split'):
        raise ValueError('PARENT_CHILD_REQUIRES_TEXT_CHUNKING_STRATEGY')
    result = []

    def emit(body, start, end, *, extra=None):
        # WeKnora processChunks filters blank output after raw SplitText /
        # SplitTextParentChild. Keep core parity but never embed empty units.
        if not body.strip():
            return
        meta, page = _metadata(spans, start, end, document.source_sha256)
        meta.update(extra or {})
        if meta.get('image_description'):
            meta['image_description'] = {**meta['image_description'],
                'indexed_text_sha256': hashlib.sha256(body.encode()).hexdigest()}
        if meta.get('image_descriptions'):
            meta['image_descriptions'] = [{**d,'indexed_text_sha256':hashlib.sha256(body.encode()).hexdigest()}
                                          for d in meta['image_descriptions']]
        hits = [s.block for s in spans if s.start < end and s.end > start]
        kind = 'table' if hits and all(b.content_type == 'table' for b in hits) else 'text'
        result.append(ParsedChunk(body, page, kind, metadata=meta, token_count=count_tokens(body)))

    if config.parent_child:
        parents, children = split_parent_child(text, config.parent_child)
        ids = [str(uuid4()) for _ in parents]
        for parent, ident in zip(parents, ids):
            emit(parent.content, parent.start, parent.end, extra=dict(id=ident, parent_id=None, chunk_type='parent',
                                                                   chunker_sequence=parent.seq))
        for child, start, end, index in children:
            parent = parents[index] if index >= 0 else None
            emit(child.content, start, end, extra=dict(id=str(uuid4()),
                parent_id=ids[index] if index >= 0 else None, chunk_type='child' if index >= 0 else 'content',
                parent_local_start=child.start if parent else None, parent_local_end=child.end if parent else None,
                child_chunk_size=config.parent_child.child_chunk_size,
                child_chunk_overlap=config.parent_child.child_chunk_overlap,
                parent_child_version='unified-go-legacy-v1'))
    elif config.mode in ('no_split', 'no_chunk'):
        if text:
            emit(text, 0, len(text))
    elif config.mode == 'semantic':
        if semantic_split is None:
            raise ValueError('SEMANTIC_SPLITTER_UNAVAILABLE')
        cursor = 0
        for body in semantic_split(text):
            start = text.find(body, cursor)
            if start < 0:
                raise ValueError('SEMANTIC_SOURCE_ALIGNMENT_FAILED')
            emit(body, start, start + len(body))
            cursor = start + len(body)
    else:
        # Explicit heading strategy stays in the common module, not parsers.
        boundaries = [0]
        if config.mode == 'structure':
            boundaries += [m.start() for m in re.finditer(r'(?m)^#{1,6}\s+', text) if m.start()]
        boundaries.append(len(text))
        for start, end in zip(boundaries, boundaries[1:]):
            for part in split_legacy(text[start:end], config.size, config.overlap, config.separators):
                emit(part.content, start + part.start, start + part.end)
    # Original binary images remain distinct assets; captions are text inputs.
    result.extend(b for b in document.blocks if b.content_type == 'image')
    return result


def child_supported_by_parent(child, parent, size=384, overlap=48):
    """Validate generated table-header children without weakening source checks."""
    if child in parent:
        return True
    if not (0 < size <= 65536 and 0 <= overlap < size) or len(parent) > 100000:
        return False
    return any(c.content == child for c in split_legacy(parent, size, overlap))
