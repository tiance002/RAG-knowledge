"""Isolated frozen-provider-input replay; never changes production routing switches.

No HTTP/SSE or retrieval claim is made. Gold is never read by this runner. Calls
are serial, no transport retries; every send consumes one of at most 48 durable
attempts. A process crash leaves UNKNOWN_PENDING, which is never sent again.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.model_router import RouterConfig, RULE_VERSION, decide_route, decide_escalation
from run_routing200 import canonical, sha, atomic_json, private, send

CHEAP = 'Qwen/Qwen3.5-4B'
EXPENSIVE = 'zai-org/GLM-5.3-Flash'
ARMS = ('CHEAP_ONLY', 'EXPENSIVE_ONLY', 'DYNAMIC')


def code_identity():
    import app.services.model_router as router
    return {name: sha(Path(path).read_bytes()) for name, path in (
        ('runner', __file__), ('model_router', router.__file__),
        ('grounded_generation', Path(router.__file__).with_name('grounded_generation.py')),
        ('transport_helpers', Path(__file__).with_name('run_routing200.py')))}


def config_for(manifest, arm):
    cfg = dict(manifest.get('router_config', {}))
    cfg.update(mode=arm, cheap_model=CHEAP, expensive_model=EXPENSIVE)
    result = RouterConfig(**cfg)
    if (result.unknown_policy != 'BASELINE' or result.escalate_on_truncated
            or result.escalate_on_provider_fallback or result.escalate_on_unsupported):
        raise ValueError('unapproved runtime policy')
    return result


def request_payload(case, model):
    payload = dict(model=model, messages=case['messages'], temperature=.7,
                   max_tokens=4096, stream=False)
    if model == CHEAP:
        payload['enable_thinking'] = False
    return payload


def validate(manifest):
    selected = manifest.get('execution_arms', list(ARMS))
    if selected not in (list(ARMS), ['DYNAMIC']):
        raise ValueError('unsupported execution arms')
    if selected == ['DYNAMIC'] and not manifest.get('previous_run'):
        raise ValueError('DYNAMIC continuation requires frozen previous run')
    cases = manifest.get('cases', [])
    if len(cases) != 12 or len({c['qid'] for c in cases}) != 12:
        raise ValueError('exactly 12 unique frozen slots required')
    if manifest.get('baseline_model') != CHEAP:
        raise ValueError('isolated baseline must explicitly be Qwen')
    if manifest.get('quality_check', 'NONE') not in ('NONE', 'REFERENCE_RANGE'):
        raise ValueError('unknown quality policy')
    for c in cases:
        if not isinstance(c['qid'], str) or not c['qid'].strip():
            raise ValueError('qid must be a nonempty frozen string')
        if c.get('source_status') in ('MISSING_INPUT', 'NOT_RUN'):
            continue
        if not isinstance(c.get('question'), str) or not c['question'].strip():
            raise ValueError('missing original question')
        messages = c.get('messages')
        if not isinstance(messages, list) or not messages:
            raise ValueError('missing frozen messages')
        for m in messages:
            if (set(m) != {'role', 'content'} or m['role'] not in ('system', 'user', 'assistant')
                    or not isinstance(m['content'], str)):
                raise ValueError('invalid exact messages')
        if sha(canonical(messages)) != c.get('messages_sha256'):
            raise ValueError('frozen message hash mismatch')
        if len(canonical(messages)) + 4096 + 512 >= 256000:
            raise ValueError('conservative context bound exceeded')
        # Missing router inputs remain a planned NOT_RUN slot. Never reconstruct
        # scores, contents or evidence metadata from Gold or retrieval IDs.
        if 'citations' in c and not isinstance(c['citations'], list):
            raise ValueError('invalid delivered citations')
        if any(not isinstance(row, dict) for row in c.get('citations', [])):
            raise ValueError('invalid delivered citation row')
        if c.get('comparison_input_hash') and c['comparison_input_hash'] != comparison_hash(c):
            raise ValueError('comparison input identity mismatch')
    for arm in ARMS:
        config_for(manifest, arm)
    if manifest.get('previous_run'):
        previous_snapshot(manifest)
    return {'status': 'PASS', 'cases': 12, 'planned_arm_slots': 36,
            'maximum_calls': 48, 'model_calls': 0,
            'evaluation_kind': 'FROZEN_PROVIDER_INPUT_REPLAY',
            'manifest_sha256': sha(canonical(manifest)), 'code_sha256': code_identity(),
            'rule_version': RULE_VERSION}


def comparison_hash(case):
    return sha(canonical({k: case.get(k) for k in (
        'messages', 'citations', 'history_messages', 'kb_scope', 'document_scope', 'retrieval_status')}))


def historical_unknowns(manifest):
    """Re-read frozen historical ledgers before *each* send; no implicit reuse.

    A genuinely unresolvable UNKNOWN blocks the whole run. The manifest lists
    the complete owner-reviewed inventory, including a nonempty audit reference.
    """
    if not manifest.get('history_inventory_audit') or 'history_ledgers' not in manifest:
        raise ValueError('historical inventory audit missing')
    unknown = []
    for source in history_sources(manifest):
        path = Path(source['path']).resolve()
        if sha(path.read_bytes()) != source['sha256']:
            raise ValueError('historical ledger changed')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            columns = {r[1] for r in db.execute('pragma table_info(attempts)')}
            role = 'role' if 'role' in columns else 'arm'
            rows = db.execute('select qid,' + role + ",state,info from attempts where upper(state) like '%UNKNOWN%'").fetchall()
        for qid, role, state, encoded in rows:
            info = json.loads(encoded)
            if not (info.get('request_hash') or
                    (info.get('model') and info.get('messages_sha256')) or qid):
                raise ValueError('unresolved historical UNKNOWN identity')
            unknown.append(dict(info, qid=qid, historical_role=role, state=state))
    return unknown


def history_sources(manifest):
    sources = list(manifest['history_ledgers'])
    previous = manifest.get('previous_run')
    if previous and not any(Path(s['path']).resolve() == Path(previous['ledger_path']).resolve()
                            for s in sources):
        sources.append({'path': previous['ledger_path'], 'sha256': previous['ledger_sha256']})
    return sources


def previous_snapshot(manifest):
    """Verify a frozen stopped run, without opening its SQLite file for writing."""
    previous = manifest['previous_run']
    path = Path(previous['ledger_path']).resolve()
    if sha(path.read_bytes()) != previous['ledger_sha256']:
        raise ValueError('previous ledger changed')
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('previous ledger must be a closed frozen snapshot')
    raw = Path(previous['manifest_path']).read_bytes()
    if sha(raw) != previous['manifest_file_sha256']:
        raise ValueError('previous manifest changed')
    old = json.loads(raw.decode('utf-8-sig'))
    # Only bookkeeping/authorization/code-runner changes may differ. Inputs,
    # router policy, endpoint, model settings and all planned slots stay exact.
    keys = ('cases', 'baseline_model', 'router_config', 'quality_check',
            'endpoint_sha256', 'credential_bindings', 'approved_max_calls')
    if any(old.get(k) != manifest.get(k) for k in keys):
        raise ValueError('previous settings or inputs do not match')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
        identity = json.loads(source.execute("select value from metadata where key='identity'").fetchone()[0])
        if identity['manifest_sha256'] != sha(canonical(old)):
            raise ValueError('previous ledger manifest identity mismatch')
        if identity['code_sha256'] != previous['code_sha256']:
            raise ValueError('previous runtime identity mismatch')
        current = code_identity()
        if any(identity['code_sha256'].get(k) != current[k] for k in current if k != 'runner'):
            raise ValueError('previous router/generation/provider semantics changed')
        attempts = source.execute('select qid,arm,step,state,info from attempts').fetchall()
        slots = source.execute('select qid,arm,info from slots').fetchall()
        events = source.execute('select qid,arm,step,state,info,recorded_at from events order by id').fetchall()
    if len(attempts) > 48:
        raise ValueError('previous run already exceeds call cap')
    cases = {case['qid']: case for case in manifest['cases']}
    for qid, arm, step, state, encoded in attempts:
        info = json.loads(encoded)
        if qid not in cases or arm not in ARMS or step not in (0, 1):
            raise ValueError('invalid previous attempt identity')
        case = cases[qid]
        model = info.get('model')
        if model not in (CHEAP, EXPENSIVE):
            raise ValueError('previous attempted unapproved model')
        payload = request_payload(case, model)
        if (info.get('request_hash') != sha(canonical(payload))
                or info.get('request_payload') != payload
                or info.get('messages_sha256') != case['messages_sha256']
                or info.get('endpoint_sha256') != manifest['endpoint_sha256'][model]):
            raise ValueError('previous attempt request identity mismatch')
    for qid, arm, encoded in slots:
        info = json.loads(encoded)
        if (qid not in cases or arm not in ARMS
                or info.get('comparison_input_hash') != comparison_hash(cases[qid])
                or info.get('evaluation_identity') != identity['manifest_sha256']):
            raise ValueError('previous result identity mismatch')
    return dict(identity=identity, attempts=attempts, slots=slots, events=events)


def import_previous(db, manifest, output):
    if not manifest.get('previous_run'):
        return
    if Path(output).resolve() == Path(manifest['previous_run']['ledger_path']).resolve().parent:
        raise ValueError('continuation output must differ from previous run')
    snapshot = previous_snapshot(manifest)
    if db.execute("select 1 from metadata where key='previous_import_complete'").fetchone():
        return
    with db:
        if db.execute('select count(*) from attempts').fetchone()[0] or db.execute('select count(*) from slots').fetchone()[0]:
            raise ValueError('previous import requires fresh continuation ledger')
        db.executemany('insert into attempts values (?,?,?,?,?)', snapshot['attempts'])
        db.executemany('insert into slots values (?,?,?)', snapshot['slots'])
        db.executemany('insert into events(qid,arm,step,state,info,recorded_at) values (?,?,?,?,?,?)',
                       snapshot['events'])
        db.execute("insert into metadata values ('previous_import_complete',?)",
                   (manifest['previous_run']['ledger_sha256'],))


def historical_match(unknown, case, model, payload_hash, endpoint_hash):
    """Missing identity fields broaden ambiguity; they never prove safety."""
    if unknown.get('request_hash') == payload_hash:
        return True
    if (unknown.get('model') == model
            and unknown.get('messages_sha256') == case['messages_sha256']):
        return not unknown.get('endpoint_sha256') or unknown['endpoint_sha256'] == endpoint_hash
    # A known message hash/model mismatch resolves a qid-only ambiguity.
    if unknown.get('messages_sha256') and unknown['messages_sha256'] != case['messages_sha256']:
        return False
    if unknown.get('model') and unknown['model'] != model:
        return False
    def normalized(qid):
        value = str(qid or '')
        while value.split(':', 1)[0] in ('initial', 'followup', 'prior') and ':' in value:
            value = value.split(':', 1)[1]
        return value
    return bool(unknown.get('qid')) and normalized(unknown['qid']) == normalized(case['qid'])


def providers_from_db(path, manifest):
    """Read credentials; an isolated Qwen alias may explicitly reuse a binding.

    Binding names are frozen in the manifest. No production row is inserted or
    modified; model request names always remain the approved Qwen/GLM pair.
    """
    result = {}
    bindings = manifest['credential_bindings']
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        for model in (CHEAP, EXPENSIVE):
            rows = db.execute('select base_url,api_key from custom_models where model_name=?',
                              (bindings[model],)).fetchall()
            if len(rows) != 1:
                raise ValueError('ambiguous provider binding')
            url, key = rows[0]
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.hostname != 'api.siliconflow.cn'
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.port not in (None, 443) or not key):
                raise ValueError('unapproved provider binding')
            url = url.rstrip('/')
            if sha(url.encode()) != manifest['endpoint_sha256'][model]:
                raise ValueError('endpoint identity mismatch')
            result[model] = (url, key)
    return result


def open_ledger(output, identity):
    os.umask(0o077)
    private(output, directory=True)
    path = Path(output) / 'ledger.sqlite3'
    db = sqlite3.connect(path, timeout=30)
    private(path)
    db.execute('pragma synchronous=FULL')
    db.executescript('''
      create table if not exists metadata (key text primary key, value text);
      create table if not exists attempts (qid text, arm text, step integer, state text, info text,
          primary key(qid,arm,step));
      create table if not exists slots (qid text, arm text, info text, primary key(qid,arm));
      create table if not exists events (id integer primary key, qid text, arm text,
          step integer, state text, info text, recorded_at text default CURRENT_TIMESTAMP);
      create trigger if not exists immutable_events_update before update on events
          begin select raise(abort,'append only'); end;
      create trigger if not exists immutable_events_delete before delete on events
          begin select raise(abort,'append only'); end;
    ''')
    encoded = canonical(identity).decode()
    old = db.execute("select value from metadata where key='identity'").fetchone()
    if old and old[0] != encoded:
        db.close()
        raise ValueError('resume identity mismatch')
    db.execute("insert or ignore into metadata values ('identity',?)", (encoded,))
    db.commit()
    return db


def attempt(db, case, arm, step, model, manifest, providers, sender):
    qid = case['qid']
    existing = db.execute('select state,info from attempts where qid=? and arm=? and step=?',
                          (qid, arm, step)).fetchone()
    if existing:
        return dict(json.loads(existing[1]), state=existing[0], resumed=True)
    payload = request_payload(case, model)
    endpoint_hash = sha(providers[model][0].encode())
    if endpoint_hash != manifest['endpoint_sha256'][model]:
        raise ValueError('endpoint identity mismatch')
    old_unknowns = historical_unknowns(manifest)
    if any(historical_match(u, case, model, sha(canonical(payload)), endpoint_hash)
           for u in old_unknowns):
        return {'state': 'HISTORICAL_UNKNOWN_BLOCKED', 'model': model}
    info = dict(model=model, messages_sha256=case['messages_sha256'],
                comparison_input_hash=comparison_hash(case), request_hash=sha(canonical(payload)),
                endpoint_sha256=endpoint_hash, cost=None, cost_status='UNKNOWN',
                started_at=time.time(), request_payload=payload)
    # BEGIN IMMEDIATE makes check+reservation atomic across accidental processes.
    db.execute('begin immediate')
    try:
        if db.execute('select count(*) from attempts').fetchone()[0] >= 48:
            raise ValueError('48-call cap exhausted')
        db.execute('insert into attempts values (?,?,?,?,?)',
                   (qid, arm, step, 'UNKNOWN_PENDING', canonical(info).decode()))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (qid, arm, step, 'UNKNOWN_PENDING', canonical(info).decode()))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    start = time.monotonic()
    state = 'UNKNOWN'
    try:
        code, raw = sender(*providers[model], payload, 120 if model == CHEAP else 180)
        info.update(http_status=code, raw_response=raw.decode('utf-8', errors='replace'),
                    response_sha256=sha(raw))
        data = json.loads(raw)
        choice = (data.get('choices') or [{}])[0]
        text = (choice.get('message') or {}).get('content')
        text = text if isinstance(text, str) else ''
        finish = choice.get('finish_reason')
        info.update(text=text, finish_reason=finish, usage=data.get('usage'),
                    provider_request_id=data.get('id'), actual_model=data.get('model'),
                    answer_sha256=sha(text.encode()))
        info['model_identity_status'] = ('UNKNOWN' if not data.get('model') else
                                         'MATCH' if data['model'] == model else 'MISMATCH')
        state = ('PROVIDER_FAILURE' if code != 200 or data.get('error') else
                 'TRUNCATED' if finish == 'length' else 'UNKNOWN' if finish != 'stop' else
                 'COMPLETED' if text.strip() else 'EMPTY')
    except urllib.error.HTTPError as exc:
        state = 'PROVIDER_FAILURE'
        info.update(http_status=exc.code, error_type=type(exc).__name__)
    except Exception as exc:
        # Exception strings may include credentials or provider bodies.
        info['error_type'] = type(exc).__name__
    info.update(state=state, latency_ms=(time.monotonic() - start) * 1000,
                ended_at=time.time())
    with db:
        db.execute('update attempts set state=?,info=? where qid=? and arm=? and step=?',
                   (state, canonical(info).decode(), qid, arm, step))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (qid, arm, step, state, canonical(info).decode()))
    return info


def run(manifest, output, *, execute=False, db_path=None, sender=send, providers=None):
    if (manifest.get('previous_run') and Path(output).resolve()
            == Path(manifest['previous_run']['ledger_path']).resolve().parent):
        raise ValueError('continuation output must differ from previous run')
    preflight = validate(manifest)
    if not execute:
        return preflight
    if not manifest.get('authorization_id') or manifest.get('approved_max_calls') != 48:
        raise ValueError('owner call authorization missing')
    if manifest.get('code_sha256') != code_identity():
        raise ValueError('runtime code is not frozen')
    unknown_inventory = historical_unknowns(manifest)
    providers = providers if providers is not None else providers_from_db(db_path, manifest)
    identity = dict(manifest_sha256=preflight['manifest_sha256'], code_sha256=code_identity(),
                    history_inventory=dict(ledger_count=len(history_sources(manifest)),
                                           unknown_rows=len(unknown_inventory),
                                           audit=manifest['history_inventory_audit'],
                                           sources=history_sources(manifest)),
                    execution_arms=manifest.get('execution_arms', list(ARMS)),
                    previous_run=manifest.get('previous_run'))
    db = open_ledger(output, identity)
    try:
        import_previous(db, manifest, output)
        evaluation_identity = (previous_snapshot(manifest)['identity']['manifest_sha256']
                               if manifest.get('previous_run') else preflight['manifest_sha256'])
        for index, case in enumerate(manifest['cases']):
            # Rotate within each source group (explicit frozen order), serial only.
            rotation = case.get('arm_rotation', index % 3)
            if type(rotation) is not int or rotation not in (0, 1, 2):
                raise ValueError('invalid frozen arm rotation')
            for arm in ARMS[rotation:] + ARMS[:rotation]:
                if arm not in manifest.get('execution_arms', ARMS):
                    continue
                qid = case['qid']
                if db.execute('select 1 from slots where qid=? and arm=?', (qid, arm)).fetchone():
                    continue
                record = dict(qid=qid, arm=arm, evaluation_identity=evaluation_identity,
                              execution_manifest_sha256=preflight['manifest_sha256'],
                              comparison_input_hash=comparison_hash(case), core_correctness='UNKNOWN',
                              cost=None, generation_status='NOT_RUN', execution_status='NOT_RUN',
                              evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY',
                              rule_version=RULE_VERSION)
                citations = case.get('citations')
                usable = (isinstance(citations, list) and case.get('retrieval_status') == 'SUCCESS'
                          and any(isinstance(c, dict) and isinstance(c.get('content'), str)
                                  and c['content'].strip() for c in citations))
                if not usable:
                    record['blocked_reason'] = 'NO_RECOVERABLE_USABLE_DELIVERED_EVIDENCE'
                else:
                    cfg = config_for(manifest, arm)
                    route = decide_route(case['question'], citations, messages=case['messages'],
                                         retrieval_status=case['retrieval_status'], config=cfg,
                                         baseline_model=manifest['baseline_model'])
                    record['route'] = route.to_payload()
                    if route.selected_model not in (CHEAP, EXPENSIVE):
                        record['blocked_reason'] = 'NO_APPROVED_SELECTED_MODEL'
                    else:
                        first = attempt(db, case, arm, 0, route.selected_model, manifest, providers, sender)
                        result = first
                        quality = 'NOT_RUN'
                        if (arm == 'DYNAMIC' and route.selected_role == 'CHEAP'
                                and first['state'] == 'COMPLETED'
                                and first.get('model_identity_status') == 'MATCH'
                                and manifest.get('quality_check', 'NONE') == 'REFERENCE_RANGE'):
                            from app.services.grounded_generation import _reference_range_quality
                            quality = _reference_range_quality(first['text'], len(citations))
                        escalation = decide_escalation(route, completion_status=first['state'],
                                                       quality_status=quality, config=cfg)
                        record.update(quality_status=quality, escalation=escalation.to_payload())
                        if escalation.should_retry:
                            result = attempt(db, case, arm, 1, escalation.selected_model,
                                             manifest, providers, sender)
                        record.update(execution_status=result['state'], generation_status=result['state'],
                                      text=result.get('text'), finish_reason=result.get('finish_reason'),
                                      model_identity_status=result.get('model_identity_status'))
                        attempts = [(step, state, json.loads(info)) for step, state, info in db.execute(
                            'select step,state,info from attempts where qid=? and arm=? order by step',
                            (qid, arm))]
                        record['attempts'] = [dict(info, step=step, state=state)
                                              for step, state, info in attempts]
                        record['actual_calls'] = len(attempts)
                        # UNKNOWN endpoints never acquire a zero latency/token value.
                        record['latency_ms'] = (sum(info['latency_ms'] for _, _, info in attempts)
                            if attempts and all('ended_at' in info and 'latency_ms' in info
                                                for _, _, info in attempts) else None)
                        for metric in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                            values = [(info.get('usage') or {}).get(metric) for _, _, info in attempts]
                            record[metric] = (sum(values) if values and all(type(v) is int and v >= 0
                                                                         for v in values) else None)
                with db:
                    db.execute('insert into slots values (?,?,?)', (qid, arm, canonical(record).decode()))
                export(db, output)
        return export(db, output)
    finally:
        db.close()


def export(db, output):
    records = [json.loads(r[0]) for r in db.execute('select info from slots order by qid,arm')]
    atomic_json(Path(output) / 'records.json', records)
    path = Path(output) / 'records.jsonl'
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temp, 'w', encoding='utf-8') as stream:
        private(temp)
        for record in records:
            stream.write(canonical(record).decode() + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    private(path)
    counts = dict(db.execute('select state,count(*) from attempts group by state'))
    status = dict(status='RECORDED', planned_slots=36, recorded_slots=len(records),
                  attempts=sum(counts.values()), attempt_states=counts, cost_status='UNKNOWN',
                  historical_reused=0, evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY')
    identity = json.loads(db.execute("select value from metadata where key='identity'").fetchone()[0])
    status['history_inventory'] = identity['history_inventory']
    status['historical_unknown_blocked_slots'] = sum(
        record['execution_status'] == 'HISTORICAL_UNKNOWN_BLOCKED' for record in records)
    atomic_json(Path(output) / 'status.json', status)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'execute'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--db', default='/opt/amrag/backend/local.db')
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
        result = run(manifest, args.output, execute=args.mode == 'execute', db_path=args.db)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
