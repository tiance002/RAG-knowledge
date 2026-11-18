import pytest
from types import SimpleNamespace

from app.services.text_rechunk import pack_text, markdown_blocks, split_table
from app.services.parser import FileParser
from app.services.parent_child import build_parent_child_chunks, ParentChildConfig, remap_and_validate_graph


def test_text_reassembly_overlap_unicode_and_bounds():
    text = '标题\n' + ('一行原文 abc。\n' * 100)
    parts = pack_text(text, 80, 8)
    assert all(len(p.content) <= 80 and p.content == text[p.start:p.end] for p in parts)
    assert parts[0].start == 0 and parts[-1].end == len(text)
    assert all(b.start <= a.end for a, b in zip(parts, parts[1:]))


def test_markdown_two_tables_and_code_fence_do_not_share_headers():
    text = '| Name | Amount |\n| --- | --- |\n| A | 1 |\n\n| Unit | Value |\n| --- | --- |\n| mm | 2 |\n\n```\n| fake | table |\n| --- | --- |\n```\n'
    blocks = list(markdown_blocks(text))
    tables = [body for kind, body in blocks if kind == 'table']
    assert len(tables) == 2
    assert 'Amount' not in tables[1] and 'Unit' not in tables[0]
    assert any('fake' in body for kind, body in blocks if kind == 'text')


def test_table_headers_repeat_in_children_and_graph_validates(tmp_path):
    path = tmp_path / 'test.md'
    path.write_text('| Name | Count |\n| --- | --- |\n' + ''.join(f'| item{i} | {i} |\n' for i in range(30)), encoding='utf-8')
    parser = FileParser('test', parser_settings=SimpleNamespace(IMAGE_DIR=str(tmp_path / 'images'), PDF_AUDIT_DIR=str(tmp_path / 'audit')))
    parser.splitter = SimpleNamespace(chunk_size=256, chunk_overlap=16)
    parser.chunk_mode = 'custom'
    graph = parser.parse(str(path), chunk_size=256, chunk_overlap=0,
                         parent_child_config=ParentChildConfig(256, 0, 100, 8))
    children = [c for c in graph if c.metadata['chunk_type'] == 'child']
    assert len(children) > 1
    assert all('| Name | Count |' in c.content for c in children)
    rows = [dict(content=c.content, **c.metadata) for c in graph]
    remap_and_validate_graph(rows, 'file')
    assert all(f'| item{i} | {i} |' in ''.join(c.content for c in children) for i in range(30))


def test_oversized_table_preserves_all_row_text_as_marked_fragments():
    header = '| A |\n| --- |\n'
    row = '| ' + 'x' * 100 + ' |\n'
    parts = split_table(header + row, 64)
    marker = '[表格行文本片段，需结合相邻片段]\n'
    assert len(parts) > 1 and all(len(p) <= 64 for p in parts)
    assert ''.join(p[len(header) + len(marker):] for p in parts) == row


def test_empty_header_promotion_and_adjacent_table_reset():
    text = '| | |\n| --- | --- |\n| Name | Amount |\n| A | 1 |\n| Unit | Price |\n| --- | --- |\n| kg | 3 |\n'
    tables = [body for kind, body in markdown_blocks(text) if kind == 'table']
    assert len(tables) == 2
    assert tables[0].startswith('| Name | Amount |\n')
    assert tables[1].startswith('| Unit | Price |\n')
    assert 'Amount' not in tables[1]


def test_escaped_pipe_is_one_cell_and_alignment_survives():
    text = '| Name | Count |\n| :--- | ---: |\n| A\\|B | 2 |\n'
    assert list(markdown_blocks(text)) == [('table', text)]


def test_column_mismatch_ends_table_without_losing_text():
    text = '| A | B |\n| --- | --- |\n| 1 | 2 |\n| three | columns | here |\nAfter\n'
    blocks = list(markdown_blocks(text))
    assert len(blocks) == 2 and blocks[1][0] == 'text'
    assert 'three' in blocks[1][1] and 'After' in blocks[1][1]
