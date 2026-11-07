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


# 单位括注既可能写成 (In thousands)，也可能写成中文单位。
_UNIT = re.compile(
    r'[（(]\s*in\s+(thousands?|millions?|billions?)\s*[)）]'
    r'|(千美元|百万美元|亿美元|千元|万元|亿元)',
    re.IGNORECASE,
)


def _column_header(proof):
    """把同一列的堆叠表头拼成主标题，并单独取出单位括注。

    原实现直接拼接整串并要求匹配 `Number of ...`，会把
    "Total operating revenue / Total / (In thousands)" 这种正常表头判为不支持，
    单位信息也随之丢失。
    """
    parts = [text for _, col, text in proof['headers'] if col == proof['value_col']]
    header = ' '.join(' '.join(parts).split())
    match = _UNIT.search(header)
    unit = match.group(0).strip() if match else ''
    if match:
        header = ' '.join((header[:match.start()] + ' ' + header[match.end():]).split())
    return header, unit


def _question_matches(question, proof, metric, *, countable):
    """只在问题明确指向该行该列时返回 True；不做同义词或模糊实体匹配。"""
    label = re.escape(proof['label'].strip())
    sheet = re.escape(proof['sheet'])
    year = re.escape(proof['fiscal_year'])
    if countable:
        words = re.escape(metric)
        patterns = (
            # 英文计数问法
            rf'\s*(?:how\s+many|what\s+(?:is|was)\s+the\s+number\s+of)\s+{words}\s+'
            rf'(?:(?:are|were|is|was)\s+)?(?:recorded\s+)?(?:for|in)\s+{label}'
            rf'(?:\s+in\s+(?:table|sheet|worksheet)\s+{sheet})?'
            rf'(?:\s+(?:in|for)\s+(?:fiscal\s+year|fy)\s+{year})?\s*[?？]?\s*',
            # 中文：<标签>有多少个<度量>
            rf'\s*{label}(?:州|市|县|区|地区)?(?:的)?\s*(?:一共|总共)?(?:有)?\s*'
            rf'(?:多少|几)\s*(?:个|所|家|间)?\s*{words}\s*[?？。]?\s*',
            # 中文：<标签>的<度量>数量是多少
            rf'\s*{label}(?:州|市|县|区|地区)?(?:的)?\s*{words}\s*(?:数量)?\s*'
            rf'(?:是|为|有)?\s*(?:多少|几)\s*(?:个|所|家|间)?\s*[?？。]?\s*',
            # 中文计数问法：<标签>……多少个/几所（计数列语义明确，无需英文度量词）
            rf'\s*{label}(?:州|市|县|区|地区)?[^?？。]*?(?:多少|几)[^?？。]*?'
            rf'(?:个|所|家|间|数量)[^?？。]*?[?？。]?\s*',
        )
    else:
        words = [word for word in re.split(r'\s+', metric) if len(word) >= 3]
        if not words:
            return False
        terms = '|'.join(re.escape(word) for word in words)
        patterns = (
            # 英文：what is the <列名> for <标签>
            rf'\s*(?:what\s+(?:is|was)|how\s+much\s+(?:is|was))\s+(?:the\s+)?[^?？]*?(?:{terms})'
            rf'[^?？]*?(?:for|of|in)\s+{label}[^?？]*?[?？]?\s*',
            # 中文：<标签>的<列名>是多少 / 总额 / 合计
            rf'\s*{label}(?:州|市|县|区|地区)?[^?？。]*?(?:{terms})[^?？。]*?'
            rf'(?:是多少|为多少|多少|总额|合计|总计)[^?？。]*?[?？。]?\s*',
        )
    return any(re.fullmatch(pattern, question, re.IGNORECASE) for pattern in patterns)


def _statement(proof, index):
    """返回 (陈述句, 度量词, 是否为计数列)；无法确定含义时返回 (None, None, False)。"""
    header, unit = _column_header(proof)
    if not header:
        return None, None, False
    countable = bool(re.fullmatch(r'Number of [A-Za-z ]+\d*', header, re.IGNORECASE))
    if countable:
        metric = re.sub(r'\d+$', '', header[len('Number of '):]).strip()
        # 计数列只能是整数，浮点数说明单位不是"个"。
        if type(proof['value']) is not int:
            return None, None, False
        display = 'Number of ' + metric
    else:
        # 非计数列必须有单位括注，否则数值口径无法确定。
        if not unit or type(proof['value']) not in (int, float):
            return None, None, False
        metric = header
        display = header
    notes = proof['notes']
    # 所有来源限定语逐字转写；没有脚注时退回 NOTE/SOURCE 说明，
    # 不再要求"必须有脚注"（无脚注列同样可用）。
    footnote_text = ' '.join(x['text'] for _, x in sorted(proof['footnotes'].items()))
    qualifier = ' '.join(part for part in (footnote_text, ' '.join(notes)) if part).strip()
    if not qualifier:
        return None, None, False
    unit_text = f' {unit}' if unit else ''
    statement = (f"In fiscal year {proof['fiscal_year']}, {display} for {proof['label']} "
                 f"was {proof['value']}{unit_text}. {qualifier} [{index}]")
    return statement, metric, countable


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
            statement, metric, countable = _statement(proof, index)
            if statement and _question_matches(question, proof, metric, countable=countable):
                matches.append(SpreadsheetGrounding(index, proof, statement))
    # Duplicated evidence is still ambiguous; no arbitrary source selection.
    return matches[0] if len(matches) == 1 else None


def spreadsheet_statement_supports(claim, context, citation_index, question):
    if not isinstance(context, SpreadsheetGrounding) or context.citation_index != citation_index:
        return False
    built = _statement(context.proof, citation_index)
    if not built:
        return False
    expected, metric, countable = built
    return bool(expected and metric
                and _question_matches(question or '', context.proof, metric, countable=countable)
                and claim.strip() == expected.rsplit(f'[{citation_index}]', 1)[0].strip())
