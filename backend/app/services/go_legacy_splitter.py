"""Python port of WeKnora SplitText's legacy text units and semantic overlap.

Defaults are 512 Unicode code points / 80 overlap, separators newline-newline,
newline, Chinese period. Atomic units can exceed ChunkSize up to 7500,
matching Go; callers needing a hard embedding budget must enforce it later.
Includes legacy Markdown header tracking; higher-level parser normalization
remains a separate stage.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Unit:
    content: str
    start: int
    end: int
    seq: int = -1


PATTERNS = [
    r'(?s)\$\$.*?\$\$', r'!\[[^\]\n]{0,200}\]\([^)\n]{1,500}\)',
    r'\[[^\]\n]{1,200}\]\([^)\n]{1,500}\)',
    r'(?m)[ ]*(?:\|[^|\n]*)+\|[\r\n]+\s*(?:\|\s*:?-{3,}:?\s*)+\|[\r\n]+',
    r'(?m)[ ]*(?:\|[^|\n]*)+\|[\r\n]+',
    r'(?s)```(?:\w+)?[\r\n].*?```', r'`[^`\r\n]+`',
]


def protected_spans(text):
    matches = sorted((m.start(), m.end()) for pattern in PATTERNS for m in re.finditer(pattern, text))
    matches.sort(key=lambda p: (p[0], -p[1]))
    result, last = [], 0
    for start, end in matches:
        if start >= last:
            result.append((start, end))
            last = end
    return result


def recursive_parts(text, separators, size):
    if not text or not separators or (size > 0 and len(text) <= size):
        return [text]
    for index, separator in enumerate(separators):
        if not separator or separator not in text:
            continue
        pieces = [part for part in re.split('(' + re.escape(separator) + ')', text) if part]
        if len(pieces) <= 1:
            continue
        result = []
        for part in pieces:
            if size > 0 and len(part) > size and separators[index + 1:]:
                result.extend(recursive_parts(part, separators[index + 1:], size))
            else:
                result.append(part)
        return result
    return [text]


def force_units(text, offset, maximum=7500):
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            for pos in range(end - 1, max(start, end - 200), -1):
                if text[pos] in '\n ':
                    end = pos + 1
                    break
        yield Unit(text[start:end], offset + start, offset + end)
        start = end


def build_units(text, size, separators):
    units, pos = [], 0
    for start, end in protected_spans(text) + [(len(text), len(text))]:
        if start > pos:
            offset = pos
            for part in recursive_parts(text[pos:start], separators, size):
                units.append(Unit(part, offset, offset + len(part)))
                offset += len(part)
        if end > start:
            units.extend(force_units(text[start:end], start))
        pos = end
    return units


def semantic_overlap(current, overlap, size, next_length):
    maximum = min(overlap, size - next_length)
    if maximum <= 0:
        return []
    # Synthetic header units are a barrier, exactly as semanticOverlapWindow.
    tail = []
    for unit in reversed(current):
        if not unit.content:
            continue
        if unit.end - unit.start != len(unit.content) or unit.start == unit.end:
            break
        tail.insert(0, unit)
    if not tail:
        return []
    text = ''.join(u.content for u in tail)
    offset = tail[0].start
    start = max(0, len(text) - maximum - 4)
    window = text[start:]
    minimum_end = max(0, len(window) - maximum)
    protected = protected_spans(window)
    candidates = []
    paragraph_positions = set()
    for priority, pattern in ((1, r'\r\n\r\n|\n\n'), (2, r'\r\n|\n'), (3, r'[。？！]|[.?!] ')):
        for match in re.finditer(pattern, window):
            a, b = match.span()
            if priority == 1:
                paragraph_positions.update(range(a, b))
            if priority == 2 and a in paragraph_positions:
                continue
            if b < minimum_end or not window[b:].strip() or any(x <= a < y for x, y in protected):
                continue
            candidates.append((priority, a, b))
    if not candidates:
        return []
    end = min(candidates)[2]
    retained = window[end:]
    if not retained.strip() or len(retained) > maximum:
        return []
    return [Unit(retained, offset + start + end, current[-1].end)]


def _row_columns(text):
    for line in text.split('\n'):
        line = line.strip()
        if line and re.fullmatch(r'\s*(?:\|[^|\n]*)+\|\s*', line, re.ASCII):
            parts, current, slashes = [], '', 0
            for char in line:
                if char == '|' and slashes % 2 == 0:
                    parts.append(current)
                    current = ''
                else:
                    current += char
                slashes = slashes + 1 if char == '\\' else 0
            parts.append(current)
            if parts and not parts[0].strip():
                parts = parts[1:]
            if parts and not parts[-1].strip():
                parts = parts[:-1]
            return len(parts)
    return 0


def _header_columns(header):
    for line in header.split('\n'):
        if line.strip() and '---' not in line:
            count = _row_columns(line)
            if count:
                return count
    return 0


def _header_column_row(header):
    for line in header.split('\n'):
        line = line.strip()
        if line and '---' not in line and line.strip('| \t'):
            return line
    return ''


class HeaderTracker:
    start_pattern = re.compile(r'^\s*(?:\|[^|\n]*)+[\r\n]+\s*(?:\|\s*:?-{3,}:?\s*)+\|?[\r\n]+$', re.S | re.I | re.ASCII)
    end_pattern = re.compile(r'^\s*$|^\s*[^|\s].*$', re.S | re.I | re.ASCII)

    def __init__(self):
        self.header = ''
        self.pending_extend = self.pending_break = self.ended = False
        self.ended_this_unit = False

    def clear(self):
        self.header = ''
        self.pending_extend = False
        self.ended = True

    def update(self, text):
        self.ended_this_unit = False
        if self.pending_break:
            self.pending_break = False
            if self.header:
                self.ended_this_unit = _row_columns(text) > 0
                self.clear()
        if self.header and self.end_pattern.search(text):
            self.clear()
        if self.header and not self.pending_extend:
            if text.rstrip(' \t\r').endswith(('\n\n', '\r\n\r\n')):
                self.pending_break = True
            else:
                count = _row_columns(text)
                if count and _header_columns(self.header) and count != _header_columns(self.header):
                    self.clear()
                    self.ended_this_unit = True
        if self.pending_extend:
            if self.header and _row_columns(text):
                separator = next((line + '\n' for line in self.header.split('\n') if '---' in line), '')
                self.header = text + separator
            self.pending_extend = False
        if not self.header and not self.ended:
            match = self.start_pattern.search(text)
            if match:
                self.header = match.group()
                self.pending_extend = not self.header.split('\n')[0].strip('| \t\r')
        if not self.header:
            self.ended = False


def split_legacy(text, size=512, overlap=80, separators=('\n\n', '\n', '。')):
    if not text:
        return []
    size = size if size > 0 else 512
    overlap = max(0, overlap)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    chunks, current, length = [], [], 0
    tracker = HeaderTracker()

    def flush():
        if current:
            chunks.append(Unit(''.join(u.content for u in current), current[0].start, current[-1].end))

    for unit in build_units(text, size, separators):
        count = len(unit.content)
        if count > 7500:
            flush()
            current, length = [], 0
            tracker.update(unit.content)
            chunks.extend(force_units(unit.content, unit.start))
            continue
        tracker.update(unit.content)
        if tracker.ended_this_unit and current:
            flush()
            current, length = [], 0
        header = tracker.header if len(tracker.header) <= size else ''
        if length + count + len(header) > size and current:
            flush()
            current = semantic_overlap(current, overlap, size, count)
            length = sum(len(u.content) for u in current)
            if header and len(header) + count <= size:
                while current and length + count + len(header) > size:
                    length -= len(current.pop(0).content)
                overlap_text = ''.join(u.content for u in current)
                column_row = _header_column_row(header)
                present = (header in overlap_text or header in unit.content or
                           bool(column_row and (column_row in overlap_text or column_row in unit.content)))
                mismatch = bool(_header_columns(header) and _row_columns(unit.content) and
                                _header_columns(header) != _row_columns(unit.content))
                if not present and not mismatch:
                    pos = current[0].start if current else unit.start
                    current.insert(0, Unit(header, pos, pos))
                    length += len(header)
        if length + count > 7500:
            flush()
            current, length = [], 0
        current.append(unit)
        length += count
    flush()
    return [Unit(c.content, c.start, c.end, i) for i, c in enumerate(chunks)]
