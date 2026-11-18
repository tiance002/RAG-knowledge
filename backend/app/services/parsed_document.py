"""Format-independent extracted document. Extraction never chooses chunks."""
from dataclasses import dataclass, field


@dataclass
class SourceSpan:
    start: int
    end: int
    block: object


@dataclass
class ParsedDocument:
    blocks: list
    source_sha256: str = ''
    pages: list = field(default_factory=list)

    def text_and_spans(self):
        text, spans = '', []
        for block in self.blocks:
            if block.content_type == 'image' or not block.content:
                continue
            body = block.content.replace('\r\n', '\n').replace('\r', '\n')
            start = len(text)
            text += body
            if not text.endswith('\n'):
                text += '\n'
            spans.append(SourceSpan(start, len(text), block))
        return text, spans
