"""Explicit hash-bound readers without write-capable RSI constructors."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from datetime import datetime


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def _file(path, expected=None):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('ANALYTICS_NOT_A_FILE')
    actual = sha256(path.read_bytes()).hexdigest()
    if expected is not None and (not re.fullmatch('[0-9a-f]{64}', expected) or expected != actual):
        raise ValueError('ANALYTICS_SOURCE_HASH_MISMATCH')
    return path, actual


def read_event_log(path, expected_sha256=None):
    path, checksum = _file(path, expected_sha256)
    def reject_sidecars():
        for suffix in ('-wal', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError('ANALYTICS_SOURCE_NOT_FROZEN')
    reject_sidecars()
    # Frozen inputs only. immutable prevents SQLite from creating SHM files.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute("SELECT type FROM sqlite_master WHERE name='events'").fetchone() != ('table',):
            raise ValueError('ANALYTICS_EVENT_SCHEMA_INVALID')
        schema = db.execute('PRAGMA table_info(events)').fetchall()
        expected = [('sequence', 'INTEGER'), ('idempotency_key', 'TEXT'), ('request_hash', 'TEXT'),
                    ('event_json', 'TEXT'), ('event_hash', 'TEXT')]
        if ([(r[1], r[2].upper()) for r in schema] != expected or schema[0][5] != 1
                or [r[3] for r in schema] != [0, 1, 1, 1, 1]):
            raise ValueError('ANALYTICS_EVENT_SCHEMA_INVALID')
        rows = db.execute('SELECT sequence,idempotency_key,request_hash,event_json,event_hash FROM events ORDER BY sequence').fetchall()
    # Reject a moving source rather than publish a snapshot with an unrelated hash.
    reject_sidecars()
    if _file(path)[1] != checksum:
        raise ValueError('ANALYTICS_SOURCE_NOT_FROZEN')
    events, previous, identities = [], '0' * 64, set()
    fields = {'schema_version', 'event_id', 'idempotency_key', 'sequence', 'event_type',
              'occurred_at', 'actor', 'evidence_hashes', 'payload', 'previous_event_hash', 'event_hash'}
    for number, (sequence, key, request_hash, raw, event_hash) in enumerate(rows, 1):
        event = json.loads(raw)
        if (not isinstance(event, dict) or set(event) != fields or not isinstance(event['payload'], dict)
                or not isinstance(event['evidence_hashes'], list) or not key
                or event['schema_version'] != 'rsi-event/v2.2'
                or not isinstance(event['actor'], str) or not event['actor']
                or not isinstance(event['occurred_at'], str)
                or not isinstance(event['event_type'], str) or not event['event_type']):
            raise ValueError('ANALYTICS_EVENT_ENVELOPE_INVALID')
        if datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError('ANALYTICS_EVENT_TIMESTAMP_INVALID')
        request = digest({k: event[k] for k in ('event_type', 'payload', 'actor', 'evidence_hashes')})
        body = {k: v for k, v in event.items() if k != 'event_hash'}
        if (type(event['sequence']) is not int or event['sequence'] != number or sequence != number
                or key != event['idempotency_key'] or request != request_hash
                or event['event_id'] != digest({'key': key, 'request_hash': request})
                or event_hash != event['event_hash'] or digest(body) != event_hash
                or event['previous_event_hash'] != previous or event['event_id'] in identities):
            raise ValueError('ANALYTICS_EVENT_CHAIN_INVALID')
        identities.add(event['event_id'])
        events.append(event)
        previous = event_hash
    return {'source_id': 'sha256:' + checksum, 'path': str(path), 'sha256': checksum, 'events': events}


def read_artifact(path, expected_sha256, kind):
    """Read only explicitly supplied public archived metadata, never closed receipts."""
    names = {'round': 'round.json', 'family': 'family-decision.json', 'replay': 'manifest.json'}
    path = Path(path)
    closed = lambda value: any(re.search(r'(^|[-_])(holdout|sealed|closed|gold)([-_.]|$)', part, re.I) for part in value.parts)
    if closed(path):
        raise ValueError('ANALYTICS_CLOSED_ARTIFACT_FORBIDDEN')
    # Resolve links before any file-body read or checksum operation, and check
    # both the requested spelling and the resolved target.
    path = path.resolve(strict=True)
    if kind not in names or path.name != names[kind] or not expected_sha256:
        raise ValueError('ANALYTICS_ARTIFACT_NOT_ALLOWED')
    if closed(path):
        raise ValueError('ANALYTICS_CLOSED_ARTIFACT_FORBIDDEN')
    path, checksum = _file(path, expected_sha256)
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != checksum:
        raise ValueError('ANALYTICS_SOURCE_NOT_FROZEN')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('ANALYTICS_ARTIFACT_INVALID')
    schema_name = str(data.get('schema', data.get('schema_version', '')))
    if re.search(r'holdout|sealed|closed|gold', schema_name, re.I):
        raise ValueError('ANALYTICS_CLOSED_ARTIFACT_FORBIDDEN')
    if kind == 'round' and (not data.get('round_id') or data.get('execution_state') not in {'COMPLETED', 'FAILED'}):
        raise ValueError('ANALYTICS_ROUND_INVALID')
    if kind == 'family':
        from ..family_decision import validate_family
        validate_family(data)
    if kind == 'replay' and (not data.get('run_id') or 'arms' not in data):
        raise ValueError('ANALYTICS_REPLAY_INVALID')
    return {'source_id': 'sha256:' + checksum, 'path': str(path), 'sha256': checksum, 'kind': kind, 'data': data}
