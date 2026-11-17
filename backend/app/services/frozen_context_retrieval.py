"""Deterministic offline context completion; accepts runtime evidence, never evaluation data.

Document text is supplied by the caller. Selected spans are exact slices of that
text, including paragraph boundaries; selection does not rewrite source claims.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re


def terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9][a-z0-9_./-]*", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words - {"the", "and", "what", "for", "was", "with", "from", "this", "that", "its", "are", "how", "does", "were", "which", "when", "have", "has", "our", "your", "can", "into", "including"}


def _spans(text: str):
    """Whole paragraphs, or whole table rows with their complete header block."""
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    i = 0
    heading = None
    remembered_table_headers = []
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        start = i
        if lines[i].lstrip().startswith("#"):
            heading = (offsets[i], offsets[i] + len(lines[i]), i + 1, i + 1)
        if "\t" in lines[i] or lines[i].count("|") >= 2:
            end = i
            while end + 1 < len(lines) and ("\t" in lines[end + 1] or lines[end + 1].count("|") >= 2):
                end += 1
            headers = [(offsets[k], offsets[k] + len(lines[k]), k + 1, k + 1)
                       for k in range(i, min(i + 7, end + 1))]
            separators = [k for k in range(i, min(i + 7, end + 1)) if re.fullmatch(r"[\s|:\-\t]+", lines[k])]
            if separators:
                headers = headers[:separators[0] - i + 1]
            if lines[i].count("|") >= 2:
                if re.search(r"claim.?id|approved.language|approved.response|\bID\s*\||\bowner\b.*\bstatus\b|\bcolumn", lines[i], re.I) and not re.match(r"\s*[A-Z]+-\d+", lines[i]):
                    remembered_table_headers = headers
                elif remembered_table_headers:
                    headers = remembered_table_headers
            for k in range(i, end + 1):
                row = (offsets[k], offsets[k] + len(lines[k]), k + 1, k + 1)
                yield list(dict.fromkeys(([heading] if heading else []) + [h for h in headers if h != row] + [row]))
            i = end + 1
            continue
        while i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].lstrip().startswith("#") and "\t" not in lines[i + 1]:
            i += 1
        end = offsets[i] + len(lines[i])
        context = [heading] if heading and heading[0] < offsets[start] else []
        if heading and text[heading[0]:heading[1]].startswith("# Page ") and len(re.findall(r"\d+", text[offsets[start]:end])) >= 3:
            # Keep the page's short report/table title and column/unit blocks.
            for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*\n?", text[heading[1]:offsets[start]]):
                raw = match.group()
                if len(raw) > 250 or len(re.findall(r"\d+", raw)) > 3:
                    break
                a, b = heading[1] + match.start(), heading[1] + match.end()
                context.append((a, b, text.count('\n', 0, a) + 1, text.count('\n', 0, b) + 1))
        yield list(dict.fromkeys(context + [(offsets[start], end, start + 1, i + 1)]))
        i += 1


def complete_context(*, question: str, messages: list[dict], documents: list[dict], max_extra_chars: int | None = None) -> dict:
    """Rank ONLY supplied, provenance-authorized documents and append bounded evidence.

    Each document requires text, source, provenance and optionally original_label.
    Missing documents are reported by the source reader, never synthesized here.
    The caller must not pass Gold, expected answers, or per-case source lookups.
    """
    original = copy.deepcopy(messages)
    total = sum(len(str(m.get("content", ""))) for m in original)
    budget = min(6000, total // 10)
    if max_extra_chars is not None:
        budget = min(budget, max(0, max_extra_chars))
    query = terms(question)
    if re.search(r"\bcurrent ratio\b", question, re.I):
        query |= {"assets", "liabilities"}
    # Pronoun follow-ups can use recent user turns, never previous assistant claims.
    if len(query) < 4 or re.search(r"\b(its|previous|same|it)\b|同一|上述", question, re.I):
        prior = [m.get("content", "") for m in original if m.get("role") == "user"]
        query |= terms(" ".join(prior[-3:]))
    existing = "\n".join(str(m.get("content", "")) for m in original if m.get("role") == "system")
    candidates = []
    corpus_text = "\n".join(d.get("text", "").lower() for d in documents)
    frequency = {t: corpus_text.count(t.lower()) for t in query}
    weights = {t: max(.15, 1 + math.log((len(corpus_text) / 500 + 1) / (frequency[t] + .5))) for t in query}
    table_names = set(re.findall(r"\bT\d{2}\b", question, re.I))
    wants_closure = bool(re.search(r"clos|final|follow.up|prevent|最后|最终|承诺", question, re.I))
    specific = terms(" ".join(re.findall(r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z]*|[A-Z]{2,}[A-Z0-9]*|[A-Z][a-z]{2,}|[a-z]+\.[a-z.]+|[a-z]+-[a-z]+-\d+)\b", question))) - {"what", "during", "round", "explain", "how", "for", "the", "in"}
    focus_scores = {d["source"]: sum(weights.get(t, 1) for t in specific & terms(d.get("text", "") + " " + d["source"].replace("_", " ").replace("-", " "))) for d in documents}
    max_focus = max(focus_scores.values(), default=0)
    for d in documents:
        if not d.get("provenance") or not d.get("text"):
            continue
        if max_focus and focus_scores[d["source"]] < .6 * max_focus:
            continue
        text = d["text"]
        title_terms = terms(d["source"].rsplit("/", 1)[-1].replace("-", " ").replace("_", " "))
        document_overlap = query & terms(text)
        document_score = sum(weights[t] for t in document_overlap) / max(sum(weights.values()), 1)
        for spans in _spans(text):
            if table_names and text[spans[0][0]:spans[0][1]].startswith("# Sheet "):
                if not any(re.search(r"\b" + re.escape(t) + r"\b", text[spans[0][0]:spans[0][1]], re.I) for t in table_names):
                    continue
            body = "".join(text[a:b] for a, b, _, _ in spans)
            overlap = query & terms(body)
            if wants_closure and re.search(r"clos|final|follow.up|prevent|resolved|agreed", body, re.I):
                overlap |= query & title_terms
            row_overlap = query & terms(text[spans[-1][0]:spans[-1][1]])
            if "\t" in body and not row_overlap:
                continue
            if not overlap or all(text[a:b].strip() in existing for a, b, _, _ in spans):
                continue
            score = sum(weights[t] for t in overlap) + 2 * document_score + 2 * sum(weights[t] for t in query & title_terms)
            if "\t" in body:
                score += 4 * sum(weights[t] for t in row_overlap)
            if re.search(r"\bcurrent ratio\b", question, re.I) and re.search(r"total current (?:assets|liabilities)", body, re.I):
                score += 20
            # Closure boost is restricted to the SAME authorized source document.
            if wants_closure and re.search(r"clos|final|follow.up|prevent|resolved|agreed", body, re.I):
                score += 3
                score += spans[-1][0] / max(len(text), 1)
            candidates.append((-score, d["source"], spans[-1][0], d, spans, overlap))
    candidates.sort(key=lambda x: x[:3])
    labels = [int(x) for x in re.findall(r"\[(\d+)\]", existing)]
    next_label = max(labels, default=0) + 1
    intro = "NEW_CONTEXT: supplementary source excerpts. Original evidence and history remain unchanged.\n"
    rendered, selected, used = [], [], set()
    while candidates:
        _, _, _, d, spans, _ = candidates.pop(0)
        spans = [s for s in spans if (d["source"], s[0], s[1]) not in used]
        if not spans:
            continue
        label = d.get("original_label") or next_label
        chunks = []
        for a, b, first, last in spans:
            raw = d["text"][a:b]
            chunks.append(f"[lines {first}-{last}]\n{raw}")
        block = f"\n[{label}] 来源：{d['source'].rsplit('/', 1)[-1]}\n" + "\n".join(chunks)
        if len(intro) + sum(map(len, rendered)) + len(block) > budget:
            continue
        rendered.append(block)
        used.update((d["source"], s[0], s[1]) for s in spans)
        selected.append({"label": label, "source": d["source"], "provenance": d["provenance"],
                         "source_sha256": d.get("sha256"), "extraction": d.get("extraction", "supplied_text"),
                         "source_chars": len(d["text"]), "spans": [dict(start=a, end=b, line_start=f, line_end=l, span_sha256=hashlib.sha256(d['text'][a:b].encode()).hexdigest()) for a,b,f,l in spans],
                         "selected_chars": sum(b-a for a,b,_,_ in spans)})
        if not d.get("original_label"):
            next_label += 1
    supplement = intro + "".join(rendered) if rendered else ""
    out = copy.deepcopy(original)
    if supplement:
        at = next((i for i, m in enumerate(out) if m.get("role") != "system"), len(out))
        out.insert(at, {"role": "system", "content": supplement})
    sources = []
    for d in documents:
        intervals = sorted((a,b) for src,a,b in used if src == d["source"])
        sources.append({"source": d["source"], "source_chars": len(d.get("text", "")),
                        "selected_chars": sum(b-a for a,b in intervals),
                        "omitted_chars": len(d.get("text", "")) - sum(b-a for a,b in intervals)})
    return {"messages": out, "arm": "NEW_CONTEXT", "added_chars": len(supplement), "budget_chars": budget,
            "selected": selected, "sources": sources, "unresolved": [] if selected else ["No additional complete matching source span fits the evidence budget."]}
