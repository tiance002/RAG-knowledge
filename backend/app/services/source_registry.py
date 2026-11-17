"""Optional, explicitly pinned source manifest for legacy corpus verification."""
import hashlib
import json
import os
from pathlib import Path
import re


def trusted_source_hash(source_path):
    """Resolve a file digest from an operator-pinned source-only manifest.

    This does not manufacture historical lineage or read question/Gold files.
    With no configuration or inconsistent provenance it authorizes nothing.
    """
    path = os.getenv('RAG_SOURCE_MANIFEST')
    expected = os.getenv('RAG_SOURCE_MANIFEST_SHA256', '')
    if not path or not re.fullmatch('[0-9a-f]{64}', expected):
        return None
    try:
        manifest_path = Path(path)
        if manifest_path.stat().st_size > 1_000_000:
            return None
        data = manifest_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            return None
        manifest = json.loads(data)
        if set(manifest) != {'schema', 'sources'} or manifest['schema'] != 'verified-source-files/v1':
            return None
        target = Path(source_path).resolve()
        matches = [entry['sha256'] for entry in manifest['sources']
                   if set(entry) == {'path', 'sha256'} and Path(entry['path']).resolve() == target]
        return matches[0] if len(matches) == 1 and re.fullmatch('[0-9a-f]{64}', matches[0]) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None
