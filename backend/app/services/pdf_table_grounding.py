"""Optional source-native PDF table expansion from retrieved file identities.

This module never uses benchmark answers or selects additional documents. It
returns proposed supplementary source locators, not indexed chunks or a verdict.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import time

# These are intentionally the only supported short-to-formal caption mappings.
_CAPTIONS = {'总收支表': '收入支出决算总表',
             '财政拨款收支表': '财政拨款收入支出决算总表'}


@dataclass(frozen=True)
class PdfTableGrounding:
    question: str
    source_sha256: str
    file_id: str
    kb_id: str
    parent_chunk_id: str
    cover: str
    tables: tuple
    supplementary_citations: tuple
    statement: str


def _compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def _question(question):
    if not isinstance(question, str):
        return None
    m = re.fullmatch(
        r'\s*([\u4e00-\u9fff]{2,20})(\d{4})年度([“《])([\u4e00-\u9fff]{2,30})([”》])在'
        r'(总收支表|财政拨款收支表)(?:与|和|及)(总收支表|财政拨款收支表)中分别是多少万元[？?]'
        r'(?:请按这两个口径的顺序回答[。.]?)?\s*', question)
    if (not m or (m.group(3), m.group(5)) not in {('“', '”'), ('《', '》')}
            or m.group(6) == m.group(7)):
        return None
    return m.group(1), m.group(2), m.group(4), m.group(6), m.group(7)


def _source_tables(path, source_digest, organization, year, field, scopes):
    import fitz
    if path.suffix.lower() != '.pdf' or not path.is_file() or path.stat().st_size > 50_000_000:
        return None
    before = path.stat()
    data = path.read_bytes()
    if sha256(data).hexdigest() != source_digest:
        return None
    started = time.monotonic()
    with fitz.open(stream=data, filetype='pdf') as doc:
        if not 1 <= len(doc) <= 100:
            return None
        cover = doc[0].get_text()
        if not _compact(cover).startswith(organization + year + '年度部门决算'):
            return None
        found = {scope: [] for scope in scopes}
        for page_no in range(len(doc)):
            if time.monotonic() - started > 10:
                return None
            page = doc[page_no]
            # Only inspect pages whose extracted text contains an exact caption.
            page_text = page.get_text()
            names = [scope for scope in scopes if any(
                _compact(line) == _CAPTIONS[scope] for line in page_text.splitlines())]
            if not names:
                continue
            if len(names) != 1:
                return None
            scope = names[0]
            tables = page.find_tables().tables
            if len(tables) != 1:
                return None
            table = tables[0]
            header = page.get_textbox(fitz.Rect(0, 0, page.rect.width, table.bbox[1]))
            lines = [_compact(line) for line in header.splitlines() if _compact(line)]
            if lines.count(_CAPTIONS[scope]) != 1:
                return None
            units = re.findall(r'(?:金额)?单位[：:]([^\s]+)', header)
            if len(units) != 1 or units[0] != '万元':
                return None
            rows = table.extract()
            if len(rows) < 4:
                return None
            h0 = [_compact(x) for x in rows[0]]
            h1 = [_compact(x) for x in rows[1]]
            # The expenditure side must be explicit. For the fiscal table,
            # select the total column by the entire header, not by a repeated
            # amount in an adjacent component column.
            common = h0[:4] == ['收入', '', '', '支出']
            if scope == '总收支表':
                layout = h1 == ['项目', '行次', '决算数', '项目', '行次', '决算数']
            else:
                layout = h1 == ['项目', '行次', '金额', '项目', '行次', '合计',
                                '一般公共预算财政拨款', '政府性基金预算财政拨款', '国有资本经营预算财政拨款']
            if not common or not layout:
                return None
            matching = []
            for row_index, row in enumerate(rows[3:], 3):
                if len(row) < 6:
                    return None
                label = re.sub(r'^[一二三四五六七八九十]+、', '', _compact(row[3]))
                ordinal, value = _compact(row[4]), _compact(row[5])
                if label == field:
                    if not ordinal.isdigit() or not re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', value):
                        return None
                    physical = table.rows[row_index].cells
                    if len(physical) <= 5 or not physical[3] or not physical[5]:
                        return None
                    matching.append({'scope': scope, 'caption': _CAPTIONS[scope],
                        'page': page_no + 1, 'table_bbox': list(table.bbox),
                        'field_bbox': list(physical[3]), 'value_bbox': list(physical[5]),
                        'field': field, 'ordinal': ordinal, 'value': value, 'unit': units[0],
                        'header': header, 'column_headers': rows[:3],
                        'source_note': re.sub(r'^—\d+—\s*', '', page.get_textbox(fitz.Rect(0, table.bbox[3], page.rect.width, page.rect.height)).strip()),
                        'note_bbox': [0, table.bbox[3], page.rect.width, page.rect.height]})
            if len(matching) != 1:
                return None
            found[scope].extend(matching)
        if any(len(found[x]) != 1 for x in scopes):
            return None
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            return None
        return cover, tuple(found[x][0] for x in scopes)


def prepare_pdf_table_grounding(session, citations, kb_ids, question):
    """Return one uniquely bound same-source proof or None.

    Only the explicit grammar and native table layouts above are supported.
    Each supplementary citation retains a genuine parent chunk ID, but its
    new page/geometry describes a source expansion, not the parent chunk's
    indexed location. Consumers must explicitly teach prompt, validation and
    citation display this distinction; this object authorizes no answer by itself.
    """
    try:
        from app.models import DocumentChunk, FileDocument
        from app.services.source_registry import trusted_source_hash
        parsed = _question(question)
        if not parsed or len(citations) > 100:
            return None
        organization, year, field, scope_a, scope_b = parsed
        scopes = (scope_a, scope_b)
        sources = {}
        for citation in citations:
            chunk = session.get(DocumentChunk, citation.get('chunk_id'))
            doc = session.get(FileDocument, citation.get('fileId')) if chunk else None
            if (not doc or chunk.file_id != doc.id or doc.kb_id not in kb_ids
                    or citation.get('kb_id') != doc.kb_id or citation.get('content') != chunk.content
                    or not isinstance(chunk.content, str) or not chunk.content.strip()):
                continue
            if Path(doc.local_path).suffix.lower() == '.pdf':
                sources.setdefault(doc.id, (doc, citation['chunk_id']))
        if not sources or len(sources) > 5:
            return None
        proofs = []
        for doc, parent in sources.values():
            digest = trusted_source_hash(doc.local_path)
            if not digest:
                continue
            parsed_source = _source_tables(Path(doc.local_path), digest, organization, year, field, scopes)
            if not parsed_source:
                continue
            cover, tables = parsed_source
            supplemental = []
            pieces = []
            for offset, table in enumerate(tables, 1):
                index = len(citations) + offset
                supplemental.append({'index': index, 'source_type': 'source_native_table_expansion',
                    'fileId': doc.id, 'kb_id': doc.kb_id, 'parent_chunk_id': parent,
                    'source_sha256': digest, 'page': table['page'],
                    'table_bbox': table['table_bbox'], 'field_bbox': table['field_bbox'],
                    'value_bbox': table['value_bbox'], 'note_bbox': table['note_bbox'],
                    'cover_page': 1, 'caption': table['caption'],
                    'raw_header': table['header'], 'column_headers': table['column_headers'],
                    'source_note': table['source_note'], 'field': field, 'value': table['value'], 'unit': table['unit']})
                note = (' 原表说明：' + _compact(table['source_note'])) if table['source_note'] else ''
                pieces.append(f"{organization}{year}年度《{table['caption']}》中，{field}为{table['value']}{table['unit']}。{note}[{index}]")
            proofs.append(PdfTableGrounding(question, digest, doc.id, doc.kb_id, parent, cover,
                                             tables, tuple(supplemental), ''.join(pieces)))
        return proofs[0] if len(proofs) == 1 else None
    except Exception:
        return None


def build_pdf_supplementary_citations(context, citations):
    """Build display records only from a server-created proof and its parent."""
    from urllib.parse import quote
    if not isinstance(context, PdfTableGrounding):
        return []
    parents = [c for c in citations if c.get('chunk_id') == context.parent_chunk_id
               and c.get('fileId') == context.file_id and c.get('kb_id') == context.kb_id
               and isinstance(c.get('content'), str) and c['content'].strip()]
    if len(parents) != 1 or len(context.tables) != 2 or len(context.supplementary_citations) != 2:
        return []
    parent = parents[0]
    records = []
    for offset, (item, table) in enumerate(zip(context.supplementary_citations, context.tables), 1):
        if (item['index'] != len(citations) + offset or item['parent_chunk_id'] != context.parent_chunk_id
                or item['fileId'] != context.file_id or item['kb_id'] != context.kb_id
                or item['source_sha256'] != context.source_sha256 or item['page'] != table['page']):
            return []
        url = ('/api/v1/files/' + quote(context.file_id, safe='') + '/source?kb_id='
               + quote(context.kb_id, safe='') + '&sha256=' + quote(context.source_sha256, safe='')
               + '#page=' + str(table['page']))
        content = ('原始PDF表格扩展（非索引片段）：\n封面：' + context.cover
                   + '\n表头：' + table['header'] + '\n列头：' + str(table['column_headers'])
                   + '\n字段：' + table['field'] + '；行次：' + table['ordinal']
                   + '；数值：' + table['value'] + table['unit'] + '\n原表说明：' + table['source_note'])
        records.append({'source_type': 'source_native_table_expansion', 'parent_chunk_id': context.parent_chunk_id,
            'fileId': context.file_id, 'kb_id': context.kb_id, 'kb_name': parent.get('kb_name', ''),
            'fileName': parent.get('fileName', ''), 'content': content,
            'location': f"第 {table['page']} 页（原始表格扩展）；第 1 页（封面）",
            'sourceUrl': url, 'source_metadata': dict(item)})
    return records


def _short_statements(context):
    """Bind either two complete clauses or one shared subject to both proofs.

    The shared form has the grammar SUBJECT + CLAUSE + comma + CLAUSE +
    period + both citation markers, in proof order. No semantic substitutions
    or arbitrary prose are accepted.
    """
    parsed = _question(context.question)
    if not parsed or len(context.tables) != 2 or len(context.supplementary_citations) != 2:
        return ()
    organization, year, field, *scopes = parsed
    subject = f"{organization}{year}年度“{field}”"
    clauses, markers = [], []
    for scope, table, citation in zip(scopes, context.tables, context.supplementary_citations):
        if (table['scope'] != scope or table['caption'] != _CAPTIONS[scope]
                or table['field'] != field):
            return ()
        clauses.append(f"在{scope}中为{table['value']}{table['unit']}")
        markers.append(f"[{citation['index']}]")
    repeated = '\n'.join(subject + clause + '。' + marker
                         for clause, marker in zip(clauses, markers))
    shared = subject + clauses[0] + '，' + clauses[1] + '。' + ''.join(markers)
    return repeated, shared


def _short_answer_matches(context, answer):
    normalized = re.sub(r'\s+', '', answer)
    return any(normalized == re.sub(r'\s+', '', expected)
               for expected in _short_statements(context))


def pdf_expansion_supports(context, citations, question, answer):
    """Require both exact server-built records and one complete supported form."""
    if not isinstance(context, PdfTableGrounding) or question != context.question:
        return False
    if answer.strip() != context.statement and not _short_answer_matches(context, answer):
        return False
    if len(citations) < 3:
        return False
    original = citations[:-2]
    expected = build_pdf_supplementary_citations(context, original)
    return len(expected) == 2 and list(citations[-2:]) == expected


def render_pdf_expansion_answer(context, answer):
    """Append exact source notes to the verified short form without model calls."""
    if not isinstance(context, PdfTableGrounding) or not _short_answer_matches(context, answer):
        return answer
    notes = []
    for table, citation in zip(context.tables, context.supplementary_citations):
        if table['source_note']:
            notes.append(f"{table['scope']}原表说明：\n{table['source_note']}[{citation['index']}]")
    return answer + ('\n\n' + '\n\n'.join(notes) if notes else '')
