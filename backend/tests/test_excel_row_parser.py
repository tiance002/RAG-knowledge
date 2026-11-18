from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.services.parser import FileParser
from app.services.parent_child import build_parent_child_chunks, ParentChildConfig


def parse(tmp_path, rows, header=False, merge=None):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Data'
    for row in rows:
        sheet.append(row)
    if merge:
        sheet.merge_cells(merge)
    path = tmp_path / 'source.xlsx'
    book.save(path)
    parser = FileParser('test', parser_settings=SimpleNamespace(
        IMAGE_DIR=str(tmp_path / 'images'), PDF_AUDIT_DIR=str(tmp_path / 'audit')),
        xlsx_first_row_as_header=header)
    return parser._parse_excel(str(path))


def test_default_preserves_title_blanks_and_original_coordinates(tmp_path):
    chunks = parse(tmp_path, [['Title', None], [None, None], ['Name', 'Count'], ['A', 12]])
    assert len(chunks) == 1
    assert 'A: Title' in chunks[0].content
    assert 'A: A,B: 12' in chunks[-1].content
    assert [loc['row_start'] for loc in chunks[0].metadata['source_locators']] == [1, 3, 4]
    assert chunks[-1].page_number is None


def test_explicit_headers_unique_and_blank_fallback(tmp_path):
    chunks = parse(tmp_path, [['Name', 'Name', None], ['A', 'B', 3]], True)
    assert len(chunks) == 1
    assert 'Name: A,Name_2: B,C: 3' in chunks[0].content
    assert chunks[0].metadata['source_locator']['row_start'] == 2


def test_real_merge_only_and_parent_child_limits(tmp_path):
    chunks = parse(tmp_path, [['Group', 'x' * 600], [None, 'y'], [None, 'z']], merge='A1:A2')
    assert chunks[0].content.count('A: Group') == 2
    assert chunks[0].content.endswith('B: z\n')
    graph = build_parent_child_chunks(chunks, ParentChildConfig(256, 0, 80, 8))
    assert all(len(c.content) <= (256 if c.metadata['chunk_type'] == 'parent' else 80) for c in graph)
    assert all(c.metadata['source_locator']['sheet'] == 'Data' for c in graph)


def test_header_flag_rejects_string(tmp_path):
    with pytest.raises(ValueError, match='XLSX_HEADER_CONFIGURATION_INVALID'):
        FileParser('test', xlsx_first_row_as_header='false')


def test_single_huge_row_is_bounded_without_parent_child(tmp_path):
    chunks = parse(tmp_path, [['x' * 12000]])
    assert len(chunks) >= 2
    assert all(len(c.content) <= 7500 for c in chunks)
    # The Go persistence layer drops a standalone whitespace-only trailing unit.
    assert ''.join(c.content for c in chunks).rstrip('\n') == 'A: ' + 'x' * 12000
    assert all(c.content.strip() for c in chunks)
    assert all(c.metadata['source_locator']['row_start'] == 1 for c in chunks)
