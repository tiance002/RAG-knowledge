"""Isolated serial paired replay of frozen inputs; no live retrieval or gold scoring."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

import run_dynamic_router_v1_experiment as base
import run_dynamic_router138 as frozen
from app.services.model_router import AFFINITY_VERSION, decide_route

canonical, sha = base.canonical, base.sha
CHEAP, EXPENSIVE = base.CHEAP, base.EXPENSIVE
ARMS = ('AFFINITY_V2', 'GLM_ONLY')


def code_identity():
    return dict(base=base.code_identity(), frozen=frozen.code_identity(), runner=sha(Path(__file__).read_bytes()))


def load_cases(manifest):
    cases = frozen.load_cases(manifest['frozen166'])
    entry = manifest.get('prior12')
    if entry:
        path = Path(entry['path'])
        raw = path.read_bytes()
        if sha(raw) != entry['sha256']:
            raise ValueError('prior freeze hash mismatch')
        prior = json.loads(raw.decode('utf-8-sig'))['cases']
        if len(prior) != 12:
            raise ValueError('expected 12 prior cases')
        cases += prior
    return cases


def historical_unknowns(manifest):
    for source in base.history_sources(manifest):
        wal = Path(str(Path(source['path']).resolve()) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('historical ledger has active WAL')
    return base.historical_unknowns(manifest)


def validate(manifest, cases):
    if manifest.get('execution_arms') != list(ARMS) or manifest.get('quality_check') != 'NONE':
        raise ValueError('paired arms and quality NONE required')
    if manifest.get('router_config', {}).get('selection_policy') != 'AFFINITY_V2':
        raise ValueError('AFFINITY_V2 policy required')
    cfg = base.config_for(manifest, 'DYNAMIC')
    if cfg.escalate_on_quality_fail:
        raise ValueError('escalation must be disabled')
    if type(manifest.get('approved_max_calls')) is not int or not 0 <= manifest['approved_max_calls'] <= len(cases) * 2:
        raise ValueError('invalid call cap')
    if len(cases) not in (166, 178) or len({c['qid'] for c in cases}) != len(cases):
        raise ValueError('expected 166 or 178 distinct cases')
    roles, blocked = {}, 0
    for c in cases:
        if frozen.not_run_reason(c):
            blocked += 1
            continue
        messages = c.get('messages')
        if (not isinstance(c.get('question'), str) or not c['question'].strip()
                or not isinstance(messages, list) or not messages
                or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                       or m['role'] not in ('system', 'user', 'assistant')
                       or not isinstance(m['content'], str) for m in messages)
                or sha(canonical(messages)) != c.get('messages_sha256')
                or len(canonical(messages)) + 4608 >= 256000):
            raise ValueError('invalid frozen input')
        if c.get('comparison_input_hash') and c['comparison_input_hash'] != base.comparison_hash(c):
            raise ValueError('comparison input mismatch')
        citations = c.get('citations', [])
        if not isinstance(citations, list) or any(not isinstance(x, dict) for x in citations):
            raise ValueError('invalid citations')
        if not usable(c):
            blocked += 1
            continue
        decision = decide_route(c['question'], citations, messages=messages,
                                retrieval_status=c['retrieval_status'], config=cfg, baseline_model=CHEAP)
        roles[decision.selected_role] = roles.get(decision.selected_role, 0) + 1
    unknowns = historical_unknowns(manifest)
    return dict(status='PASS', cases=len(cases), planned_arm_slots=2 * len(cases),
                maximum_calls=manifest['approved_max_calls'], model_calls=0,
                blocked_cases=blocked, planned_roles=roles, historical_unknown_rows=len(unknowns),
                rule_version=AFFINITY_VERSION, manifest_sha256=sha(canonical(manifest)),
                code_sha256=code_identity(), evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY')


def usable(c):
    return (not frozen.not_run_reason(c) and c.get('retrieval_status') == 'SUCCESS'
            and any(isinstance(x.get('content'), str) and x['content'].strip()
                    for x in c.get('citations', [])))


def historical_completed(manifest, case, model, payload, endpoint):
    request_hash = sha(canonical(payload))
    for source in base.history_sources(manifest):
        path = Path(source['path']).resolve()
        if sha(path.read_bytes()) != source['sha256']:
            raise ValueError('historical ledger changed')
        wal = Path(str(path) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('historical ledger has active WAL')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            for (encoded,) in db.execute("select info from attempts where state='COMPLETED'"):
                info = json.loads(encoded)
                if (info.get('model') == model and info.get('actual_model') == model
                        and info.get('messages_sha256') == case['messages_sha256']
                        and info.get('request_hash') == request_hash
                        and info.get('endpoint_sha256') == endpoint
                        and info.get('request_payload') == payload
                        and info.get('finish_reason') == 'stop'
                        and isinstance(info.get('text'), str) and info['text'].strip()):
                    return dict(info, state='COMPLETED', reuse='HISTORICAL_EXACT', source_ledger_sha256=source['sha256'])
    return None


def attempt(db, case, arm, model, manifest, providers, sender):
    payload = base.request_payload(case, model)
    request_hash = sha(canonical(payload))
    endpoint = sha(providers[model][0].encode())
    if endpoint != manifest['endpoint_sha256'][model]:
        raise ValueError('endpoint mismatch')
    unknowns = historical_unknowns(manifest)
    # UNKNOWN takes priority over all reuse, including another completed response.
    if any(frozen.matches(u, case, model, request_hash, endpoint) for u in unknowns):
        return dict(state='HISTORICAL_UNKNOWN_BLOCKED', model=model)
    db.execute('begin immediate')
    try:
        rows = db.execute('select state,info from attempts').fetchall()
        for state, encoded in rows:
            info = json.loads(encoded)
            if frozen.matches(dict(info, state=state), case, model, request_hash, endpoint):
                db.commit()
                return dict(info, state=state, reuse='CURRENT_LEDGER', resumed=True)
        old = historical_completed(manifest, case, model, payload, endpoint)
        if old:
            db.commit()
            return old
        if len(rows) >= manifest['approved_max_calls']:
            db.commit()
            return dict(state='CALL_CAP_BLOCKED', model=model)
        info = dict(qid=case['qid'], model=model, messages_sha256=case['messages_sha256'],
                    request_hash=request_hash, request_payload=payload, endpoint_sha256=endpoint,
                    started_at=time.time(), cost=None, cost_status='UNKNOWN')
        encoded = canonical(info).decode()
        db.execute('insert into attempts values (?,?,?,?,?)', (case['qid'], arm, 0, 'UNKNOWN_PENDING', encoded))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (case['qid'], arm, 0, 'UNKNOWN_PENDING', encoded))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    start, state = time.monotonic(), 'UNKNOWN'
    try:
        code, raw = sender(*providers[model], payload, 120 if model == CHEAP else 180)
        info.update(http_status=code, raw_response=raw.decode('utf-8', errors='replace'), response_sha256=sha(raw))
        if code == 429:
            state = 'PROVIDER_RATE_LIMITED'
        elif code != 200:
            state = 'PROVIDER_FAILURE'
        else:
            data = json.loads(raw)
            choice = (data.get('choices') or [{}])[0]
            text = (choice.get('message') or {}).get('content')
            text = text if isinstance(text, str) else ''
            finish, actual = choice.get('finish_reason'), data.get('model')
            info.update(text=text, finish_reason=finish, usage=data.get('usage'), actual_model=actual,
                        provider_request_id=data.get('id'), answer_sha256=sha(text.encode()),
                        model_identity_status='UNKNOWN' if not actual else 'MATCH' if actual == model else 'MISMATCH')
            state = ('PROVIDER_FAILURE' if data.get('error') else 'TRUNCATED' if finish == 'length'
                     else 'UNKNOWN' if finish != 'stop' else 'COMPLETED' if text.strip() else 'EMPTY')
    except urllib.error.HTTPError as exc:
        state = 'PROVIDER_RATE_LIMITED' if exc.code == 429 else 'PROVIDER_FAILURE'
        info.update(http_status=exc.code, error_type=type(exc).__name__)
    except Exception as exc:
        info['error_type'] = type(exc).__name__
    info.update(state=state, ended_at=time.time(), latency_ms=(time.monotonic() - start) * 1000)
    with db:
        db.execute('update attempts set state=?,info=? where qid=? and arm=? and step=0',
                   (state, canonical(info).decode(), case['qid'], arm))
        db.execute('insert into events(qid,arm,step,state,info) values (?,?,?,?,?)',
                   (case['qid'], arm, 0, state, canonical(info).decode()))
    return info


def export(db, output, count, cap):
    records = [json.loads(row[0]) for row in db.execute('select info from slots order by qid,arm')]
    base.atomic_json(Path(output) / 'records.json', records)
    states = dict(db.execute('select state,count(*) from attempts group by state'))
    status = dict(status='RECORDED', planned_slots=count * 2, recorded_slots=len(records),
                  attempts=sum(states.values()), attempt_states=states, maximum_calls=cap,
                  cost_status='UNKNOWN', evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY',
                  slot_states={s: sum(r['execution_status'] == s for r in records)
                               for s in sorted({r['execution_status'] for r in records})})
    base.atomic_json(Path(output) / 'status.json', status)
    return status


def run(manifest, output, *, execute=False, db_path=None, providers=None, sender=base.send):
    cases = load_cases(manifest)
    preflight = validate(manifest, cases)
    if not execute:
        return preflight
    if not manifest.get('authorization_id') or manifest.get('code_sha256') != code_identity():
        raise ValueError('authorization and exact runtime identity required')
    if any(Path(output).resolve() == Path(s['path']).resolve().parent for s in base.history_sources(manifest)):
        raise ValueError('output overlaps historical ledger')
    providers = providers if providers is not None else base.providers_from_db(db_path, manifest)
    db = base.open_ledger(output, dict(manifest_sha256=preflight['manifest_sha256'], code_sha256=code_identity()))
    lock = sqlite3.connect(str(Path(output) / 'execution-lock.sqlite3'), timeout=0)
    try:
        lock.execute('begin exclusive')
        cfg = base.config_for(manifest, 'DYNAMIC')
        for case in cases:
            route = (decide_route(case['question'], case['citations'], messages=case['messages'],
                                  retrieval_status=case['retrieval_status'], config=cfg, baseline_model=CHEAP)
                     if usable(case) else None)
            for arm in ARMS:
                if db.execute('select 1 from slots where qid=? and arm=?', (case['qid'], arm)).fetchone():
                    continue
                record = dict(qid=case['qid'], arm=arm, messages_sha256=case.get('messages_sha256'),
                              comparison_input_hash=case.get('comparison_input_hash'),
                              execution_status='NOT_RUN', core_correctness='UNKNOWN', cost=None,
                              cost_status='UNKNOWN', evaluation_kind='FROZEN_PROVIDER_INPUT_REPLAY')
                if route:
                    model = route.selected_model if arm == 'AFFINITY_V2' else EXPENSIVE
                    record['route'] = route.to_payload() if arm == 'AFFINITY_V2' else None
                    if model not in (CHEAP, EXPENSIVE):
                        raise ValueError('unapproved selected model')
                    outcome = attempt(db, case, arm, model, manifest, providers, sender)
                    record.update(execution_status=outcome['state'], outcome=outcome)
                else:
                    record['blocked_reason'] = frozen.not_run_reason(case) or 'NO_RECOVERABLE_USABLE_DELIVERED_EVIDENCE'
                with db:
                    db.execute('insert into slots values (?,?,?)', (case['qid'], arm, canonical(record).decode()))
                export(db, output, len(cases), manifest['approved_max_calls'])
        return export(db, output, len(cases), manifest['approved_max_calls'])
    finally:
        lock.close()
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('preflight', 'execute'))
    p.add_argument('--manifest', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--db', default='/opt/amrag/backend/local.db')
    a = p.parse_args()
    try:
        manifest = json.loads(Path(a.manifest).read_text(encoding='utf-8-sig'))
        print(json.dumps(run(manifest, a.output, execute=a.mode == 'execute', db_path=a.db), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='FAIL', error_type=type(exc).__name__)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
