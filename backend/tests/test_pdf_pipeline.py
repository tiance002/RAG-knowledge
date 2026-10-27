import hashlib
import json
from pathlib import Path

import fitz
import pytest

from app.services.pdf_pipeline import classify_page, image_coverage, parse_page
from app.services.pdf_provenance import source_metadata
from app.services import parser as parser_module

TESSDATA = Path(__file__).resolve().parents[2] / 'var/tessdata'


def make_pdf(path, *, scan=False, table=False, mixed=False):
    source = fitz.open()
    page = source.new_page(width=500, height=650)
    if table:
        for x in (40, 250, 460):
            page.draw_line((x, 60), (x, 360), width=2)
        for y in (60, 160, 260, 360):
            page.draw_line((40, y), (460, y), width=2)
        for x, y, value in [(60, 110, 'Product'), (270, 110, 'Revenue'),
                            (60, 210, 'ALPHA'), (270, 210, '12345'),
                            (60, 310, 'BETA'), (270, 310, '67890')]:
            page.insert_text((x, y), value, fontsize=22)
    else:
        page.insert_text((45, 90), 'The scanned access code is ORANGE 7319.', fontsize=20)
    if not scan:
        source.save(path)
        source.close()
        return
    image = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes('png')
    source.close()
    output = fitz.open()
    target = output.new_page(width=500, height=650)
    target.insert_image(target.rect, stream=image)
    if mixed:
        native = output.new_page(width=500, height=650)
        native.insert_text((45, 90), 'A separate native PDF page with code BLUE 4921.', fontsize=16)
    output.save(path)
    output.close()


@pytest.fixture
def parser(tmp_path, monkeypatch):
    settings = parser_module.settings
    monkeypatch.setattr(settings, 'IMAGE_DIR', str(tmp_path/'images'))
    monkeypatch.setattr(settings, 'PDF_AUDIT_DIR', str(tmp_path/'manifests'))
    monkeypatch.setattr(settings, 'PDF_TESSDATA_DIR', str(TESSDATA))
    monkeypatch.setattr(settings, 'PDF_PARSER_MODE', 'enhanced')
    monkeypatch.setattr(settings, 'PDF_SCANNED_TABLES_ENABLED', False)
    return parser_module.FileParser('test-kb')


def require_ocr():
    if not all((TESSDATA/f'{name}.traineddata').is_file() for name in ('eng', 'chi_sim')):
        pytest.skip('Real Tesseract language files not present')


def test_classifier_preserves_usable_text():
    assert classify_page(0, 1) == 'ocr'
    assert classify_page(6, .2) == 'ocr'
    assert classify_page(200, .9) == 'mixed'
    assert classify_page(0, 0) == 'native'


def test_coverage_union_not_sum():
    doc = fitz.open()
    page = doc.new_page(width=100, height=100)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10))
    pix.clear_with(255)
    raw = pix.tobytes('png')
    for box in ([0, 0, 60, 100], [40, 0, 100, 100]):
        page.insert_image(box, stream=raw, keep_proportion=False)
    assert image_coverage(page) == pytest.approx(1)
    doc.close()


def test_native_table_rows_and_cell_locations(tmp_path, parser):
    path = tmp_path/'native.pdf'
    make_pdf(path, table=True)
    chunks = parser.parse(str(path))
    rows = [c for c in chunks if c.content_type == 'table']
    assert any('ALPHA' in c.content and '12345' in c.content for c in rows)
    row = next(c for c in rows if 'ALPHA' in c.content)
    assert row.metadata['source_locator']['cells'][0]['bbox']
    assert row.metadata['source_locator']['row'] == 2
    assert parser.pdf_page_records[0]['route'] == 'native'
    assert sum('12345' in c.content for c in chunks) == 1


def test_real_scan_ocr_enters_text_and_manifest(tmp_path, parser):
    require_ocr()
    path = tmp_path/'scan.pdf'
    make_pdf(path, scan=True)
    chunks = parser.parse(str(path))
    assert any('7319' in c.content for c in chunks if c.content_type == 'text')
    assert parser.pdf_page_records[0]['route'] == 'ocr'
    assert parser.pdf_page_records[0]['text_chars'] == 0
    assert parser.pdf_page_records[0]['image_coverage'] == pytest.approx(1)
    chunk = next(c for c in chunks if '7319' in c.content)
    metadata = source_metadata(path, chunk.content, chunk.page_number, chunk.content_type,
                               root=parser_module.settings.PDF_AUDIT_DIR)
    assert metadata['source_locators'][0]['regions'][0]['method'] == 'tesseract'
    assert metadata['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not source_metadata(path, 'edited '+chunk.content, chunk.page_number, chunk.content_type,
                               root=parser_module.settings.PDF_AUDIT_DIR)


def test_missing_ocr_fails_with_diagnostics(tmp_path, parser, monkeypatch):
    path = tmp_path/'scan.pdf'
    make_pdf(path, scan=True)
    monkeypatch.setattr(parser_module.settings, 'PDF_TESSDATA_DIR', str(tmp_path/'missing'))
    with pytest.raises(ValueError, match='PDF_NO_SEARCHABLE_CONTENT'):
        parser.parse(str(path))
    record = json.loads(Path(parser.pdf_manifest_path).read_text())['pages'][0]
    assert record['status'] == 'failed'
    assert 'OCR_UNAVAILABLE' in record['warnings']


def test_mixed_document_keeps_page_numbers(tmp_path, parser):
    require_ocr()
    path = tmp_path/'mixed.pdf'
    make_pdf(path, scan=True, mixed=True)
    chunks = parser.parse(str(path))
    assert next(c for c in chunks if '7319' in c.content).page_number == 1
    assert next(c for c in chunks if '4921' in c.content).page_number == 2
    assert [p['route'] for p in parser.pdf_page_records] == ['ocr', 'native']


def test_no_split_retains_cross_page_source_regions(tmp_path, parser):
    require_ocr()
    path = tmp_path/'mixed.pdf'
    make_pdf(path, scan=True, mixed=True)
    chunks = parser.parse(str(path), chunk_mode='no_split')
    text = [c for c in chunks if c.content_type == 'text']
    assert len(text) == 1
    assert text[0].metadata['source_locator']['pages'] == [1, 2]


def test_scanned_grid_is_separate_opt_in(tmp_path, parser, monkeypatch):
    require_ocr()
    path = tmp_path/'scan-table.pdf'
    make_pdf(path, scan=True, table=True)
    initial = parser.parse(str(path))
    assert not any(c.content_type == 'table' for c in initial)
    monkeypatch.setattr(parser_module.settings, 'PDF_SCANNED_TABLES_ENABLED', True)
    chunks = parser.parse(str(path))
    rows = [c for c in chunks if c.content_type == 'table']
    assert any('ALPHA' in c.content and '12345' in c.content for c in rows)
    assert rows[0].metadata['source_locator']['method'] == 'raster-grid+local-ocr'
    assert 'SCAN_TABLE_OCR_UNVERIFIED' in parser.pdf_page_records[0]['warnings']


def test_manifest_tampering_detected(tmp_path, parser):
    path = tmp_path/'native.pdf'
    make_pdf(path)
    chunks = parser.parse(str(path))
    manifest = Path(parser.pdf_manifest_path)
    manifest.write_text(manifest.read_text()+' ')
    with pytest.raises(ValueError, match='PDF_MANIFEST_HASH_MISMATCH'):
        source_metadata(path, chunks[0].content, 1, 'text', root=parser_module.settings.PDF_AUDIT_DIR)


def test_legacy_mode_retains_existing_parser(tmp_path, parser, monkeypatch):
    path = tmp_path/'native.pdf'
    make_pdf(path)
    monkeypatch.setattr(parser_module.settings, 'PDF_PARSER_MODE', 'legacy')
    assert any('7319' in c.content for c in parser.parse(str(path)))


def test_mixed_missing_ocr_cannot_silently_succeed(tmp_path, parser, monkeypatch):
    path = tmp_path/'mixed.pdf'
    make_pdf(path, scan=True, mixed=True)
    monkeypatch.setattr(parser_module.settings, 'PDF_TESSDATA_DIR', str(tmp_path/'missing'))
    with pytest.raises(ValueError, match='PDF_PARTIAL_PARSE'):
        parser.parse(str(path))


def test_numeric_chart_grid_keeps_native_labels(tmp_path, parser):
    path = tmp_path/'numeric-chart.pdf'
    doc=fitz.open()
    page=doc.new_page(width=500,height=650)
    for x in (150,300,450):page.draw_line((x,60),(x,360),width=2)
    for y in (60,160,260,360):page.draw_line((150,y),(450,y),width=2)
    page.insert_text((40,410),'Category labels: All voters; Men; Women',fontsize=15)
    for y,a,b in [(110,'17','79'),(210,'16','81'),(310,'19','78')]:
        page.insert_text((180,y),a,fontsize=15)
        page.insert_text((330,y),b,fontsize=15)
    doc.save(path);doc.close()
    chunks=parser.parse(str(path))
    assert not any(c.content_type=='table' for c in chunks)
    assert any('Men' in c.content and '81' in c.content for c in chunks)
    assert 'PDF_NUMERIC_GRID_LABELS_UNCONFIRMED' in parser.pdf_page_records[0]['warnings']


def test_image_paths_do_not_depend_on_text_chunk_count(tmp_path, parser):
    import numpy as np
    path=tmp_path/'placements.pdf'
    doc=fitz.open();page=doc.new_page(width=500,height=650)
    page.insert_textbox((40,40,460,230),'alpha evidence '*70,fontsize=10)
    for index,y in enumerate((250,420)):
        pixels=np.random.default_rng(index).integers(0,256,(100,100,3),dtype=np.uint8)
        pix=fitz.Pixmap(fitz.csRGB,100,100,pixels.tobytes(),False)
        page.insert_image((40,y,100,y+60),stream=pix.tobytes('png'))
    doc.save(path);doc.close()
    def images(size):
        chunks=parser.parse(str(path),chunk_mode='custom',chunk_size=size,chunk_overlap=5)
        return {c.image_path:hashlib.sha256((Path(parser.image_dir).parent/c.image_path).read_bytes()).hexdigest()
                for c in chunks if c.content_type=='image'}
    assert images(50)==images(500)


def test_ocr_sqlite_bm25_and_provenance_round_trip(tmp_path, parser):
    require_ocr()
    from sqlmodel import SQLModel, Session, create_engine
    from app.models import KnowledgeBase, FileDocument, DocumentChunk, ContentType
    from app.services.bm25 import BM25Index
    path=tmp_path/'indexed-scan.pdf'
    make_pdf(path,scan=True)
    chunks=parser.parse(str(path))
    engine=create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb',name='test',embedding_model='unused',vlm_model='unused'))
        session.add(FileDocument(id='file',kb_id='kb',name=path.name,local_path=str(path),size=path.stat().st_size))
        session.commit()
        index=BM25Index()
        for i,c in enumerate(chunks):
            record=DocumentChunk(id=str(i),file_id='file',content=c.content,page_number=c.page_number,
                                 content_type=ContentType(c.content_type))
            session.add(record);session.commit()
            index.index_chunk(session,record.id,record.content,'kb')
        session.commit()
        hits=index.search(session,'7319','kb',top_k=10)
        assert hits
        record=session.get(DocumentChunk,hits[0]['chunk_id'])
        metadata=source_metadata(path,record.content,record.page_number,record.content_type,
                                 root=parser_module.settings.PDF_AUDIT_DIR)
        assert metadata['source_locators'][0]['page']==1
