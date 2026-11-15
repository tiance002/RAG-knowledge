"""Short request-local handles over the existing canonical source registry.

The source_id allocated by agent_workflow is the sole identity. This module
neither allocates another ID nor stores another identity table. Aliases encode
that integer reversibly for model-facing tools and citation presentation.
"""
import re

_ALIAS = re.compile(r's([1-9][0-9]*)\Z')
_CITATION_ALIAS = re.compile(r'\[s([1-9][0-9]*)\]')


def source_alias(source_id):
    """Return the exact sN handle; booleans and nonpositive IDs are invalid."""
    if type(source_id) is not int or source_id <= 0:
        raise ValueError('A source alias requires a positive integer source_id')
    return f's{source_id}'


class RequestSourceRegistry:
    """Read-only view of a request's canonical list, including later additions."""

    def __init__(self, entries):
        self.entries = entries if isinstance(entries, list) else []

    def resolve(self, handle):
        """Resolve a registered integer or exact alias; ambiguous IDs fail closed."""
        if type(handle) is int:
            sid = handle
        elif isinstance(handle, str) and (match := _ALIAS.fullmatch(handle)):
            # Bound conversion before int(): arbitrary provider strings must not
            # hit Python's large-decimal conversion limit or allocate giant ints.
            if len(match[1]) > 20:
                return None
            sid = int(match[1])
        else:
            return None
        if sid <= 0:
            return None
        matches = [entry for entry in self.entries
                   if isinstance(entry, dict) and type(entry.get('source_id')) is int
                   and entry['source_id'] == sid]
        return matches[0] if len(matches) == 1 else None

    def alias_for(self, source_id):
        source = self.resolve(source_id)
        return source_alias(source['source_id']) if source is not None else None

    def restore_known_aliases(self, text, delivered_source_ids):
        """Restore known delivered [sN] tokens; unknown tokens remain verbatim.

        This is an identity translation, not an assertion that a cited passage
        supports the answer. It does not create, replace or validate answer facts.
        """
        delivered = {sid for sid in delivered_source_ids or [] if type(sid) is int}
        stats = {'restored_alias_count': 0, 'unknown_alias_count': 0,
                 'undelivered_alias_count': 0}
        if not isinstance(text, str):
            return text, stats

        def restore(match):
            source = self.resolve('s' + match[1])
            if source is None:
                stats['unknown_alias_count'] += 1
                return match[0]
            sid = source['source_id']
            if sid not in delivered:
                stats['undelivered_alias_count'] += 1
                return match[0]
            stats['restored_alias_count'] += 1
            return f'[{sid}]'

        return _CITATION_ALIAS.sub(restore, text), stats


def resolve_source_alias(handle, registry):
    return RequestSourceRegistry(registry).resolve(handle)


def restore_known_source_aliases(text, registry, delivered_source_ids):
    return RequestSourceRegistry(registry).restore_known_aliases(text, delivered_source_ids)
