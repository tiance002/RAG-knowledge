"""Read-only historical model receipts for DEV debugging, never live generation.

``load_archive(path)`` returns parent paths and ``roles[role]`` containing
``public``, ``envelope``, ``usage``, ``historical_cost_nano_upper`` and bound
original request/response/transport references. No identifiers are rewritten.
``freeze_archive(source_root, output)`` binds the genuine matched-k3 archive.
This module neither executes generated code nor opens a provider connection.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


SCHEMA = 'rsi-dev-debug-archive/v1'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def _ref(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': _sha(path.read_bytes())}


def _read(ref):
    data = Path(ref['path']).read_bytes()
    if _sha(data) != ref['sha256']:
        raise ValueError('ARCHIVE_SOURCE_CHANGED')
    return data


def _envelope(receipt):
    if receipt.get('transport_status') != 'RECEIVED' or receipt.get('http_status') != 200:
        raise ValueError('ARCHIVE_TRANSPORT_NOT_SUCCESSFUL')
    response = receipt['response']
    if response.get('status') != 'completed':
        raise ValueError('ARCHIVE_RESPONSE_NOT_COMPLETED')
    texts = [c['text'] for item in response['output']
             if item.get('type') == 'message' and item.get('phase') == 'final_answer'
             for c in item.get('content', []) if c.get('type') == 'output_text']
    if len(texts) != 1:
        raise ValueError('ARCHIVE_ENVELOPE_AMBIGUOUS')
    envelope = json.loads(texts[0])
    if envelope.get('status') not in ('OK', 'ABSTAIN'):
        raise ValueError('ARCHIVE_ENVELOPE_INVALID')
    return envelope


def freeze_archive(source_root, output):
    """Freeze metadata only; original receipts, ledger and worker remain read-only."""
    root = Path(source_root).resolve()
    round_ref = _ref(root/'matched-k3/round.json')
    record = json.loads(_read(round_ref))
    registry = root/'query-aware-v1/registry'
    parent = {'candidate_id': record['parent_id']}
    for key, directory in (('worker', 'workers'), ('config', 'configs')):
        ref = _ref(registry/directory/record[f'parent_{key}_sha256'])
        if ref['sha256'] != record[f'parent_{key}_sha256']:
            raise ValueError('ARCHIVE_PARENT_HASH_MISMATCH')
        parent[f'{key}_path'], parent[f'{key}_sha256'] = ref['path'], ref['sha256']
    candidate_ref = _ref(registry/'candidates'/parent['candidate_id'])
    if candidate_ref['sha256'] != parent['candidate_id']:
        raise ValueError('ARCHIVE_CANDIDATE_HASH_MISMATCH')
    ledger = root/'task-model-budget.sqlite'
    # SQLite URI mode=ro prevents creating or modifying the historical ledger.
    with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True) as db:
        events = [json.loads(row[0]) for row in db.execute('SELECT event_json FROM events')]
    roles = {}
    for role in ('diagnose', 'P1_DEDUP', 'P2_BUDGET', 'P3_ORDER', 'curator', 'meta'):
        directory = root/('matched-k3-advisory-roles' if role in ('curator', 'meta') else 'matched-k3-roles')
        files = list(directory.glob(f'*-{role}-0.request.json'))
        if len(files) != 1:
            raise ValueError('ARCHIVE_REQUEST_AMBIGUOUS')
        request_path = files[0]
        key = request_path.name.removesuffix('.request.json')
        refs = {'request': _ref(request_path), 'response': _ref(directory/(key+'.json')),
                'transport': _ref(directory/(key+'.transport.json'))}
        request = json.loads(_read(refs['request']))
        response = json.loads(_read(refs['response']))
        transport = json.loads(_read(refs['transport']))
        if response != transport:
            raise ValueError('ARCHIVE_TRANSPORT_RECEIPT_MISMATCH')
        payloads = [json.loads(x['content']) for x in request['input'] if x['role'] == 'user']
        if len(payloads) != 1:
            raise ValueError('ARCHIVE_PUBLIC_AMBIGUOUS')
        starts = [e for e in events if e['event_type'] == 'MODEL_REQUEST_STARTED' and e['payload'].get('request_key') == key]
        ends = [e for e in events if e['event_type'] == 'MODEL_REQUEST_COMPLETED' and e['payload'].get('request_key') == key]
        if len(starts) != 1 or len(ends) != 1:
            raise ValueError('ARCHIVE_LEDGER_BINDING_MISSING')
        start, end = starts[0], ends[0]
        usage = response['response']['usage']
        if (start['payload']['request_hash'] != _sha(_canonical(request))
                or end['payload']['usage'] != usage
                or end['payload']['provider_request_id'] != response['provider_request_id']):
            raise ValueError('ARCHIVE_LEDGER_BINDING_MISMATCH')
        roles[role] = {'refs': refs, 'public': payloads[0]['public'],
                       'original_diagnosis': payloads[0].get('diagnosis'),
                       'envelope': _envelope(response), 'usage': usage,
                       'request_key': key, 'model': request['model'],
                       'ledger_start': start, 'ledger_completion': end,
                       'historical_cost_nano_upper': end['payload']['cost_nano_upper']}
    bundle = {'schema': SCHEMA, 'evidence_mode': 'ARCHIVED_MODEL_RECEIPTS',
              'source_role_profile': 'legacy/v1',
              'profile_provenance': 'Original P3_ORDER request and legacy policy; profile field absent in historical round; inferred, never M1_SPECIALIZED_V1.',
              'source_round_id': record['round_id'], 'seam_profile': record['seam_profile'],
              'round_ref': round_ref, 'parent_candidate_ref': candidate_ref,
              'parent': parent, 'roles': roles, 'ledger_path': str(ledger),
              'new_paid_calls': 0, 'historical_usage_recharged': False,
              'limitations': ['Historical context and aliases remain original.',
                              'Advisory receipts refer to their original completed round; replay does not re-adjudicate them.',
                              'Not a live Specialized-profile experiment or evidence of current DEV gain.']}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(_canonical(bundle))
    return load_archive(output)


def load_archive(path):
    """Validate frozen source bytes and return original role publics/envelopes."""
    bundle = json.loads(Path(path).read_bytes())
    if (bundle.get('schema') != SCHEMA or bundle.get('source_role_profile') != 'legacy/v1'
            or bundle.get('evidence_mode') != 'ARCHIVED_MODEL_RECEIPTS'
            or bundle.get('new_paid_calls') != 0 or bundle.get('historical_usage_recharged') is not False):
        raise ValueError('ARCHIVE_DEBUG_SCOPE_REQUIRED')
    original_round = json.loads(_read(bundle['round_ref']))
    candidate = json.loads(_read(bundle['parent_candidate_ref']))
    if bundle['source_round_id'] != original_round['round_id']:
        raise ValueError('ARCHIVE_ROUND_CHANGED')
    for key in ('worker', 'config'):
        _read({'path': bundle['parent'][key+'_path'], 'sha256': bundle['parent'][key+'_sha256']})
        if (bundle['parent'][key+'_sha256'] != original_round['parent_'+key+'_sha256']
                or candidate[key+'_sha256'] != bundle['parent'][key+'_sha256']):
            raise ValueError('ARCHIVE_PARENT_BINDING_MISMATCH')
    if (bundle['parent']['candidate_id'] != original_round['parent_id']
            or bundle['parent_candidate_ref']['sha256'] != bundle['parent']['candidate_id']
            or set(bundle['roles']) != {'diagnose', 'P1_DEDUP', 'P2_BUDGET', 'P3_ORDER', 'curator', 'meta'}):
        raise ValueError('ARCHIVE_ROLES_OR_PARENT_CHANGED')
    ledger = Path(bundle['ledger_path']).resolve()
    with sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True) as db:
        ledger_events = {json.loads(row[0])['event_id']: json.loads(row[0])
                         for row in db.execute('SELECT event_json FROM events')}
    for role, item in bundle['roles'].items():
        request = json.loads(_read(item['refs']['request']))
        response = json.loads(_read(item['refs']['response']))
        transport = json.loads(_read(item['refs']['transport']))
        public = [json.loads(x['content']) for x in request['input'] if x['role'] == 'user'][0]
        if (item['envelope'] != _envelope(response) or item['public'] != public['public']
                or item['original_diagnosis'] != public.get('diagnosis')
                or response != transport or item['usage'] != response['response']['usage']):
            raise ValueError('ARCHIVE_DERIVED_DATA_CHANGED')
        start, end = item['ledger_start']['payload'], item['ledger_completion']['payload']
        for event in (item['ledger_start'], item['ledger_completion']):
            event_body = {k: v for k, v in event.items() if k != 'event_hash'}
            if (event['event_hash'] != _sha(_canonical(event_body))
                    or ledger_events.get(event['event_id']) != event):
                raise ValueError('ARCHIVE_LEDGER_EVENT_CHANGED')
        if (start['request_hash'] != _sha(_canonical(request)) or start['role'] != role
                or end['usage'] != item['usage'] or end['request_key'] != item['request_key']
                or end['provider_request_id'] != response['provider_request_id']
                or end['cost_nano_upper'] != item['historical_cost_nano_upper']):
            raise ValueError('ARCHIVE_LEDGER_BINDING_MISMATCH')
    return bundle


def role_receipt(bundle, role):
    """Return the original public and envelope without rebinding historical aliases."""
    item = bundle['roles'][role]
    return item['public'], item['envelope']
