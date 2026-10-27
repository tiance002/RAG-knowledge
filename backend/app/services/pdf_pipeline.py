"""Page routing, local Tesseract OCR and source-located PDF table evidence.

OCR parameters originate from the old project's PdfParser; this module has no
runtime dependency on that project. Model/retrieval configuration is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import hashlib
import math
import re
from pathlib import Path

import fitz

LOGGER = logging.getLogger(__name__)
PARSER_VERSION = 'pdf-page-router/v2.1'
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


def _table_rows(rows, boxes, bbox, page_no, table_id, *, method, has_header=None):
    """A whole source row is one evidence unit; coordinates are never invented."""
    pieces = []
    if not rows or len(rows) > 1000 or max(map(len, rows), default=0) > 200:
        raise ValueError('PDF_TABLE_LIMIT')
    first_row = [str(v or '').strip() for v in rows[0]]
    if has_header is None:
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


def _ruled_borderless_tables(page, page_no):
    """Admit aligned native cells between two real horizontal rules.

    This is deliberately narrower than arbitrary text-strategy guesses: require
    four aligned numeric data rows, a bold multi-column header and no intersecting
    prose blocks. Cell bboxes are native text extents, not invented ruling lines.
    """
    rules = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing['rect'])
        if rect.height <= 2 and rect.width >= 8:
            y = (rect.y0+rect.y1)/2
            group = next((r for r in rules if abs(r[0]-y) <= 2), None)
            if group is None:
                rules.append([y, [(rect.x0, rect.x1)]])
            else:
                group[1].append((rect.x0, rect.x1))
    merged = []
    for y, intervals in rules:
        spans = []
        for left, right in sorted(intervals):
            if spans and left <= spans[-1][1]+2:
                spans[-1][1] = max(right, spans[-1][1])
            else:
                spans.append([left, right])
        merged.extend((y, a, b) for a,b in spans if b-a >= page.rect.width*.5)
    result, admitted = [], []
    for top, left, right in sorted(merged):
        bottoms = [(y,a,b) for y,a,b in merged if 30 <= y-top <= page.rect.height*.6
                   and abs(a-left) <= 4 and abs(b-right) <= 4]
        if not bottoms:
            continue
        bottom = min(bottoms)[0]
        box = fitz.Rect(left-1, top, right+1, bottom+1)
        if any(box.intersects(other) for other in admitted):
            continue
        native_blocks = page.get_text('blocks')
        if any(box.intersects(fitz.Rect(b[:4])) and not box.contains(fitz.Rect(b[:4]))
               for b in native_blocks if b[6] == 0):
            continue
        spans = [s for block in page.get_text('dict')['blocks'] if block['type'] == 0
                 for line in block['lines'] for s in line['spans']
                 if s['text'].strip() and box.contains(fitz.Rect(s['bbox']))]
        lines = []
        for span in sorted(spans, key=lambda s:(s['bbox'][1],s['bbox'][0])):
            y = (span['bbox'][1]+span['bbox'][3])/2
            line = next((v for v in lines if abs(v[0]-y) <= 2), None)
            if line is None:
                lines.append([y,[span]])
            else:
                line[1].append(span)
        # Merge adjacent spans inside a source cell, keeping the original spans.
        grouped = []
        for y, line in sorted(lines):
            cells = []
            for s in sorted(line,key=lambda s:s['bbox'][0]):
                r = fitz.Rect(s['bbox'])
                if cells and r.x0-cells[-1]['bbox'].x1 <= max(3,s['size']*.5):
                    cells[-1]['text'] += s['text']
                    cells[-1]['bbox'] |= r
                    cells[-1]['spans'].append(s)
                else:
                    cells.append({'text':s['text'],'bbox':r,'spans':[s]})
            grouped.append((y,cells))
        def data_row(cells):
            return (3 <= len(cells) <= 12 and bool(re.search(r'[A-Za-z\u3400-\u9fff]',cells[0]['text']))
                    and sum(bool(re.search(r'\d',c['text'])) for c in cells[1:]) >= len(cells)-2)
        runs, run = [], []
        for y,cells in grouped:
            if data_row(cells) and (not run or len(cells)==len(run[-1][1])):
                run.append((y,cells))
            else:
                if run:runs.append(run)
                run = [(y,cells)] if data_row(cells) else []
        if run:runs.append(run)
        data = max(runs,key=len,default=[])
        if len(data) < 4:
            continue
        columns = len(data[0][1])
        gutters = []
        for c in range(columns-1):
            a = max(row[c]['bbox'].x1 for _,row in data)
            b = min(row[c+1]['bbox'].x0 for _,row in data)
            if b-a < 5:break
            gutters.append((a+b)/2)
        if len(gutters)!=columns-1:
            continue
        bounds = [box.x0]+gutters+[box.x1]
        header_cells = [[] for _ in range(columns)]
        used = set(id(s) for _,row in data for c in row for s in c['spans'])
        first_y=data[0][0]
        for s in spans:
            cy=(s['bbox'][1]+s['bbox'][3])/2
            bold = bool(s['flags'] & 16) or bool(re.search('bold|demi|black|heavy',s['font'],re.I))
            if not bold or not 0 < first_y-cy <= s['size']*3:
                continue
            cx=(s['bbox'][0]+s['bbox'][2])/2
            for c in range(columns):
                if bounds[c] <= cx < bounds[c+1]:header_cells[c].append(s);used.add(id(s));break
        if not all(header_cells):
            continue
        headers = [' '.join(s['text'].strip() for s in sorted(cell,key=lambda s:(s['bbox'][1],s['bbox'][0])))
                   for cell in header_cells]
        if not all(re.search(r'[A-Za-z\u3400-\u9fff]',h) for h in headers):
            continue
        def union(cell):
            rect=fitz.Rect(cell[0]['bbox'])
            for s in cell[1:]:rect |= fitz.Rect(s['bbox'])
            return list(rect)
        rows=[headers]+[[c['text'].strip() for c in row] for _,row in data]
        boxes=[[union(cell) for cell in header_cells]]+[[list(c['bbox']) for c in row] for _,row in data]
        mapped=_table_rows(rows,boxes,box,page_no,f'p{page_no}-aligned-t{len(admitted)}',method='native-ruled-aligned-text')
        for piece in mapped:
            piece.locator.update(header_status='ALIGNED_BOLD_TEXT_INFERRED',geometry_status='NATIVE_TEXT_EXTENTS')
        # Preserve captions or unmatched native text rather than silently deleting them.
        for s in spans:
            if id(s) not in used:
                mapped.append(PagePiece(s['text'].strip(),'text',dict(page=page_no,bbox=list(s['bbox']),
                    table_bbox=list(box),method='native-text',coordinate_basis=COORDINATES)))
        result.extend(mapped);admitted.append(box)
    return result, admitted


def native_tables(page, page_no):
    """Conservative native grid admission; unconfirmed layouts retain raw text."""
    if page.rotation:
        return [], [], ['PDF_TABLE_ROTATION_UNSUPPORTED']
    try:
        tables = page.find_tables(strategy='lines').tables
        if not tables:
            mapped, boxes = _ruled_borderless_tables(page,page_no)
            if mapped:
                return mapped,boxes,['PDF_BORDERLESS_HEADER_INFERRED']
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


def _raster_grid(gray):
    """Locate continuous horizontal/vertical boundaries, including colored grids."""
    import numpy as np
    height,width=gray.shape
    # Retain the validated dark-grid path for monochrome scanned documents.
    dark=gray<100
    def dark_centers(indices):
        groups=[]
        for i in indices:
            if not groups or i>groups[-1][-1]+2:groups.append([])
            groups[-1].append(int(i))
        return [sum(g)/len(g) for g in groups]
    old_ys=dark_centers(np.flatnonzero(dark.sum(axis=1)>width*.3))
    old_xs=dark_centers(np.flatnonzero(dark.sum(axis=0)>height*.15))
    if 3<=len(old_xs)<=31 and 3<=len(old_ys)<=101:
        horizontal_ok=all(dark[max(0,int(y)-2):int(y)+3,int(old_xs[0]):int(old_xs[-1])+1].any(axis=0).mean()>=.95 for y in old_ys)
        vertical_ok=all(dark[int(old_ys[0]):int(old_ys[-1])+1,max(0,int(x)-2):int(x)+3].any(axis=1).mean()>=.95 for x in old_xs)
        if horizontal_ok and vertical_ok:return old_xs,old_ys
    values=gray.astype(np.int16)
    horizontal=np.zeros_like(gray,dtype=bool)
    vertical=np.zeros_like(gray,dtype=bool)
    horizontal[1:]=np.abs(np.diff(values,axis=0))>12
    vertical[:,1:]=np.abs(np.diff(values,axis=1))>12
    # Antialiasing and line intersections may shift an edge by a pixel.
    h=horizontal.copy();h[1:] |= horizontal[:-1];h[:-1] |= horizontal[1:]
    v=vertical.copy();v[:,1:] |= vertical[:,:-1];v[:,:-1] |= vertical[:,1:]
    # A vertical rule of the same color interrupts a horizontal edge by 1-3px.
    for gap in (1,2,3):
        anchors=h[:,:-gap-1] & h[:,gap+1:]
        for offset in range(1,gap+1):
            h[:,offset:offset+anchors.shape[1]] |= anchors
    def centers(indices):
        groups=[]
        for i in indices:
            if not groups or i>groups[-1][-1]+3:groups.append([])
            groups[-1].append(int(i))
        return [sum(g)/len(g) for g in groups]
    groups=[]
    for y in np.flatnonzero(h.sum(axis=1)>width*.3):
        boundaries=np.flatnonzero(np.diff(np.r_[False,h[y],False].astype(np.int8)))
        for left,right in zip(boundaries[::2],boundaries[1::2]):
            if right-left<width*.3:continue
            group=next((g for g in groups if abs(g['left']-left)<=4 and abs(g['right']-right)<=4),None)
            if group is None:
                groups.append({'left':int(left),'right':int(right),'ys':[int(y)]})
            else:group['ys'].append(int(y))
    candidates=[]
    for group in groups:
        ys=centers(sorted(set(group['ys'])))
        if not 3<=len(ys)<=101:continue
        left,right=max(0,group['left']-4),min(width,group['right']+4)
        top,bottom=int(ys[0]),int(ys[-1])+1
        if bottom-top<height*.1:continue
        xs=centers(np.flatnonzero(v[top:bottom,left:right].mean(axis=0)>.9)+left)
        if not 3<=len(xs)<=31:continue
        if abs(xs[0]-group['left'])>5 or abs(xs[-1]-group['right'])>5:continue
        candidates.append((xs,ys))
    return max(candidates,key=lambda xy:(len(xy[0])-1)*(len(xy[1])-1),default=([],[]))


def _cell_ocr(page, box, tessdata, dpi):
    """OCR a border-free, padded cell; retain output without lexical correction."""
    import numpy as np
    clip=fitz.Rect(box)+fitz.Rect(2,2,-2,-2)
    if clip.width<8 or clip.height<8:return '', 'CELL_TOO_SMALL'
    pix=page.get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72),clip=clip,
                        colorspace=fitz.csGRAY,alpha=False)
    values=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
    # Flat colored table fills and rules interfere with OCR on whole-page scans.
    # Use local background contrast, retaining original pixels on ambiguous fills.
    ink=np.abs(values.astype(np.int16)-np.median(values))>50
    if .005<ink.mean()<.4:
        pixels=np.where(ink,0,255).astype(np.uint8)
        pix=fitz.Pixmap(fitz.csGRAY,pix.width,pix.height,pixels.tobytes(),False)
    with fitz.open() as crop:
        target=crop.new_page(width=clip.width+12,height=clip.height+12)
        target.insert_image(fitz.Rect(6,6,clip.width+6,clip.height+6),pixmap=pix)
        tp,error=local_ocr(target,tessdata,dpi=dpi)
        raw=target.get_text(textpage=tp).strip() if tp else ''
    return raw,error


def _scan_has_header(rows):
    """Infer only compact column labels over longer data, otherwise use C1/C2.

    Text in the first row alone is not evidence of a header (e.g. glossaries).
    No row is discarded, regardless of the decision.
    """
    first=rows[0]
    if not all(re.search(r'[A-Za-z\u3400-\u9fff]',v) and len(v.split())<=6
               and len(v)<=60 for v in first):return False
    return any(max((len(row[c].split()) for row in rows[1:]),default=0)
               >max(3,len(value.split())*1.5) for c,value in enumerate(first))


def scanned_tables(page, textpage, page_no, *, tessdata=None, dpi=150):
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
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    xs,ys = _raster_grid(gray)
    if not (3 <= len(xs) <= 31 and 4 <= len(ys) <= 101):
        return [], [], ['SCAN_TABLE_GRID_UNCONFIRMED']
    sx, sy = page.rect.width/pix.width, page.rect.height/pix.height
    xs = [page.rect.x0+x*sx for x in xs]
    ys = [page.rect.y0+y*sy for y in ys]
    boxes = [[[xs[c], ys[r], xs[c+1], ys[r+1]] for c in range(len(xs)-1)] for r in range(len(ys)-1)]
    rows = [['' for _ in row] for row in boxes]
    words = page.get_text('words', textpage=textpage, sort=True)
    grid_tokens=[]
    for word in words:
        cx, cy = (word[0]+word[2])/2, (word[1]+word[3])/2
        if word[4]=='|' and any(abs(cx-x)<5 for x in xs):
            grid_tokens.append({'text':word[4],'bbox':list(word[:4])})
            continue
        for r, row in enumerate(boxes):
            for c, box in enumerate(row):
                if box[0] <= cx < box[2] and box[1] <= cy < box[3]:
                    rows[r][c] += (' ' if rows[r][c] else '') + word[4]
    recovered=0
    crop_raw={}
    original_rows=[list(row) for row in rows]
    # An all-cells pass fixes nonempty but corrupted page-OCR text too. Bound
    # work and fail closed to raw page text instead of emitting partial tables.
    if tessdata is None or sum(map(len,rows))>64:
        return [],[],['SCAN_TABLE_CELL_OCR_UNAVAILABLE']
    for r,row in enumerate(rows):
        for c in range(len(row)):
            raw,error=_cell_ocr(page,boxes[r][c],tessdata,max(dpi,300))
            crop_raw[f'R{r+1}C{c+1}']=raw
            if error:return [],[],['SCAN_TABLE_CELL_OCR_FAILED']
            rows[r][c]=' '.join(line.strip() for line in raw.splitlines() if line.strip() not in {'|','_'})
            recovered+=int(rows[r][c]!=original_rows[r][c])
    if sum(bool(v) for row in rows for v in row) < 3:
        return [], [], ['SCAN_TABLE_OCR_CELLS_EMPTY']
    box = [xs[0], ys[0], xs[-1], ys[-1]]
    pieces=_table_rows(rows, boxes, box, page_no, f'p{page_no}-scan-t0',
                       method='raster-grid+local-ocr',has_header=_scan_has_header(rows))
    for piece in pieces:
        piece.locator.update(cell_crop_ocr_recovered=recovered,cell_crop_raw=crop_raw,
                             cell_page_ocr_raw=original_rows,cell_ocr_dpi=max(dpi,300),
                             excluded_grid_tokens=grid_tokens)
    return pieces, [fitz.Rect(box)], ['SCAN_TABLE_OCR_UNVERIFIED']


def parse_page(page, *, source_sha256, tessdata, dpi=150, scan_tables=False, tables_enabled=True):
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
    record['tables_enabled']=tables_enabled
    record['page_text_sha256']=hashlib.sha256(
        (page.get_text(textpage=tp) if tp else native).encode()).hexdigest()
    result = PageResult(record)
    tables, table_boxes, warnings = ([], [], [])
    if not tables_enabled:
        warnings=['PDF_TABLE_STRUCTURE_DISABLED']
    elif tp is not None and route == 'ocr':
        if scan_tables:
            tables, table_boxes, warnings = scanned_tables(page, tp, page.number+1,tessdata=tessdata,dpi=dpi)
        else:
            warnings = ['SCAN_TABLE_STRUCTURE_NOT_ENABLED']
    else:
        tables, table_boxes, warnings = native_tables(page, page.number+1)
    record['warnings'].extend(warnings)
    record['table_rows'] = sum(p.kind=='table' for p in tables)
    if record['status'] == 'failed':
        LOGGER.info('PDF_PAGE %s', json.dumps(record, ensure_ascii=False))
        return result
    data = page.get_text('dict', textpage=tp, sort=True) if tp else page.get_text('dict', sort=True)
    if tp is not None:
        result.images = [{**b, '_source_block_index': index}
                         for index,b in enumerate(page.get_text('dict', sort=True)['blocks']) if b['type'] == 1]
    events = [(fitz.Rect(p.locator.get('bbox',p.locator['table_bbox'])).y0,
               fitz.Rect(p.locator.get('bbox',p.locator['table_bbox'])).x0, 0, p) for p in tables]
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
