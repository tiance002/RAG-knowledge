"""Bounded local lexical variants; never call a model or translate a query."""

from collections import Counter
from typing import Iterable, List

from app.services.bm25 import tokenize


def distinct_queries(queries: Iterable[str]) -> List[str]:
    """Keep the original spelling and order, removing whitespace-only duplicates."""
    result = []
    seen = set()
    for query in queries:
        query = (query or "").strip()
        key = " ".join(query.split()).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(query)
    return result


def expand_queries(queries: Iterable[str], max_queries: int = 5) -> List[str]:
    """Extract stopword-filtered Jieba/English terms using the existing tokenizer.

    Frequency and first occurrence select at most eight terms per source query.
    Keyword-only, split-keyword and individual-keyword variants relax lexical
    matching without inventing facts, synonyms or cross-language translations.
    """
    base = distinct_queries(queries)
    limit = min(5, max(0, max_queries))
    proposals = []
    for query in base:
        tokens = tokenize(query)
        counts = Counter(tokens)
        terms = sorted(counts, key=lambda term: (-counts[term], tokens.index(term)))[:8]
        if not terms:
            continue
        proposals.append(" ".join(terms))
        if len(terms) > 2:
            middle = (len(terms) + 1) // 2
            proposals.extend((" ".join(terms[:middle]), " ".join(terms[middle:])))
        proposals.extend(terms)
    base_keys = {" ".join(q.split()).casefold() for q in base}
    return [q for q in distinct_queries(proposals)
            if " ".join(q.split()).casefold() not in base_keys][:limit]
