"""Conservative, model-free checks for source-attributed answers.

VERIFIED means deterministic textual or structured attribution only. It does
not establish that the source is correct, relevant, or answers the question.
"""
from dataclasses import dataclass
import re
from typing import Mapping, Sequence


@dataclass(frozen=True)
class GroundedValidationResult:
    status: str
    reasons: tuple[str, ...]
    referenced_citation_ids: tuple[int, ...] = ()
    missing_fields: tuple[str, ...] = ()


_MARKER = re.compile(r"\[(\d+)\]")
# Numeric/date/identifier components must occur in the source cited for the
# specific claim, rather than somewhere in the aggregate retrieval context.
_FACT = re.compile(r"(?<![A-Za-z0-9_.])(?:[A-Za-z]+[-_/])?\d+(?:[.,:/-]\d+)*(?:[A-Za-z%％]+)?(?![A-Za-z0-9_]|\.(?=\d))")
_BREAK = re.compile(r"(?<=[。！？!?；;])(?![”\"])|\n+")
_FIELD_VALUE = re.compile(
    r"(?P<field>[\u4e00-\u9fffA-Za-z0-9（）()·]{2,30}?)\s*"
    r"(?:为|是|:|：)\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<unit>[\u4e00-\u9fffA-Za-z%％]{1,8})"
)
_QUOTATION = re.compile(r"(?:资料显示|原文指出|原文为)[：:]\s*[“\"](.+)[”\"]")


def _normalise(text: str) -> str:
    return " ".join(text.split()).strip(" \t\r\n。！？!?；;，,.:：")


def _units(text: str) -> list[str]:
    return [_normalise(piece) for piece in _BREAK.split(text) if _normalise(piece)]


def _source_units(text: str) -> list[str]:
    # A source question cannot be promoted to a factual assertion simply
    # because normalization removes punctuation.
    return [_normalise(piece) for piece in _BREAK.split(text)
            if _normalise(piece) and not piece.rstrip().endswith(('?', '？'))]


def _source_context_is_unambiguous(text: str) -> bool:
    """Reject partial excerpts if surrounding prose might qualify the claim.

    A sequence of self-contained field/value rows is the only multi-unit
    context we can assess without making assumptions about surrounding prose.
    """
    raw_units = _units(text)
    eligible_units = _source_units(text)
    if len(raw_units) != len(eligible_units) or not eligible_units:
        return False
    return len(eligible_units) == 1 or all(_FIELD_VALUE.fullmatch(unit) for unit in eligible_units)


def _exact_equals_row_supports(claim: str, source: str) -> bool:
    """Recognize an exact `field=value` row expressed with an English copula.

    The field and the entire value must match the same row after case and
    whitespace normalization. No numeric, unit or semantic substitution is
    allowed. Callers must first establish source-context eligibility.
    """
    claim = re.sub(r'^according to the table\s*,\s*', '', claim, flags=re.IGNORECASE)
    compact = lambda text: re.sub(r'\s+', '', _normalise(text)).casefold()
    wanted = compact(claim)
    rows = source.split('|')
    # Explanatory or correction rows may qualify their neighbors. A simple
    # field/value matcher cannot establish that relationship safely.
    if any(row.count('=') != 1 or re.match(
        r'\s*(?:note|warning|caution|correction|disclaimer|erratum)\s*=',
        row, flags=re.IGNORECASE) for row in rows):
        return False
    for row in rows:
        if row.count('=') != 1:
            continue
        field, value = (part.strip() for part in row.split('=', 1))
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 ]{1,59}', field) or not value:
            continue
        if wanted in {compact(field + ' is ' + value), compact(field + ' are ' + value)}:
            return True
    return False


def _exact_single_row_table_supports(claim: str, source: str) -> bool:
    """Match a complete three-line Markdown table and one exact header/value.

    The narrow shape prevents an unrelated row, a repeated number in another
    column, or omitted extra source text from being treated as support.
    """
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if len(lines) != 3 or any(not line.startswith('|') or not line.endswith('|') for line in lines):
        return False
    cells = [[cell.strip() for cell in line[1:-1].split('|')] for line in lines]
    header, separator, values = cells
    if not header or len(header) != len(values) or len(separator) != len(header):
        return False
    if not all(re.fullmatch(r':?-{3,}:?', cell) for cell in separator):
        return False
    if any(not cell for cell in header) or len(set(' '.join(x.casefold().split()) for x in header)) != len(header):
        return False
    text = re.sub(r'^according to the table\s*,\s*', '', claim, flags=re.IGNORECASE)
    text = re.sub(r'^the\s+', '', text, flags=re.IGNORECASE)
    normal = lambda value: ' '.join(_normalise(value).casefold().split())
    return any(normal(text) in {normal(h + ' is ' + v), normal(h + ': ' + v), normal(h + '：' + v)}
               for h, v in zip(header, values) if v)


def _verified_pdf_compound_supports(claim: str, proof: Mapping) -> bool:
    """Verify a narrow document/year/table prefix and two exact table fields."""
    prefix = re.match(r'^([\u4e00-\u9fff]{2,20})(\d{4})年度《([^》]{1,40})》中[，,]', claim)
    if not prefix:
        return False
    organization, year, title = prefix.groups()
    cover = re.sub(r'\s+', '', str(proof.get('cover', '')))
    if organization + year + '年度' not in cover:
        return False
    tail = claim[prefix.end():].strip()
    first = _FIELD_VALUE.match(tail)
    if not first:
        return False
    remainder = tail[first.end():]
    if not remainder.startswith('，'):
        return False
    second = _FIELD_VALUE.match(remainder[1:].lstrip())
    if not second or _normalise(remainder[1:].lstrip()[second.end():]):
        return False
    fields = [first.groupdict(), second.groupdict()]
    if fields[0]['field'] == fields[1]['field']:
        return False
    rows = proof.get('rows', ())
    for field in fields:
        matching = [row for row in rows if row[0] == field['field']]
        if (len(matching) != 1 or matching[0][1].replace(',', '') != field['value'].replace(',', '')
                or matching[0][2] != field['unit']):
            return False
    matching_groups = [group for group in proof.get('table_groups', ())
                       if title in re.sub(r'\s+', '', str(group.get('header', '')))
                       and all(any(row[0] == field['field']
                                       and row[1].replace(',', '') == field['value'].replace(',', '')
                                       and row[2] == field['unit']
                                       for row in group.get('rows', ())) for field in fields)]
    return len(matching_groups) == 1


def validate_grounded_answer(
    answer: str,
    citations: Sequence[Mapping],
    *,
    finish_reason: str | None = None,
    question: str | None = None,
    verified_pdf_rows: Mapping[int, Sequence[tuple[str, str, str]]] | None = None,
    spreadsheet_context=None,
    pdf_context=None,
) -> GroundedValidationResult:
    """Check claims with trailing [1]-style markers against cited source text.

    Each marker indexes the original citations sequence (1-based). A claim may
    carry multiple adjacent markers. Supported claims either reproduce a source
    sentence, quote one with a neutral wrapper, or match a complete delimited
    field/value/unit source row. Source relevance and answer completeness are
    always unknown; no question-level semantic validation is performed.
    """
    if finish_reason is not None and finish_reason != "stop":
        return GroundedValidationResult("INVALID", ("incomplete_response",))
    if not isinstance(answer, str) or not answer.strip():
        return GroundedValidationResult("INVALID", ("empty_answer",))
    if re.search(r"(?:\.\.\.|…|\[\d*$)\s*$", answer):
        return GroundedValidationResult("INVALID", ("possibly_truncated",))

    markers = list(_MARKER.finditer(answer))
    if not markers:
        return GroundedValidationResult("UNKNOWN", ("missing_citations",))
    referenced = tuple(dict.fromkeys(int(m.group(1)) for m in markers))
    # Source-native expansions are accepted only as the complete paired
    # statement and exact records built from the server-held proof. Their
    # parent IDs must never be mistaken for indexed chunk IDs.
    expansion_indices = {i for i, c in enumerate(citations, 1)
                         if isinstance(c, Mapping) and (c.get('source_type') == 'source_native_table_expansion'
                                                        or c.get('parent_chunk_id'))}
    if pdf_context is not None:
        from app.services.pdf_table_grounding import build_pdf_supplementary_citations
        expected = build_pdf_supplementary_citations(pdf_context, citations[:-2]) if len(citations) >= 3 else []
        if len(expected) != 2 or list(citations[-2:]) != expected:
            return GroundedValidationResult('INVALID', ('source_expansion_identity_mismatch',), referenced)
        if not expansion_indices.intersection(referenced):
            return GroundedValidationResult('UNKNOWN', ('source_expansion_incomplete',), referenced)
    if expansion_indices.intersection(referenced):
        from app.services.pdf_table_grounding import pdf_expansion_supports
        if (finish_reason == 'stop' and expansion_indices == set(referenced)
                and pdf_expansion_supports(pdf_context, citations, question, answer)):
            return GroundedValidationResult('VERIFIED', ('source_native_table_expansion',), referenced)
        return GroundedValidationResult('UNKNOWN', ('source_expansion_unverified',), referenced)
    for index in referenced:
        if index < 1 or index > len(citations):
            return GroundedValidationResult("INVALID", ("invalid_citation_id",), referenced)
        citation = citations[index - 1]
        if (not isinstance(citation, Mapping) or not citation.get("chunk_id")
                or not isinstance(citation.get("content"), str)
                or not citation["content"].strip()
                or citation.get("content_type") == "image"):
            return GroundedValidationResult("INVALID", ("invalid_citation_source",), referenced)

    # A marker belongs to the immediately preceding claim, and adjacent
    # markers belong to the same claim. Uncited text is never silently ignored.
    groups = list(re.finditer(r"(?:\[\d+\]\s*)+", answer))
    previous = 0
    unknown = False
    structured_rows_used = set()
    explicit_pair = (re.search(r'在([^\s，。？！?]{1,24}?)(?:与|和|及)([^\s，。？！?]{1,24}?)中分别', question or '')
                     or re.search(r'[，,]([^\s，。？！?]{1,24}?)(?:与|和|及)([^\s，。？！?]{1,24}?)分别', question or ''))
    paired_fields = set(explicit_pair.groups()) if explicit_pair else set()
    supported_fields = set()
    for group in groups:
        claim = answer[previous:group.start()].strip()
        previous = group.end()
        if not claim:
            return GroundedValidationResult("INVALID", ("missing_claim_for_citation",), referenced)
        ids = [int(m.group(1)) for m in _MARKER.finditer(group.group())]
        if len(set(ids)) > 1:
            return GroundedValidationResult("UNKNOWN", ("multi_citation_support_unverified",), referenced)
        texts = [citations[i - 1]["content"] for i in ids]
        for field in paired_fields:
            if field in claim and any(field in text for text in texts):
                supported_fields.add(field)
        from app.services.spreadsheet_grounding import spreadsheet_statement_supports
        spreadsheet_match = spreadsheet_statement_supports(claim, spreadsheet_context, ids[0], question)
        table_match = (len(texts) == 1 and _exact_single_row_table_supports(claim, texts[0]))
        field_match = _FIELD_VALUE.fullmatch(_normalise(claim))
        pdf_proof = (verified_pdf_rows or {}).get(ids[0], ())
        pdf_rows = pdf_proof.get('rows', ()) if isinstance(pdf_proof, Mapping) else pdf_proof
        pdf_compound = isinstance(pdf_proof, Mapping) and _verified_pdf_compound_supports(claim, pdf_proof)
        matching_rows = [row for row in pdf_rows if field_match and row[0] == field_match.group('field')]
        # Even identical values are ambiguous when the same label occurs in
        # multiple table columns or rows (for example, two "总计" fields).
        pdf_match = bool(len(matching_rows) == 1 and field_match and
                         field_match.group('value').replace(',', '') == matching_rows[0][1].replace(',', '') and
                         field_match.group('unit') == matching_rows[0][2])
        if not spreadsheet_match and not table_match and not pdf_match and not pdf_compound and any(not _source_context_is_unambiguous(text) for text in texts):
            return GroundedValidationResult("UNKNOWN", ("source_context_unverified",), referenced)
        source_facts = {fact for text in texts for fact in _FACT.findall(text)}
        if not spreadsheet_match and not pdf_match and not pdf_compound and any(fact not in source_facts for fact in _FACT.findall(claim)):
            return GroundedValidationResult("UNSUPPORTED", ("fact_absent_from_cited_sources",), referenced)
        if spreadsheet_match:
            continue
        claims = _units(claim)
        source_units = {unit for text in texts for unit in _source_units(text)}
        for unit in claims:
            if (table_match or pdf_match or pdf_compound) and len(claims) == 1:
                continue
            if unit in source_units:
                continue
            if any(_exact_equals_row_supports(unit, text) for text in texts):
                continue
            quoted = _QUOTATION.fullmatch(unit)
            if quoted and _normalise(quoted.group(1)) in source_units:
                continue
            field_value = _FIELD_VALUE.fullmatch(unit)
            if not field_value:
                unknown = True
                continue
            parts = field_value.groupdict()
            candidates = []
            same_field = False
            for citation_id in ids:
                for row_number, row in enumerate(_source_units(citations[citation_id - 1]["content"])):
                    source_field = _FIELD_VALUE.fullmatch(row)
                    if source_field and source_field.group("field") == parts["field"]:
                        same_field = True
                        if source_field.groupdict() == parts:
                            candidates.append((citation_id, row_number))
            available = [row for row in candidates if row not in structured_rows_used]
            if available:
                structured_rows_used.add(available[0])
            elif same_field and not candidates:
                return GroundedValidationResult("UNSUPPORTED", ("field_value_or_unit_mismatch",), referenced)
            else:
                unknown = True
        if not claims:
            unknown = True
    # Sentence punctuation after a citation marker (e.g. "claim [1].") is
    # formatting, not an uncited claim. Any words, numbers or symbols beyond
    # this narrowly defined punctuation still require source attribution.
    if not re.fullmatch(r"[\s。.!?！？；;，,]*", answer[previous:]):
        return GroundedValidationResult("UNKNOWN", ("uncited_text",), referenced)
    if unknown:
        return GroundedValidationResult("UNKNOWN", ("semantic_or_field_support_unverified",), referenced)
    if paired_fields and paired_fields != supported_fields:
        return GroundedValidationResult("UNKNOWN", ("requested_fields_unverified",), referenced,
                                        tuple(sorted(paired_fields - supported_fields)))
    if finish_reason is None:
        return GroundedValidationResult("UNKNOWN", ("completion_status_unknown",), referenced)
    return GroundedValidationResult(
        "VERIFIED", ("deterministic_source_attribution", "semantic_relevance_unknown", "answer_completeness_unknown"), referenced
    )
