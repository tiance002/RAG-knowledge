"""Bounded request-local evidence files; handles never accept filesystem paths.

Only already registered canonical JSON is saved. Index previews are explicitly
incomplete and are never evidence-delivery receipts. No model calls occur here.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from app.services.agent_source_registry import RequestSourceRegistry
from app.services.agent_evidence_projection import _readable_title, _semantic_metadata

logger = logging.getLogger(__name__)


class ToolResultStoreError(ValueError):
    pass


def _context(citations):
    return '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}"
                       for c in citations)


class AgentToolResultStore:
    """Single-request store. ``root`` is trusted server configuration only.

    POSIX permissions are 0700/0600; on Windows the directory inherits the
    configured root's ACL. Cleanup unlinks only files created by this instance.
    """

    def __init__(self, registry, *, root=None, max_bytes=8 * 1024 * 1024, max_entries=32):
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_entries) is not int or max_entries <= 0:
            raise ToolResultStoreError('Invalid store limits')
        self.registry = registry if isinstance(registry, RequestSourceRegistry) else RequestSourceRegistry(registry)
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._directory = Path(tempfile.mkdtemp(prefix='rag-agent-results-', dir=root))
        os.chmod(self._directory, 0o700)
        self._entries = {}
        self._hashes = {}
        self._bytes = 0
        self._closed = False
        self._handle_prefix = str(secrets.randbits(64) or 1)

    def update_registry(self, registry):
        """Refresh the same request's canonical list after workflow state copying."""
        self.registry = registry if isinstance(registry, RequestSourceRegistry) else RequestSourceRegistry(registry)

    def _entry(self, handle):
        if self._closed or not isinstance(handle, str) or not re.fullmatch(r'r[1-9][0-9]{0,31}', handle):
            raise ToolResultStoreError('Unknown request-local result handle')
        entry = self._entries.get(handle)
        if entry is None:
            raise ToolResultStoreError('Unknown request-local result handle')
        return entry

    def _validate(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('citations'), list):
            raise ToolResultStoreError('Expected registered canonical evidence')
        selected = []
        for citation in data['citations']:
            if not isinstance(citation, dict):
                raise ToolResultStoreError('Invalid citation')
            if citation.get('content_type') == 'image' and citation.get('content') in ('', None):
                continue
            source = self.registry.resolve(citation.get('source_id'))
            if source is None or source != citation or not isinstance(citation.get('content'), str) or not citation['content']:
                raise ToolResultStoreError('Evidence is not registered in this request')
            selected.append(citation)
        if (not selected or data.get('source_ids') != [c['source_id'] for c in selected]
                or data.get('context') != _context(selected)):
            raise ToolResultStoreError('Canonical evidence mapping mismatch')
        return selected

    def save(self, canonical_json):
        if self._closed or not isinstance(canonical_json, str):
            raise ToolResultStoreError('Store closed or payload is not text')
        raw = canonical_json.encode('utf-8')
        if len(raw) > self.max_bytes:
            raise ToolResultStoreError('Tool result exceeds disk budget')
        try:
            sources = self._validate(json.loads(canonical_json))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ToolResultStoreError('Invalid evidence JSON') from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest in self._hashes:
            handle = self._hashes[digest]
            self.resolve(handle)  # Never reuse a corrupted saved object.
            return self.index(handle)
        if len(self._entries) >= self.max_entries or self._bytes + len(raw) > self.max_bytes:
            raise ToolResultStoreError('Request tool-result disk budget exhausted')
        handle = f'r{self._handle_prefix}{len(self._entries) + 1:04d}'
        path = self._directory / (handle + '.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._entries[handle] = {'path': path, 'sha256': digest, 'bytes': len(raw),
                                 'source_ids': [c['source_id'] for c in sources]}
        self._hashes[digest] = handle
        self._bytes += len(raw)
        return self.index(handle)

    def resolve(self, handle):
        entry = self._entry(handle)
        try:
            fd = os.open(entry['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != entry['bytes']:
                    raise ToolResultStoreError('Saved evidence size/type mismatch')
                raw = stream.read(entry['bytes'] + 1)
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ToolResultStoreError('Saved evidence hash mismatch')
            return raw.decode('utf-8')
        except OSError as exc:
            raise ToolResultStoreError('Saved evidence unavailable') from exc

    def _sources(self, handle):
        data = json.loads(self.resolve(handle))
        return data, self._validate(data)

    def _source_index(self, citation, preview_chars=80):
        # Whitelist locators: no raw backend IDs, paths or registry tables.
        view = _semantic_metadata(citation)
        result = {'source_id': self.registry.alias_for(citation['source_id']),
                  'title': str(_readable_title(citation) or citation.get('title') or 'Source')[:160],
                  'body_chars': len(citation['content']),
                  'preview': citation['content'][:preview_chars],
                  'preview_status': 'EXTRACTIVE_INCOMPLETE_NOT_FULL_EVIDENCE'}
        for key in ('url', 'page_number', 'heading_text', 'content_type', 'source_metadata',
                    'source_locators', 'source_locator'):
            if key in view:
                candidate = {**result, key: view[key]}
                if len(json.dumps(candidate, ensure_ascii=False)) <= 1024:
                    result[key] = view[key]
                else:
                    result['locator_metadata_omitted'] = True
        return result

    def index(self, handle):
        _, sources = self._sources(handle)
        entry = self._entry(handle)
        return {'result_handle': handle, 'sha256': entry['sha256'], 'utf8_bytes': entry['bytes'],
                'tool_result_status': 'SAVED_INCOMPLETE', 'delivered_source_ids': [],
                'sources': [self._source_index(c) for c in sources[:10]],
                'total_sources': len(sources), 'available_source_count': len(sources),
                'next_source_offset': 10 if len(sources) > 10 else None,
                'instruction': 'Use read_tool_result with this result_handle and source_ids or offset/limit source paging (including sources beyond this first index page). Previews are incomplete; do not treat them as fully read evidence.'}

    def metadata(self, handle):
        data, _ = self._sources(handle)
        return {key: value for key, value in data.items() if key not in {'context', 'citations', 'source_ids'}}

    def read(self, handle, source_ids=None, *, offset=0, limit=3, max_chars=16000):
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20
                or type(max_chars) is not int or not 1 <= max_chars <= 16000):
            raise ToolResultStoreError('Invalid bounded source paging')
        data, sources = self._sources(handle)
        if source_ids is not None:
            if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 20:
                raise ToolResultStoreError('Invalid source selection')
            selected_ids = []
            for alias in source_ids:
                source = self.registry.resolve(alias)
                if source is None or source['source_id'] not in self._entry(handle)['source_ids']:
                    raise ToolResultStoreError('Source is not part of this saved result')
                if source['source_id'] not in selected_ids:
                    selected_ids.append(source['source_id'])
            sources = [c for c in sources if c['source_id'] in selected_ids]
        total = len(sources)
        page = sources[offset:offset + limit]
        selected, cannot_fit = [], []
        for citation in page:
            # Keep whole bodies. Never call a substring a delivered source.
            candidate = selected + [citation]
            if len(json.dumps({'context': _context(candidate), 'citations': candidate}, ensure_ascii=False)) > max_chars:
                cannot_fit.append(self._source_index(citation, preview_chars=0))
            else:
                selected.append(citation)
        ids = [c['source_id'] for c in selected]
        return {**{key: data[key] for key in ('retrieval_status', 'expansion_delivery_status', 'omitted_images') if key in data},
                'tool_result_read': True, 'result_handle': handle,
                'context': _context(selected), 'citations': selected, 'source_ids': ids,
                'delivered_source_ids': ids, 'cannot_fit': cannot_fit,
                'read_status': 'CANNOT_FIT_WHOLE_SOURCE' if cannot_fit else 'FULL_SELECTED_SOURCES',
                'next_offset': offset + len(page) if offset + len(page) < total else None,
                'available_source_ids': [self.registry.alias_for(c['source_id']) for c in sources]}

    def close(self):
        """Best-effort cleanup never replaces a cancellation or business error."""
        self._closed = True
        failures = 0
        for entry in self._entries.values():
            try:
                entry['path'].unlink(missing_ok=True)
            except OSError:
                failures += 1
        # No recursive deletion: unexpected foreign files are never removed.
        try:
            self._directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            failures += 1
        if failures:
            logger.warning('Request tool-result cleanup incomplete: %s operations failed', failures)
        return {'cleanup_status': 'INCOMPLETE' if failures else 'COMPLETE', 'failed_operations': failures}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
