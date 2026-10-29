from types import SimpleNamespace
from unittest.mock import MagicMock

from app.models import DocumentChunk, FileDocument
from app.services.grounded_validation import validate_grounded_answer
from app.services.pdf_table_validation import verified_pdf_rows


def test_table_proof_binds_chunk_file_kb_page_field_and_unit(tmp_path, monkeypatch):
    import fitz
    path = tmp_path / 'source.pdf'
    path.write_bytes(b'fixture')
    table = SimpleNamespace(bbox=(0, 50, 100, 100), extract=lambda: [
        ['项目', '行次', '决算数', '项目', '行次', '决算数'],
        ['本年收入合计', '8', '190,395.32', '本年支出合计', '19', '189,274.27'],
    ])
    page = SimpleNamespace(rect=SimpleNamespace(width=100),
                           find_tables=lambda: SimpleNamespace(tables=[table]),
                           get_textbox=lambda rect: '收入支出决算总表\n单位：万元',
                           get_text=lambda: '审计署2024年度 部门决算')
    pdf = MagicMock()
    pdf.__enter__.return_value = pdf
    pdf.__len__.return_value = 1
    pdf.__getitem__.return_value = page
    monkeypatch.setattr(fitz, 'open', lambda *a, **kw: pdf)
    content = '本年收入合计\n8\n190,395.32\n本年支出合计\n19\n189,274.27'
    chunk = SimpleNamespace(file_id='file', content=content, page_number=1)
    doc = SimpleNamespace(id='file', kb_id='kb', local_path=str(path))
    session = MagicMock()
    session.get.side_effect = lambda model, ident: chunk if model is DocumentChunk and ident == 'chunk' else doc if model is FileDocument and ident == 'file' else None
    citation = {'chunk_id': 'chunk', 'fileId': 'file', 'content': content}
    rows = verified_pdf_rows(session, [citation], ['kb'])
    assert rows[1]['rows'] == [('本年收入合计', '190,395.32', '万元'), ('本年支出合计', '189,274.27', '万元')]
    assert verified_pdf_rows(session, [citation], ['other']) == {}
    assert verified_pdf_rows(session, [{**citation, 'content': content + 'changed'}], ['kb']) == {}

    q = '本表中，本年收入合计和本年支出合计分别是多少万元？'
    correct = '本年收入合计为190395.32万元。[1]本年支出合计为189274.27万元。[1]'
    assert validate_grounded_answer(correct, [citation], finish_reason='stop', question=q, verified_pdf_rows=rows).status == 'VERIFIED'
    compound = '审计署2024年度《收入支出决算总表》中，本年收入合计为190,395.32万元，本年支出合计为189,274.27万元。[1]'
    assert validate_grounded_answer(compound, [citation], finish_reason='stop', question=q, verified_pdf_rows=rows).status == 'VERIFIED'
    # A title belonging to one table cannot support values from another table.
    split = {1: {**rows[1], 'table_groups': [
        {'header': '收入支出决算总表', 'rows': [rows[1]['rows'][0]]},
        {'header': '另一张表', 'rows': [rows[1]['rows'][1]]},
    ]}}
    assert validate_grounded_answer(compound, [citation], finish_reason='stop', question=q, verified_pdf_rows=split).status != 'VERIFIED'
    for bad_prefix in ('审计署2023年度', '教育部2024年度'):
        bad_compound = compound.replace('审计署2024年度', bad_prefix)
        assert validate_grounded_answer(bad_compound, [citation], finish_reason='stop', question=q, verified_pdf_rows=rows).status != 'VERIFIED'
    for bad in ('本年收入合计为189274.27万元。[1]', '本年收入合计为190395.32元。[1]',
                '本年收入合计为190395.32万元。[1]'):
        assert validate_grounded_answer(bad, [citation], finish_reason='stop', question=q, verified_pdf_rows=rows).status != 'VERIFIED'
    ambiguous = {1: [('总计', '100', '万元'), ('总计', '100', '万元')]}
    assert validate_grounded_answer('总计为100万元。[1]', [citation], finish_reason='stop', verified_pdf_rows=ambiguous).status != 'VERIFIED'


def test_multiple_pdf_tables_do_not_share_title_or_rows(tmp_path, monkeypatch):
    import fitz
    path = tmp_path / 'source.pdf'
    path.write_bytes(b'fixture')
    table_a = SimpleNamespace(bbox=(0, 50, 100, 100), extract=lambda: [
        ['项目', '行次', '决算数'], ['本年收入合计', '8', '190,395.32']])
    table_b = SimpleNamespace(bbox=(0, 150, 100, 200), extract=lambda: [
        ['项目', '行次', '决算数'], ['本年支出合计', '19', '189,274.27']])
    page = SimpleNamespace(rect=SimpleNamespace(width=100),
                           find_tables=lambda: SimpleNamespace(tables=[table_a, table_b]),
                           get_textbox=lambda rect: ('收入支出决算总表\n单位：万元' if rect.y0 == 0
                                                     else '其他决算表\n单位：万元'),
                           get_text=lambda: '审计署2024年度 部门决算')
    pdf = MagicMock()
    pdf.__enter__.return_value = pdf
    pdf.__len__.return_value = 1
    pdf.__getitem__.return_value = page
    monkeypatch.setattr(fitz, 'open', lambda *a, **kw: pdf)
    content = '本年收入合计\n8\n190,395.32\n本年支出合计\n19\n189,274.27'
    chunk = SimpleNamespace(file_id='file', content=content, page_number=1)
    doc = SimpleNamespace(id='file', kb_id='kb', local_path=str(path))
    session = MagicMock()
    session.get.side_effect = lambda model, ident: chunk if model is DocumentChunk else doc
    citation = {'chunk_id': 'chunk', 'fileId': 'file', 'content': content}
    proof = verified_pdf_rows(session, [citation], ['kb'])
    assert len(proof[1]['table_groups']) == 2
    claim = '审计署2024年度《收入支出决算总表》中，本年收入合计为190,395.32万元，本年支出合计为189,274.27万元。[1]'
    assert validate_grounded_answer(claim, [citation], finish_reason='stop', verified_pdf_rows=proof).status != 'VERIFIED'
    table_b.bbox = (0, 75, 100, 125)
    assert verified_pdf_rows(session, [citation], ['kb']) == {}
