"""Transactional append-only event chain; projections can always be rebuilt."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .locking import atomic_write


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class EventLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events(
                  sequence INTEGER PRIMARY KEY,
                  idempotency_key TEXT UNIQUE NOT NULL,
                  request_hash TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  event_hash TEXT UNIQUE NOT NULL);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT,'immutable events'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT,'immutable events'); END;
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        return db

    def append(self, key, event_type, payload, *, actor='workflow', evidence_hashes=()):
        if not key or not event_type or not isinstance(payload, dict):
            raise ValueError('invalid event contract')
        request_hash = digest({'event_type': event_type, 'payload': payload, 'actor': actor,
                               'evidence_hashes': list(evidence_hashes)})
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM events WHERE idempotency_key=?', (key,)).fetchone()
            if existing:
                if existing['request_hash'] != request_hash:
                    raise RuntimeError('IDEMPOTENCY_CONFLICT')
                db.commit()
                return json.loads(existing['event_json'])
            last = db.execute('SELECT sequence,event_hash FROM events ORDER BY sequence DESC LIMIT 1').fetchone()
            event = {'schema_version': 'rsi-event/v2.2', 'event_id': digest({'key': key, 'request_hash': request_hash}),
                     'idempotency_key': key, 'sequence': last['sequence'] + 1 if last else 1,
                     'event_type': event_type, 'occurred_at': datetime.now(timezone.utc).isoformat(),
                     'actor': actor, 'evidence_hashes': list(evidence_hashes), 'payload': payload,
                     'previous_event_hash': last['event_hash'] if last else '0' * 64}
            event['event_hash'] = digest(event)
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (event['sequence'], key, request_hash, canonical(event).decode(), event['event_hash']))
            db.commit()
            return event
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def read(self):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM events ORDER BY sequence').fetchall()
        previous, result = '0' * 64, []
        for number, row in enumerate(rows, 1):
            event = json.loads(row['event_json'])
            body = {key: value for key, value in event.items() if key != 'event_hash'}
            request_hash = digest({k: event[k] for k in ('event_type', 'payload', 'actor', 'evidence_hashes')})
            if (event['sequence'] != number or row['sequence'] != number
                    or event['previous_event_hash'] != previous or digest(body) != event['event_hash']
                    or event['event_hash'] != row['event_hash'] or request_hash != row['request_hash']
                    or event['idempotency_key'] != row['idempotency_key']):
                raise RuntimeError('EVENT_CHAIN_INVALID')
            previous = event['event_hash']
            result.append(event)
        return result

    def anchor(self, path):
        events = self.read()
        value = {'sequence': len(events), 'event_hash': events[-1]['event_hash'] if events else '0' * 64}
        atomic_write(path, canonical(value))
        return value

    def verify_anchor(self, path):
        anchor = json.loads(Path(path).read_text(encoding='utf-8'))
        events = self.read()
        if (anchor['sequence'] > len(events)
                or (events[anchor['sequence']-1]['event_hash'] if anchor['sequence'] else '0' * 64) != anchor['event_hash']):
            raise RuntimeError('EVENT_ANCHOR_MISMATCH')
        return True
