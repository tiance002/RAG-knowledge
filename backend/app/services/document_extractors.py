"""Format readers produce source units, never retrieval chunks."""
from pathlib import Path
import hashlib
import re

from app.services.parsed_document import ParsedDocument


def extract_document(parser, file_path):
    from app.services.parser import ParsedChunk
    path = Path(file_path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ext = path.suffix.lower()
    blocks, pages = [], []

    def add(text, locator, *, page=None, kind='text', metadata=None):
        if text:
            blocks.append(ParsedChunk(text, page, kind, metadata={
                **(metadata or {}), 'source_locator': {**locator, 'source_sha256': sha}}))

    if ext == '.xlsx':
        blocks = parser._extract_excel(str(path))
        for block in blocks:
            block.metadata['source_locator']['source_sha256'] = sha
    elif ext in ('.txt', '.md'):
        try:
            text = path.read_text(encoding='utf8')
        except UnicodeDecodeError:
            text = path.read_text(encoding='gbk')
        if ext == '.md':
            from app.services.text_rechunk import markdown_blocks
            for index, (kind, body) in enumerate(markdown_blocks(text)):
                add(body, {'kind': 'markdown-' + kind, 'block_index': index,
                           'offset_status': 'NORMALIZED_BLOCK_ONLY'})
        else:
            add(text, {'kind': 'text', 'line_start': 1, 'line_end': len(text.splitlines())})
    elif ext == '.csv':
        import csv
        from io import StringIO
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            text = path.read_text(encoding='gbk')
        rows = list(csv.reader(StringIO(text)))
        width = max(map(len, rows), default=0)
        def render(row):
            values = [str(v).replace('|', '\\|').replace('\r\n', '<br>').replace('\n', '<br>') for v in row]
            return '| ' + ' | '.join(values + [''] * (width-len(values))) + ' |\n'
        if rows:
            add(render(rows[0]) + render(['---']*width), {'kind':'csv-header','record_start':0,'record_end':0})
            for i, row in enumerate(rows[1:], 1):
                add(render(row), {'kind':'csv-record','record_start':i,'record_end':i,
                                  'column_start':1,'column_end':width})
    elif ext == '.docx':
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(str(path))
        for index, element in enumerate(document.element.body):
            tag = element.tag.rsplit('}', 1)[-1]
            locator = {'kind':'docx-' + tag, 'body_index':index, 'page_status':'UNKNOWN'}
            if tag == 'p':
                paragraph = Paragraph(element, document)
                text = paragraph.text
                heading = re.match(r'Heading\s+(\d+)', paragraph.style.name or '', re.I)
                if heading:
                    text = '#' * min(6, int(heading.group(1))) + ' ' + text
                add(text + '\n' if text else '', locator)
                for image_index, blip in enumerate(element.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')):
                    rid = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    if rid not in document.part.related_parts:
                        continue
                    image = document.part.related_parts[rid]
                    if len(image.blob) < 2048:
                        continue
                    suffix = image.content_type.split('/')[-1].replace('jpeg', 'jpg')
                    image_path = parser._save_image(image.blob, sha[:16], 0, f'{index}-{image_index}', suffix)
                    blocks.append(ParsedChunk(f'[图片: {image_path}]', None, 'image', image_path,
                        {'source_locator':{**locator,'source_sha256':sha,'image_index':image_index}}))
            elif tag == 'tbl':
                table = Table(element, document)
                rows = [[c.text.replace('|','\\|').replace('\n','<br>') for c in row.cells] for row in table.rows]
                if rows:
                    for row_index, values in enumerate(rows):
                        body = '| ' + ' | '.join(values) + ' |\n'
                        if row_index == 0:
                            body += '| ' + ' | '.join('---' for _ in values) + ' |\n'
                        add(body, {**locator,'row':row_index+1}, kind='table')
    elif ext == '.pdf':
        import fitz
        from app.services.pdf_pipeline import parse_page
        if parser.settings.PDF_PARSER_MODE not in ('legacy','enhanced'):
            raise ValueError('PDF_PARSER_MODE_INVALID')
        with fitz.open(str(path)) as doc:
            if doc.needs_pass:
                raise ValueError('PDF_PASSWORD_REQUIRED')
            for page in doc:
                page_number = page.number + 1
                if parser.settings.PDF_PARSER_MODE == 'enhanced':
                    parsed = parse_page(page, source_sha256=sha,
                        tessdata=parser.settings.PDF_TESSDATA_DIR, dpi=parser.settings.PDF_OCR_DPI,
                        scan_tables=parser.settings.PDF_SCANNED_TABLES_ENABLED,
                        tables_enabled=parser.settings.PDF_TABLES_ENABLED)
                    pages.append(parsed.record)
                    events = [(p.locator.get('bbox',p.locator.get('table_bbox'))[1],0,p) for p in parsed.pieces]
                    events += [(image['bbox'][1],1,image) for image in parsed.images]
                    for _, is_image, piece in sorted(events,key=lambda item:item[:2]):
                        if not is_image:
                            loc = {**piece.locator,'kind':'pdf-table-row' if piece.kind=='table' else 'pdf-text'}
                            if piece.kind != 'table':
                                loc['regions'] = [dict(piece.locator)]
                            add(piece.content, loc, page=page_number, kind='table' if piece.kind=='table' else 'text')
                        else:
                            _pdf_image(parser,blocks,piece,sha,page_number)
                else:
                    for index, block in enumerate(page.get_text('dict',sort=True)['blocks']):
                        if block['type'] == 0:
                            body = '\n'.join(''.join(span['text'] for span in line['spans']) for line in block['lines'])
                            add(body,{'kind':'pdf-text','page':page_number,'bbox':list(block['bbox']),
                                      'method':'native','block_index':index},page=page_number)
                        elif block['type'] == 1:
                            _pdf_image(parser,blocks,{**block,'_source_block_index':index},sha,page_number)
                    pages.append({'page':page_number,'route':'native','status':'completed'})
        parser.pdf_page_records = pages
        if not blocks or (pages and all(p['status']=='failed' for p in pages)):
            from app.services.pdf_provenance import write_manifest
            parser.pdf_manifest_path = write_manifest(parser.settings.PDF_AUDIT_DIR,sha,pages,[],flat=parser.audit_flat)
            raise ValueError('PDF_NO_SEARCHABLE_CONTENT: see page diagnostics')
        if any(p['status'] in ('failed','partial') for p in pages):
            from app.services.pdf_provenance import write_manifest
            parser.pdf_manifest_path = write_manifest(parser.settings.PDF_AUDIT_DIR,sha,pages,blocks,flat=parser.audit_flat)
            raise ValueError('PDF_PARTIAL_PARSE: see page diagnostics')
    elif ext in ('.jpg','.jpeg','.png','.bmp','.webp'):
        blocks = parser._parse_image_file(str(path))
    else:
        raise ValueError(f'不支持的文件格式: {ext}')
    return ParsedDocument(blocks, sha, pages)


def _pdf_image(parser, blocks, image, sha, page):
    from app.services.parser import ParsedChunk
    raw = image['image']
    box = list(image['bbox'])
    width,height = box[2]-box[0],box[3]-box[1]
    if len(raw)<3072 or width<=0 or height<=0 or not .02<=width/height<=50:
        return
    path = parser._save_image(raw,sha[:16],page,image['_source_block_index'],image.get('ext','png'))
    blocks.append(ParsedChunk(f'[图片: {path}]',page,'image',path,{
        'source_locator':{'kind':'pdf-image','source_sha256':sha,'page':page,'bbox':box}}))
