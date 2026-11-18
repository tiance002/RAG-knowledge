"""Character-bounded text packing inspired by WeKnora's Go chunker.

Parsed units are intermediate content, not final index chunks. Offsets below
refer to normalized extracted text, never source-file bytes.
"""
from dataclasses import dataclass
import re


@dataclass
class TextPart:
    content: str
    start: int
    end: int


def pack_text(text, size, overlap=0):
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError('CHUNK_CONFIGURATION_INVALID')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    parts, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer a complete paragraph, then line, then sentence/word.
            for separator in ('\n\n', '\n', '。', '. ', ' '):
                boundary = text.rfind(separator, start + max(size // 2, overlap + 1), end)
                if boundary >= 0:
                    end = boundary + len(separator)
                    break
        parts.append(TextPart(text[start:end], start, end))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return parts


def table_cells(line):
    line = line.strip()
    if not line.startswith('|'):
        return None
    # A pipe escaped by an odd number of backslashes is literal cell content.
    cells, current, slashes = [], '', 0
    for char in line[1:]:
        if char == '|' and slashes % 2 == 0:
            cells.append(current.strip())
            current = ''
        else:
            current += char
        slashes = slashes + 1 if char == '\\' else 0
    if current or not line.endswith('|'):
        cells.append(current.strip())
    return cells


def markdown_blocks(text):
    """Recognize GFM tables outside fences; normalize only recognized tables."""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').splitlines(keepends=True)
    i, pending, fence = 0, [], None
    while i < len(lines):
        line = lines[i]
        token = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if token:
            marker = token.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            pending.append(line)
            i += 1
            continue
        cells = table_cells(line) if fence is None else None
        separator = table_cells(lines[i + 1]) if i + 1 < len(lines) else None
        if cells and separator and len(cells) == len(separator) and all(re.fullmatch(r':?-{3,}:?', c) for c in separator):
            if pending:
                yield 'text', ''.join(pending)
                pending = []
            rows = [cells, separator]
            i += 2
            while i < len(lines):
                row = table_cells(lines[i])
                if row is None or len(row) != len(cells):
                    break
                next_row = table_cells(lines[i + 1]) if i + 1 < len(lines) else None
                if next_row and len(next_row) == len(row) and all(re.fullmatch(r':?-{3,}:?', c) for c in next_row):
                    break
                rows.append(row)
                i += 1
            # Match WeKnora's pendingExtend: empty placeholder column names
            # acquire semantic names from the first real row, retaining it.
            if not any(rows[0]) and len(rows) > 2:
                rows[0] = list(rows[2])
            yield 'table', ''.join('| ' + ' | '.join(row) + ' |\n' for row in rows)
        else:
            pending.append(line)
            i += 1
    if pending:
        yield 'text', ''.join(pending)


def split_table(text, size):
    lines = text.splitlines(keepends=True)
    header = ''.join(lines[:2])
    if len(header) > size or (len(header) == size and len(lines) > 2):
        raise ValueError('MARKDOWN_TABLE_HEADER_EXCEEDS_CHUNK_BUDGET')
    if len(lines) <= 2:
        return [text]
    result, body = [], ''
    for line in lines[2:]:
        if len(header) + len(line) > size:
            if body:
                result.append(header + body)
                body = ''
            marker = '[表格行文本片段，需结合相邻片段]\n'
            budget = size - len(header) - len(marker)
            if budget <= 0:
                raise ValueError('MARKDOWN_TABLE_HEADER_EXCEEDS_CHUNK_BUDGET')
            result.extend(header + marker + part.content for part in pack_text(line, budget))
            continue
        if body and len(header) + len(body) + len(line) > size:
            result.append(header + body)
            body = ''
        body += line
    if body:
        result.append(header + body)
    return result
