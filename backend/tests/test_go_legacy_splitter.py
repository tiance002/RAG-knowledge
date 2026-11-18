from app.services.go_legacy_splitter import split_legacy, recursive_parts
import json
from pathlib import Path
import pytest


_GOLDEN = json.loads((Path(__file__).parent / 'fixtures/go_legacy_golden.json').read_text(encoding='utf8'))


@pytest.mark.parametrize('case', _GOLDEN['cases'], ids=lambda c: c['input']['name'])
def test_frozen_actual_go_output(case):
    request = case['input']
    actual = [dict(Content=c.content, Start=c.start, End=c.end, Seq=i, ContextHeader='')
              for i, c in enumerate(split_legacy(request['Text'], request['Size'], request['Overlap']))]
    assert actual == case['expected']


def test_separator_priority_preserves_delimiters():
    assert recursive_parts('abc\n\ndef\nghi', ('\n\n', '\n', '。'), 4) == ['abc', '\n\n', 'def', '\n', 'ghi']


def test_atomic_unit_can_exceed_target_but_not_absolute_guard():
    assert len(split_legacy('x' * 1000, 512, 80)) == 1
    chunks = split_legacy('x' * 16000)
    assert [len(c.content) for c in chunks] == [7500, 7500, 1000]


def test_semantic_overlap_and_unicode_source_spans():
    text = ('中文🙂 sentence. abc\n' * 100)
    chunks = split_legacy(text, 90, 25)
    assert len(chunks) > 1
    assert all(c.content == text[c.start:c.end] for c in chunks)
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(0 <= a.end - b.start <= 25 for a, b in zip(chunks, chunks[1:]))


def test_inline_code_and_links_are_protected():
    text = 'before\n' + '`' + 'a\n'.replace('\n', ' ') * 100 + '`\nafter'
    chunks = split_legacy(text, 50, 10)
    assert any('`' + 'a ' * 100 + '`' in c.content for c in chunks)
