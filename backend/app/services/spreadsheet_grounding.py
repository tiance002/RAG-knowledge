"""Narrow spreadsheet context prepared from verified source files only."""
from dataclasses import dataclass
import re

from app.models import FileDocument
from app.services.source_registry import trusted_source_hash
from app.services.spreadsheet_table_validation import discover_spreadsheet_cells


@dataclass(frozen=True)
class SpreadsheetGrounding:
    citation_index: int
    proof: dict
    statement: str


def _question_matches(question, proof, metric):
    # No fuzzy entity matching, synonyms, comparisons or unstated filters.
    # Only an explicit English count question with an optional exact FY and
    # exact worksheet qualifier is supported.
    label = re.escape(proof['label'])
    measure = re.escape(metric)
    sheet = re.escape(proof['sheet'])
    year = re.escape(proof['fiscal_year'])
    return bool(re.fullmatch(
        rf'\s*how\s+many\s+{measure}\s+(?:are|were)\s+(?:recorded\s+)?(?:for|in)\s+{label}'
        rf'(?:\s+in\s+(?:table|sheet|worksheet)\s+{sheet})?'
        rf'(?:\s+(?:in|for)\s+(?:fiscal\s+year|fy)\s+{year})?\s*\?\s*',
        question, re.IGNORECASE))


def _statement(proof, index):
    header = ' '.join(text for _, col, text in proof['headers'] if col == proof['value_col'])
    # A number-of header is the only semantic interpretation supported here.
    if not re.fullmatch(r'Number of [A-Za-z ]+\d*', header, re.IGNORECASE):
        return None, None
    display_header = re.sub(r'\d+$', '', header)
    metric = display_header[len('Number of '):].strip()
    if type(proof['value']) is not int or not proof['footnotes']:
        return None, None
    notes = proof['notes']
    # All source qualifiers are transcribed verbatim, including general notes.
    qualifier = ' '.join(x['text'] for _, x in sorted(proof['footnotes'].items()))
    qualifier += (' ' + ' '.join(notes)) if notes else ''
    if not qualifier:
        return None, None
    return (f"In fiscal year {proof['fiscal_year']}, {display_header} for {proof['label']} "
            f"was {proof['value']}. {qualifier} [{index}]"), metric


def prepare_spreadsheet_grounding(session, citations, kb_ids, question):
    """Return one unambiguous server-verified proof or None, without model calls.

    Only selected retrieved citations and an operator-pinned source manifest
    may supply evidence. Caller metadata is never used as source proof.
    """
    if not isinstance(question, str):
        return None
    matches = []
    # Bounded work: only the first three XLSX citations are inspected.
    xlsx = 0
    for index, citation in enumerate(citations, 1):
        if not str(citation.get('fileName', '')).lower().endswith('.xlsx'):
            continue
        xlsx += 1
        if xlsx > 3:
            break
        doc = session.get(FileDocument, citation.get('fileId'))
        if not doc or doc.kb_id not in kb_ids:
            continue
        digest = trusted_source_hash(doc.local_path)
        if not digest:
            continue
        for proof in discover_spreadsheet_cells(session, citation, kb_ids, source_sha256=digest):
            statement, metric = _statement(proof, index)
            if statement and _question_matches(question, proof, metric):
                matches.append(SpreadsheetGrounding(index, proof, statement))
    # Duplicated evidence is still ambiguous; no arbitrary source selection.
    return matches[0] if len(matches) == 1 else None


def spreadsheet_statement_supports(claim, context, citation_index, question):
    if not isinstance(context, SpreadsheetGrounding) or context.citation_index != citation_index:
        return False
    expected, metric = _statement(context.proof, citation_index)
    return bool(expected and metric and _question_matches(question or '', context.proof, metric)
                and claim.strip() == expected.rsplit(f'[{citation_index}]', 1)[0].strip())
