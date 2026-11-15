"""Request-local web evidence identities and explicit historical snapshots.

Captured search snippets are snapshots, never proof of current page contents.
History candidates must already come from the server-owned conversation loader.
"""
from datetime import datetime, timezone
import hashlib
import re

from app.services.agent_source_registry import RequestSourceRegistry, source_alias
from app.services.web_search import _usable_url


def _hash(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def validate_web_snapshot(reference):
    if not isinstance(reference, dict) or reference.get('source_type') != 'web':
        return None
    body, title = reference.get('content'), reference.get('title')
    if (not _usable_url(reference.get('url')) or not isinstance(title, str) or not title.strip()
            or not isinstance(body, str) or not body.strip()
            or reference.get('web_snapshot_status') != 'CAPTURED_SEARCH_RESULT'
            or reference.get('content_hash') != _hash(body)):
        return None
    # A prior turn's source_id/alias is not an identity in this request.
    result = {key: value for key, value in reference.items()
              if key not in {'source_id', 'source_alias'}}
    result.update(fileName=title, kbName='互联网', location=reference['url'],
                  verification_status='SNAPSHOT_ONLY')
    return result


def build_web_registry(current, history, query):
    """Preserve fresh ranking; append at most three related exact snapshots."""
    from app.core.config import get_settings
    from app.services.bm25 import tokenize
    now = datetime.now(timezone.utc).isoformat()
    entries, seen = [], set()

    def append(candidate):
        identity = (candidate['url'], candidate['content_hash'])
        if identity in seen:
            return False
        seen.add(identity)
        sid = len(entries) + 1
        entries.append({**candidate, 'source_id': sid, 'source_alias': source_alias(sid)})
        return True

    for row in current or []:
        if not isinstance(row, dict):
            continue
        body = row.get('content')
        if not isinstance(body, str):
            continue
        candidate = validate_web_snapshot({**row, 'content_hash': _hash(body),
            'web_snapshot_status': 'CAPTURED_SEARCH_RESULT'})
        if candidate:
            append({**candidate, 'origin': 'CURRENT_WEB_SEARCH', 'fetched_at': now})

    tokens = set(tokenize(query))
    explicit_followup = bool(re.search(
        r'刚才|上述|上面|上一条|前面|\b(?:previous|that)\s+(?:source|page|result)\b',
        query, re.IGNORECASE))
    # Deliberately narrow complete forms: an explicit new topic must not become
    # a pronoun followup merely because a sentence also contains "it" or 它.
    pronoun_followup = bool(re.fullmatch(
        r'\s*(?:它(?:具体)?(?:是什么|有什么限制|的限制是什么|有哪些局限)[？?。!！]*'
        r'|what\s+about\s+its\s+limitations[?!.]*|what\s+is\s+it[?!.]*)\s*',
        query, re.IGNORECASE))
    valid_history = [candidate for row in history or []
                     if (candidate := validate_web_snapshot(row)) is not None]
    latest_turn = valid_history[-1].get('history_turn_id') if valid_history else None
    ranked = []
    for index, candidate in enumerate(valid_history):
        if not tokens:
            continue
        overlap = len(tokens & set(tokenize(candidate['title'] + '\n' + candidate['content']))) / len(tokens)
        if (explicit_followup or pronoun_followup) and latest_turn is not None and candidate.get('history_turn_id') == latest_turn:
            overlap = max(overlap, 0.5)
            candidate = {**candidate, 'selection_reason': (
                'LATEST_TURN_PRONOUN_HEURISTIC' if pronoun_followup else 'LATEST_TURN_EXPLICIT_REFERENCE')}
        if overlap >= 0.5:
            ranked.append((overlap, index, candidate))
    used = sum(len(row['content']) for row in entries)
    added = 0
    for _, _, candidate in sorted(ranked, key=lambda row: (row[0], row[1]), reverse=True):
        if added >= 3 or used + len(candidate['content']) > get_settings().CONTEXT_MAX_CHARS:
            continue
        if append({**candidate, 'origin': 'HISTORY_WEB_SNAPSHOT'}):
            used += len(candidate['content'])
            added += 1
    return RequestSourceRegistry(entries)
