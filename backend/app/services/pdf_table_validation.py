"""Conservative, read-only verification of already cited PDF table cells.

This does not select documents or add model context. It only verifies a
retrieved chunk against cells on its own original PDF page.
"""
from pathlib import Path
import hashlib
import re


def verified_pdf_rows(session, citations, kb_ids, selected_indices=None):
    """Return citation-indexed field/value/unit triples or no evidence.

    A result requires the chunk, file and KB to agree, the exact chunk text
    to match the citation, and a PDF table with explicit project/row/value
    column headings. Unsupported table layouts fail closed.
    """
    from app.models import DocumentChunk, FileDocument
    try:
        import fitz
    except ImportError:
        return {}
    verified = {}
    for index, citation in enumerate(citations, 1):
        if selected_indices is not None and index not in selected_indices:
            continue
        try:
            chunk = session.get(DocumentChunk, citation.get('chunk_id'))
            doc = session.get(FileDocument, citation.get('fileId')) if chunk else None
            if (not chunk or not doc or chunk.file_id != doc.id or doc.kb_id not in kb_ids
                    or chunk.content != citation.get('content') or not chunk.page_number):
                continue
            path = Path(doc.local_path)
            if path.suffix.lower() != '.pdf' or not path.is_file() or path.stat().st_size > 50_000_000:
                continue
            before = path.stat()
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
            with fitz.open(path) as pdf:
                page_index = chunk.page_number - 1
                if page_index < 0 or page_index >= len(pdf):
                    continue
                page = pdf[page_index]
                rows = []
                page_headers = []
                locators = []
                table_groups = []
                cover = pdf[0].get_text() if len(pdf) else ''
                previous_bottom = 0
                for table in sorted(page.find_tables().tables, key=lambda t: (t.bbox[1], t.bbox[0])):
                    # Overlapping/side-by-side tables cannot be assigned a unique
                    # title using vertical positions alone; fail closed for them.
                    top = table.bbox[1]
                    if top < previous_bottom:
                        table_groups = []
                        rows = []
                        break
                    header = page.get_textbox(fitz.Rect(0, previous_bottom, page.rect.width, top))
                    previous_bottom = table.bbox[3]
                    group_rows = []
                    units = re.findall(r'(?:金额)?单位\s*[：:]\s*([^\s]+)', header)
                    if len(units) != 1 or not re.fullmatch(r'[\u4e00-\u9fff]{1,8}', units[0]):
                        continue
                    page_headers.append(header)
                    locators.append({'page': chunk.page_number, 'bbox': list(table.bbox)})
                    table_rows = table.extract()
                    for header_index, table_header in enumerate(table_rows):
                        normalized = [re.sub(r'\s+', '', str(cell or '')) for cell in table_header]
                        triples = [i for i in range(len(normalized) - 2)
                                   if normalized[i:i + 3] == ['项目', '行次', '决算数']]
                        if not triples:
                            continue
                        for row in table_rows[header_index + 1:]:
                            for col in triples:
                                if len(row) <= col + 2:
                                    continue
                                field = re.sub(r'^\s*[一二三四五六七八九十]+、', '', str(row[col] or '')).strip()
                                ordinal = re.sub(r'\s+', '', str(row[col + 1] or ''))
                                value = re.sub(r'\s+', '', str(row[col + 2] or ''))
                                if (not re.fullmatch(r'[\u4e00-\u9fff（）()]{2,30}', field)
                                        or not ordinal.isdigit()
                                        or not re.fullmatch(r'-?\d[\d,]*(?:\.\d+)?', value)):
                                    continue
                                # The exact indexed text must preserve the same
                                # field, row number and value in that order.
                                row_text = (re.escape(field) + r'\s*' + re.escape(ordinal) +
                                            r'\s*' + re.escape(value) + r'(?![\d.,])')
                                if re.search(row_text, chunk.content):
                                    row_evidence = (field, value, units[0])
                                    rows.append(row_evidence)
                                    group_rows.append(row_evidence)
                    if group_rows:
                        table_groups.append({'header': header, 'rows': group_rows})
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    continue
                if rows:
                    verified[index] = {'rows': rows, 'page_headers': page_headers,
                                       'table_groups': table_groups,
                                       'cover': cover, 'cover_page': 1, 'tables': locators,
                                       'source_sha256': digest.hexdigest()}
        except Exception:
            # Missing source, parser errors or unusual layouts never authorize
            # an assertion and never interrupt the main question-answer path.
            continue
    return verified
