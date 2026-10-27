"""Page routing, local Tesseract OCR and source-located PDF table evidence.

OCR parameters originate from the old project's PdfParser; this module has no
runtime dependency on that project. Model/retrieval configuration is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import math
import re
from pathlib import Path

import fitz

LOGGER = logging.getLogger(__name__)
PARSER_VERSION = 'pdf-page-router/v1'
COORDINATES = 'PDF points; unrotated page; top-left; page 1-based'


@dataclass
class PagePiece:
    content: str
    kind: str
    locator: dict


@dataclass
class PageResult:
    record: dict
    pieces: list[PagePiece] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)


def image_coverage(page):
    """Union area avoids double counting repeated/overlapping image placements."""
    boundary = page.rect * page.derotation_matrix
    rects = [fitz.Rect(i['bbox']) & boundary for i in page.get_image_info()]
    rects = [r for r in rects if not r.is_empty and not r.is_infinite]
    xs = sorted({x for r in rects for x in (r.x0, r.x1)})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((r.y0, r.y1) for r in rects if r.x0 < right and r.x1 > left)
        end = -math.inf
        height = 0.0
        for lo, hi in intervals:
            height += max(0, hi - max(lo, end))
            end = max(end, hi)
        area += (right-left)*height
    return min(1.0, area / boundary.get_area()) if boundary.get_area() else 0.0


def classify_page(text_chars, coverage):
    # Retain a usable native text layer on scanned/OCRed or large-figure pages.
    # The old rule coverage>=0.5 alone can unnecessarily replace good text.
    if text_chars < 10 and coverage >= .1:
        return 'ocr'
    if text_chars < 80 and coverage >= .5:
        return 'ocr'
    if text_chars >= 10 and coverage >= .1:
        return 'mixed'
    return 'native'


def local_ocr(page, tessdata, *, dpi=150, full=True):
    directory = Path(tessdata)
    if not all((directory / f'{name}.traineddata').is_file() for name in ('eng', 'chi_sim')):
        return None, 'OCR_UNAVAILABLE'
    try:
        tp = page.get_textpage_ocr(language='eng+chi_sim', dpi=dpi, full=full,
                                   tessdata=str(directory.resolve()))
        if not page.get_text(textpage=tp).strip():
            return None, 'OCR_EMPTY'
        return tp, None
    except Exception as exc:
        LOGGER.warning('PDF OCR failed: %s', type(exc).__name__)
        return None, 'OCR_FAILED'


def _table_rows(rows, boxes, bbox, page_no, table_id, *, method):
    """A whole source row is one evidence unit; coordinates are never invented."""
    pieces = []
    if not rows or len(rows) > 1000 or max(map(len, rows), default=0) > 200:
        raise ValueError('PDF_TABLE_LIMIT')
    first_row = [str(v or '').strip() for v in rows[0]]
    has_header = any(re.search(r'[A-Za-z\u3400-\u9fff]', v) for v in first_row)
    headers = first_row if has_header else ['C'+str(i+1) for i in range(len(first_row))]
    for r, row in enumerate(rows):
        values = [str(v or '').strip() for v in row]
        if not any(values):
            continue
        content = ' | '.join(f'{headers[c] or "C"+str(c+1)}={v}'
                             for c, v in enumerate(values))
        cells = [{"row": r+1, "column": c+1, "text": v,
                  "bbox": boxes[r][c] if r < len(boxes) and c < len(boxes[r]) else None}
                 for c, v in enumerate(values)]
        pieces.append(PagePiece(content, 'table', dict(
            page=page_no, table_id=table_id, table_bbox=list(bbox), row=r+1,
            cells=cells, method=method, header_status='FIRST_ROW_INFERRED' if has_header else 'NONE',
            coordinate_basis=COORDINATES)))
    return pieces


def native_tables(page, page_no):
    """Conservative native grid admission; unconfirmed layouts retain raw text."""
    if page.rotation:
        return [], [], ['PDF_TABLE_ROTATION_UNSUPPORTED']
    try:
        tables = page.find_tables(strategy='lines').tables
        if not tables:
            # Borderless text grouping is diagnostic only, not asserted cell evidence.
            guesses = page.find_tables(strategy='text').tables
            return [], [], ['PDF_BORDERLESS_TABLE_UNCONFIRMED'] if guesses else []
        blocks = page.get_text('blocks')
        pieces, admitted, warnings = [], [], []
        for index, table in enumerate(tables):
            box = fitz.Rect(table.bbox)
            if any(box.intersects(fitz.Rect(b[:4])) and not box.contains(fitz.Rect(b[:4]))
                   for b in blocks if b[6] == 0):
                warnings.append('PDF_TABLE_TEXT_OVERLAP_UNCONFIRMED')
                continue
            if any(box.intersects(fitz.Rect(other.bbox)) for other in tables if other is not table):
                warnings.append('PDF_TABLE_OVERLAP_UNCONFIRMED')
                continue
            rows = table.extract()
            # Chart value grids often omit their labels outside the detected box.
            # Do not replace those native blocks with falsely labelled table rows.
            if not rows or not any(re.search(r'[A-Za-z\u3400-\u9fff]', str(v or '')) for v in rows[0]):
                warnings.append('PDF_NUMERIC_GRID_LABELS_UNCONFIRMED')
                continue
            boxes = [[list(c) if c else None for c in row.cells] for row in table.rows]
            if any(cell is None for row in boxes for cell in row):
                warnings.append('PDF_TABLE_MERGED_CELLS_UNCONFIRMED')
                continue
            mapped = _table_rows(rows, boxes, table.bbox, page_no,
                                 f'p{page_no}-native-t{index}', method='pymupdf-lines')
            if mapped:
                pieces.extend(mapped)
                admitted.append(box)
        return pieces, admitted, warnings
    except Exception as exc:
        LOGGER.warning('PDF table extraction failed: %s', type(exc).__name__)
        return [], [], ['PDF_TABLE_EXTRACTION_FAILED']


def scanned_tables(page, textpage, page_no):
    """Separate bounded raster-grid path. Never infer borderless/merged cells.

    Raster rules identify a candidate grid; only existing OCR words are assigned
    to cells. It does not claim semantic headers or accurate numeric recognition.
    """
    import numpy as np
    if page.rotation:
        return [], [], ['SCAN_TABLE_ROTATION_UNSUPPORTED']
    if page.rect.width * page.rect.height * 2.25 > 12_000_000:
        return [], [], ['SCAN_TABLE_RASTER_LIMIT']
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), colorspace=fitz.csGRAY, alpha=False)
    dark = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 100
    def centers(indices):
        groups = []
        for i in indices:
            if not groups or i > groups[-1][-1]+2:
                groups.append([])
            groups[-1].append(int(i))
        return [sum(g)/len(g) for g in groups]
    ys = centers(np.flatnonzero(dark.sum(axis=1) > pix.width*.3))
    xs = centers(np.flatnonzero(dark.sum(axis=0) > pix.height*.15))
    if not (3 <= len(xs) <= 31 and 3 <= len(ys) <= 101):
        return [], [], ['SCAN_TABLE_GRID_UNCONFIRMED']
    # Require continuous lines across the proposed grid, not aligned letters.
    if any(dark[max(0,int(y)-2):int(y)+3, int(xs[0]):int(xs[-1])+1].any(axis=0).mean() < .95 for y in ys):
        return [], [], ['SCAN_TABLE_GRID_UNCONFIRMED']
    if any(dark[int(ys[0]):int(ys[-1])+1, max(0,int(x)-2):int(x)+3].any(axis=1).mean() < .95 for x in xs):
        return [], [], ['SCAN_TABLE_GRID_UNCONFIRMED']
    sx, sy = page.rect.width/pix.width, page.rect.height/pix.height
    xs = [page.rect.x0+x*sx for x in xs]
    ys = [page.rect.y0+y*sy for y in ys]
    boxes = [[[xs[c], ys[r], xs[c+1], ys[r+1]] for c in range(len(xs)-1)] for r in range(len(ys)-1)]
    rows = [['' for _ in row] for row in boxes]
    words = page.get_text('words', textpage=textpage, sort=True)
    for word in words:
        cx, cy = (word[0]+word[2])/2, (word[1]+word[3])/2
        for r, row in enumerate(boxes):
            for c, box in enumerate(row):
                if box[0] <= cx < box[2] and box[1] <= cy < box[3]:
                    rows[r][c] += (' ' if rows[r][c] else '') + word[4]
    if sum(bool(v) for row in rows for v in row) < 3:
        return [], [], ['SCAN_TABLE_OCR_CELLS_EMPTY']
    box = [xs[0], ys[0], xs[-1], ys[-1]]
    return _table_rows(rows, boxes, box, page_no, f'p{page_no}-scan-t0',
                       method='raster-grid+local-ocr'), [fitz.Rect(box)], ['SCAN_TABLE_OCR_UNVERIFIED']


def parse_page(page, *, source_sha256, tessdata, dpi=150, scan_tables=False):
    native = page.get_text('text').strip()
    coverage = image_coverage(page)
    route = classify_page(len(native), coverage)
    record = dict(parser_version=PARSER_VERSION, pymupdf_version=fitz.VersionBind,
                  source_sha256=source_sha256, page=page.number+1,
                  text_chars=len(native), image_coverage=coverage, route=route,
                  status='ready', warnings=[], ocr_dpi=dpi if route in ('ocr', 'mixed') else None)
    tp = None
    if route in ('ocr', 'mixed'):
        tp, error = local_ocr(page, tessdata, dpi=dpi, full=route == 'ocr')
        if error:
            record.update(status='partial' if native else 'failed', route='native_fallback' if native else 'ocr_failed')
            record['warnings'].append(error)
    result = PageResult(record)
    tables, table_boxes, warnings = ([], [], [])
    if tp is not None and route == 'ocr':
        if scan_tables:
            tables, table_boxes, warnings = scanned_tables(page, tp, page.number+1)
        else:
            warnings = ['SCAN_TABLE_STRUCTURE_NOT_ENABLED']
    else:
        tables, table_boxes, warnings = native_tables(page, page.number+1)
    record['warnings'].extend(warnings)
    record['table_rows'] = len(tables)
    if record['status'] == 'failed':
        LOGGER.info('PDF_PAGE %s', json.dumps(record, ensure_ascii=False))
        return result
    data = page.get_text('dict', textpage=tp, sort=True) if tp else page.get_text('dict', sort=True)
    if tp is not None:
        result.images = [{**b, '_source_block_index': index}
                         for index,b in enumerate(page.get_text('dict', sort=True)['blocks']) if b['type'] == 1]
    events = [(fitz.Rect(p.locator['table_bbox']).y0, fitz.Rect(p.locator['table_bbox']).x0, 0, p) for p in tables]
    for block_index, block in enumerate(data['blocks']):
        bbox = fitz.Rect(block['bbox'])
        if block['type'] == 0:
            # Avoid deleting prose that crosses a raster table boundary.
            if table_boxes and any(box.contains(bbox) for box in table_boxes):
                continue
            if tp and table_boxes and any(box.intersects(bbox) for box in table_boxes):
                # Keep OCR block on ambiguous overlap, with an explicit duplication warning.
                record['warnings'].append('SCAN_TABLE_TEXT_OVERLAP_RETAINED')
            text = '\n'.join(''.join(span['text'] for span in line['spans']) for line in block['lines']).strip()
            if text:
                locator = dict(page=page.number+1, bbox=list(bbox), method='tesseract' if tp else 'native-text',
                               coordinate_basis=COORDINATES)
                if tp is not None and route == 'mixed':
                    locator['method'] = 'native+image-ocr'
                events.append((bbox.y0, bbox.x0, 1, PagePiece(text, 'text', locator)))
        elif block['type'] == 1 and tp is None and route != 'mixed':
            result.images.append({**block, '_source_block_index': block_index})
    result.pieces = [v[3] for v in sorted(events, key=lambda v: v[:3])]
    record['extracted_chars'] = sum(len(p.content) for p in result.pieces)
    if not result.pieces and not result.images:
        record['status'] = 'empty' if not native and coverage == 0 else 'failed'
        if record['status'] == 'failed':
            record['warnings'].append('PDF_PAGE_NO_SEARCHABLE_CONTENT')
    LOGGER.info('PDF_PAGE %s', json.dumps(record, ensure_ascii=False))
    return result
