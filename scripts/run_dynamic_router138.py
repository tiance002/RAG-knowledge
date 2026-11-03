"""166 new DYNAMIC slots, serial frozen-input replay; never changes production.

Each send is durably reserved as UNKNOWN_PENDING. No retry or escalation is
possible, including on resume. Gold is not sent or used for routing.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

import run_dynamic_router_v1_experiment as base
from app.services.model_router import RULE_VERSION, decide_route, decide_escalation

CHEAP, EXPENSIVE = base.CHEAP, base.EXPENSIVE
MAX_CALLS = 166
canonical, sha = base.canonical, base.sha


def code_identity():
    result = base.code_identity()
    result['previous_runner'] = result.pop('runner')
    result['runner'] = sha(Path(__file__).read_bytes())
    freeze = Path(__file__).with_name('freeze_dynamic138.py')
    if freeze.exists():
        result['freeze_loader'] = sha(freeze.read_bytes())
        result['freeze_helpers'] = sha(freeze.with_name('dynamic_router_freeze12.py').read_bytes())
    return result


def load_cases(manifest):
    if manifest.get('case_loader') == 'freeze_dynamic138':
        from freeze_dynamic138 import load_frozen_cases
        return load_frozen_cases(manifest)
    return manifest['cases']


def not_run_reason(case):
    if case.get('not_run_reason'):
        return case['not_run_reason']
    for key in ('source_status', 'source_state'):
        if case.get(key) in ('MISSING_INPUT', 'NOT_RUN'):
            return case[key]
    return None


def validate(manifest, cases=None):
    if manifest.get('execution_arms') != ['DYNAMIC']:
        raise ValueError('DYNAMIC only')
    if manifest.get('quality_check') != 'NONE' or manifest.get('baseline_model') != CHEAP:
        raise ValueError('fixed Qwen baseline and quality NONE required')
    if manifest.get('approved_max_calls') != MAX_CALLS:
        raise ValueError('166 maximum calls required')
    base.config_for(manifest, 'DYNAMIC')
    cases = load_cases(manifest) if cases is None else cases
    if len(cases) != MAX_CALLS or len({c['qid'] for c in cases}) != MAX_CALLS:
        raise ValueError('166 unique frozen slots required')
    for case in cases:
        if not isinstance(case['qid'], str) or not case['qid'].strip():
            raise ValueError('invalid qid')
        if not_run_reason(case):
            continue
        messages = case.get('messages')
        if not isinstance(messages, list) or not messages:
            raise ValueError('missing frozen messages')
        if any(not isinstance(m, dict) or set(m) != {'role', 'content'}
               or m['role'] not in ('system', 'user', 'assistant')
               or not isinstance(m['content'], str) for m in messages):
            raise ValueError('invalid frozen message')
        if sha(canonical(messages)) != case.get('messages_sha256'):
            raise ValueError('frozen messages hash mismatch')
        if not isinstance(case.get('question'), str) or not case['question'].strip():
            raise ValueError('missing frozen question')
        if len(canonical(messages)) + 4096 + 512 >= 256000:
            raise ValueError('context bound exceeded')
        if case.get('comparison_input_hash') != base.comparison_hash(case):
            raise ValueError('comparison hash mismatch')
        if not isinstance(case.get('citations', []), list) or any(
                not isinstance(c, dict) for c in case.get('citations', [])):
            raise ValueError('invalid frozen evidence')
    # Full audited inventory is validated even for offline preflight.
    unknowns = base.historical_unknowns(manifest)
    routes, blocked = {}, 0
    cfg = base.config_for(manifest, 'DYNAMIC')
    for case in cases:
        citations = case.get('citations', [])
        if (not_run_reason(case) or case.get('retrieval_status') != 'SUCCESS'
                or not any(isinstance(c.get('content'), str) and c['content'].strip() for c in citations)):
            blocked += 1
            continue
        route = decide_route(case['question'], citations, messages=case['messages'],
                             retrieval_status=case['retrieval_status'], config=cfg, baseline_model=CHEAP)
        routes[route.selected_role] = routes.get(route.selected_role, 0) + 1
    return dict(status='PASS', cases=len(cases), planned_arm_slots=len(cases),
                maximum_calls=MAX_CALLS, model_calls=0,
                planned_roles=routes, not_run_slots=blocked,
                historical_unknown_rows=len(unknowns), rule_version=RULE_VERSION,
                manifest_sha256=sha(canonical(manifest)), code_sha256=code_identity(),
                evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY')


def matches(unknown, case, model, request_hash, endpoint_hash):
    # Match payload independent of qid or prior experimental arm. A hash of
    # messages + model also blocks across endpoints, conservatively.
    payload = unknown.get('request_payload') or unknown.get('payload')
    if isinstance(payload, dict) and sha(canonical(payload)) == request_hash:
        return True
    if unknown.get('request_hash') == request_hash:
        return True
    if unknown.get('model') == model and unknown.get('messages_sha256') == case['messages_sha256']:
        return True
    return base.historical_match(unknown, case, model, request_hash, endpoint_hash)


def attempt(db, case, model, manifest, providers, sender):
    payload = base.request_payload(case, model)
    request_hash = sha(canonical(payload))
    endpoint_hash = sha(providers[model][0].encode())
    if endpoint_hash != manifest['endpoint_sha256'][model]:
        raise ValueError('endpoint identity mismatch')
    # Inventory is rehashed each time; a concurrent historical mutation blocks
    # additional sends instead of silently invalidating UNKNOWN protection.
    unknowns = base.historical_unknowns(manifest)
    db.execute('begin immediate')
    try:
        row = db.execute('select state,info from attempts where qid=? and arm=? and step=0',
                         (case['qid'], 'DYNAMIC')).fetchone()
        if row:
            db.commit()
            return dict(json.loads(row[1]), state=row[0], resumed=True)
        for state, encoded in db.execute("select state,info from attempts where upper(state) like '%UNKNOWN%'"):
            unknowns.append(dict(json.loads(encoded), state=state))
        if any(matches(u, case, model, request_hash, endpoint_hash) for u in unknowns):
            db.commit()
            return dict(state='HISTORICAL_UNKNOWN_BLOCKED', model=model)
        if db.execute('select count(*) from attempts').fetchone()[0] >= MAX_CALLS:
            db.commit()
            return dict(state='CALL_CAP_BLOCKED', model=model)
        info = dict(qid=case['qid'], model=model, messages_sha256=case['messages_sha256'],
                    comparison_input_hash=base.comparison_hash(case), request_hash=request_hash,
                    endpoint_sha256=endpoint_hash, started_at=time.time(),
                    request_settings={k: v for k, v in payload.items() if k != 'messages'},
                    cost=None, cost_status='UNKNOWN')
        encoded = canonical(info).decode()
        db.execute('insert into attempts values (?,?,?,?,?)',
                   (case['qid'], 'DYNAMIC', 0, 'UNKNOWN_PENDING', encoded))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (case['qid'], 'DYNAMIC', 0, 'UNKNOWN_PENDING', encoded))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    start, state = time.monotonic(), 'UNKNOWN'
    try:
        code, raw = sender(*providers[model], payload, 120 if model == CHEAP else 180)
        info.update(http_status=code, raw_response=raw.decode('utf-8', errors='replace'),
                    response_sha256=sha(raw))
        # Provider rate limits are a separate state even for a non-JSON body.
        if code == 429:
            state = 'PROVIDER_RATE_LIMITED'
        elif code != 200:
            state = 'PROVIDER_FAILURE'
        else:
            data = json.loads(raw)
            choice = (data.get('choices') or [{}])[0]
            text = (choice.get('message') or {}).get('content')
            text = text if isinstance(text, str) else ''
            finish = choice.get('finish_reason')
            actual = data.get('model')
            info.update(text=text, finish_reason=finish, usage=data.get('usage'),
                        actual_model=actual, provider_request_id=data.get('id'),
                        answer_sha256=sha(text.encode()), model_identity_status=(
                            'UNKNOWN' if not actual else 'MATCH' if actual == model else 'MISMATCH'))
            state = ('PROVIDER_FAILURE' if data.get('error') else 'TRUNCATED' if finish == 'length'
                     else 'UNKNOWN' if finish != 'stop' else 'COMPLETED' if text.strip() else 'EMPTY')
    except urllib.error.HTTPError as exc:
        state = 'PROVIDER_RATE_LIMITED' if exc.code == 429 else 'PROVIDER_FAILURE'
        info.update(http_status=exc.code, error_type=type(exc).__name__)
    except Exception as exc:
        # No credential-bearing exception text. Unknown is permanent, no retry.
        info['error_type'] = type(exc).__name__
    info.update(state=state, ended_at=time.time(), latency_ms=(time.monotonic() - start) * 1000)
    with db:
        db.execute('update attempts set state=?,info=? where qid=? and arm=? and step=0',
                   (state, canonical(info).decode(), case['qid'], 'DYNAMIC'))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (case['qid'], 'DYNAMIC', 0, state, canonical(info).decode()))
    return info


def export(db, output):
    records = [json.loads(r[0]) for r in db.execute('select info from slots order by qid')]
    base.atomic_json(Path(output) / 'records.json', records)
    counts = dict(db.execute('select state,count(*) from attempts group by state'))
    status = dict(status='RECORDED', planned_slots=MAX_CALLS, recorded_slots=len(records),
                  attempts=sum(counts.values()), attempt_states=counts, maximum_calls=MAX_CALLS,
                  cost_status='UNKNOWN', evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY',
                  slot_states={s: sum(r['execution_status'] == s for r in records)
                               for s in sorted({r['execution_status'] for r in records})})
    base.atomic_json(Path(output) / 'status.json', status)
    return status


def run(manifest, output, *, execute=False, db_path=None, sender=base.send, providers=None):
    cases = load_cases(manifest)
    preflight = validate(manifest, cases)
    if not execute:
        return preflight
    if not manifest.get('authorization_id') or manifest.get('code_sha256') != code_identity():
        raise ValueError('owner authorization or frozen runtime identity missing')
    if any(Path(output).resolve() == Path(s['path']).resolve().parent for s in base.history_sources(manifest)):
        raise ValueError('output must differ from historical ledger directories')
    providers = providers if providers is not None else base.providers_from_db(db_path, manifest)
    db = base.open_ledger(output, dict(manifest_sha256=preflight['manifest_sha256'],
                                     code_sha256=code_identity(), maximum_calls=MAX_CALLS))
    cfg = base.config_for(manifest, 'DYNAMIC')
    try:
        for case in cases:
            qid = case['qid']
            if db.execute('select 1 from slots where qid=? and arm=?', (qid, 'DYNAMIC')).fetchone():
                continue
            record = dict(qid=qid, original_qid=case.get('original_qid'), arm='DYNAMIC',
                          stratum=case.get('stratum'), messages_sha256=case.get('messages_sha256'),
                          comparison_input_hash=case.get('comparison_input_hash'),
                          execution_manifest_sha256=preflight['manifest_sha256'],
                          core_correctness='UNKNOWN', cost=None, cost_status='UNKNOWN',
                          actual_calls=0, execution_status='NOT_RUN', generation_status='NOT_RUN',
                          evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY', rule_version=RULE_VERSION)
            try:
                citations = case.get('citations', [])
                reason = not_run_reason(case)
                usable = (case.get('retrieval_status') == 'SUCCESS' and
                          any(isinstance(c.get('content'), str) and c['content'].strip() for c in citations))
                if reason or not usable:
                    record['blocked_reason'] = reason or 'NO_RECOVERABLE_USABLE_DELIVERED_EVIDENCE'
                else:
                    route = decide_route(case['question'], citations, messages=case['messages'],
                                         retrieval_status=case['retrieval_status'], config=cfg,
                                         baseline_model=CHEAP)
                    record['route'] = route.to_payload()
                    if route.selected_model not in (CHEAP, EXPENSIVE):
                        record['blocked_reason'] = 'NO_APPROVED_SELECTED_MODEL'
                    else:
                        outcome = attempt(db, case, route.selected_model, manifest, providers, sender)
                        escalation = decide_escalation(route, completion_status=outcome['state'],
                                                       quality_status='NOT_RUN', config=cfg)
                        if escalation.should_retry:
                            raise ValueError('unapproved escalation')
                        record.update(execution_status=outcome['state'], generation_status=outcome['state'],
                                      escalation=escalation.to_payload(), quality_status='NOT_RUN',
                                      text=outcome.get('text'), finish_reason=outcome.get('finish_reason'),
                                      model_identity_status=outcome.get('model_identity_status'))
            except Exception as exc:
                record.update(execution_status='ERROR', generation_status='NOT_RUN',
                              error_type=type(exc).__name__)
            rows = db.execute('select state,info from attempts where qid=? and arm=?', (qid, 'DYNAMIC')).fetchall()
            record['attempts'] = [dict(json.loads(info), state=state) for state, info in rows]
            record['actual_calls'] = len(rows)
            for metric in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                values = [(a.get('usage') or {}).get(metric) for a in record['attempts']]
                record[metric] = sum(values) if values and all(type(v) is int and v >= 0 for v in values) else None
            values = [a.get('latency_ms') for a in record['attempts']]
            record['latency_ms'] = sum(values) if values and all(v is not None for v in values) else None
            with db:
                db.execute('insert or ignore into slots values (?,?,?)', (qid, 'DYNAMIC', canonical(record).decode()))
            export(db, output)
        return export(db, output)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'execute'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--db', default='/opt/amrag/backend/local.db')
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
        print(json.dumps(run(manifest, args.output, execute=args.mode == 'execute', db_path=args.db)))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='FAIL', error_type=type(exc).__name__)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
