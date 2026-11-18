from types import SimpleNamespace
import hashlib

import pytest
import fitz
from docx import Document
from openpyxl import Workbook

from app.services.parser import FileParser, ParsedChunk
from app.services.parsed_document import ParsedDocument
from app.services.document_chunker import ChunkConfig, chunk_document, split_parent_child
from app.services.parent_child import ParentChildConfig, remap_and_validate_graph, supported_child
from app.services.pdf_provenance import source_metadata


@pytest.fixture
def parser(tmp_path):
    return FileParser('unit',parser_settings=SimpleNamespace(
        IMAGE_DIR=str(tmp_path/'images'),PDF_AUDIT_DIR=str(tmp_path/'audit'),PDF_PARSER_MODE='legacy'))


@pytest.mark.parametrize('extension',['txt','md','csv','xlsx','docx','pdf'])
def test_every_supported_text_format_has_one_post_extraction_entry(tmp_path, parser, monkeypatch, extension):
    path=tmp_path/f'source.{extension}'
    if extension=='xlsx':
        book=Workbook();book.active.append(['Name','Count']);book.active.append(['ALPHA',12]);book.save(path)
    elif extension=='docx':
        doc=Document();doc.add_heading('Report',1);doc.add_paragraph('ALPHA count 12');doc.save(path)
    elif extension=='pdf':
        doc=fitz.open();page=doc.new_page();page.insert_text((50,60),'ALPHA count 12');doc.save(path);doc.close()
    elif extension=='csv':
        path.write_text('Name,Count\nALPHA,12\n',encoding='utf8')
    else:
        path.write_text('# Report\nALPHA count 12\n',encoding='utf8')
    import app.services.parser as old
    monkeypatch.setattr(old,'TextSplitter',lambda *a,**kw:pytest.fail('old splitter constructed'))
    monkeypatch.setattr(old,'StructuredSplitter',lambda *a,**kw:pytest.fail('old structured splitter constructed'))
    raw=parser.parse(str(path),chunk_size=128,chunk_overlap=8,defer_chunking=True)
    assert raw and not any((b.metadata or {}).get('chunker_version') for b in raw)
    final=parser.chunk_document(raw)
    assert any('ALPHA' in c.content for c in final)
    for c in final:
        assert c.metadata['chunker_version']=='unified-legacy-v1'
        assert c.metadata['source_locators']
        saved=source_metadata(path,c.content,c.page_number,c.content_type,root=parser.settings.PDF_AUDIT_DIR)
        assert saved['source_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='ALREADY_CHUNKED'):
        parser.chunk_document(final)


def test_parent_child_operates_on_whole_document_not_rows(parser,tmp_path):
    path=tmp_path/'rows.csv';path.write_text('Name,Count\n'+''.join(f'item{i},{i}\n' for i in range(30)))
    raw=parser.parse(str(path),chunk_size=128,chunk_overlap=8,defer_chunking=True)
    assert len(raw)==31
    text,_=parser._document.text_and_spans()
    pc=ParentChildConfig(256,0,100,8)
    expected_parents,expected_children=split_parent_child(text,pc)
    final=parser.chunk_document(raw,parent_child_config=pc)
    assert [c.content for c in final if c.metadata.get('chunk_type')=='parent']==[c.content for c in expected_parents]
    assert [c.content for c in final if c.metadata.get('chunk_type')!='parent']==[c.content for c,_,_,_ in expected_children if c.content.strip()]
    assert all(c.content.strip() for c in final)
    remap_and_validate_graph([dict(content=c.content,**c.metadata) for c in final],'file')


def test_synthetic_header_parent_relationship_and_forgery_rejection():
    text='| Name | Count |\n| --- | --- |\n'+''.join(f'| item{i} | {i} |\n' for i in range(25))
    blocks=[ParsedChunk(text,None,'text',metadata={'source_locator':{'kind':'markdown-table'}})]
    final=chunk_document(ParsedDocument(blocks),ChunkConfig(parent_child=ParentChildConfig(512,0,100,8)))
    parents={c.metadata['id']:c for c in final if c.metadata.get('chunk_type')=='parent'}
    children=[c for c in final if c.metadata.get('chunk_type')=='child']
    assert any(c.content not in parents[c.metadata['parent_id']].content for c in children)
    for c in children:
        assert supported_child(dict(content=c.content,metadata=c.metadata),dict(content=parents[c.metadata['parent_id']].content))
    assert not supported_child(dict(content='forged answer',metadata=children[0].metadata),dict(content=text))


def test_mixed_original_and_generated_caption_keeps_origin_metadata():
    original=ParsedChunk('Original source text.',1,'text',metadata={'source_locator':{'page':1}})
    caption=ParsedChunk('Model generated caption.',1,'text',metadata={
        'source_locator':{'page':1,'text_origin':'model_generated_image_description'},
        'content_origin':'model_generated_image_description','image_description':{'model':'fixture-model'}})
    chunks=chunk_document(ParsedDocument([original,caption]),ChunkConfig())
    assert len(chunks)==1
    assert chunks[0].metadata['contains_original_text']
    assert chunks[0].metadata['content_origin']=='model_generated_image_description'
    assert chunks[0].metadata['image_descriptions'][0]['model']=='fixture-model'


def test_excel_row_terminator_is_distinct_from_trailing_cell_newline(parser,tmp_path):
    path=tmp_path/'newlines.xlsx'
    book=Workbook();book.active.append(['value\n']);book.active.append(['next']);book.save(path)
    raw=parser.parse(str(path),chunk_size=512,chunk_overlap=80,defer_chunking=True)
    text,_=parser._document.text_and_spans()
    assert len(raw)==2
    assert text=='A: value\n\nA: next\n'
