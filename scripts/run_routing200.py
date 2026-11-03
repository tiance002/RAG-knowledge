"""Serial frozen-message routing pilot. Preflight is offline; no automatic retries.

A durable UNKNOWN_PENDING row is committed before every provider send. All visited
(qid, role) pairs, including UNKNOWN, are permanently excluded from replay.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = dict(max_cheap_calls=150, max_expensive_calls=150,
                cheap_model='XingChenAGI/Xing4.0-29B',
                expensive_model='zai-org/GLM-5.3-Flash',
                max_tokens=4096, temperature=0.7, context_limit=256000)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def validate_manifest(manifest):
    cfg = dict(DEFAULTS, **manifest.get('config', {}))
    for key in ('max_cheap_calls', 'max_expensive_calls'):
        if type(cfg[key]) is not int or not 0 <= cfg[key] <= 150:
            raise ValueError('invalid call cap: ' + key)
    for key in ('cheap_model', 'expensive_model', 'max_tokens', 'temperature', 'context_limit'):
        if cfg[key] != DEFAULTS[key]:
            raise ValueError('unapproved model/settings: ' + key)
    cases = manifest.get('cases')
    if not isinstance(cases, list) or not cases or len(cases) > cfg['max_cheap_calls']:
        raise ValueError('invalid case count')
    seen = set()
    for c in cases:
        qid = c.get('qid')
        if not isinstance(qid, str) or not qid or qid in seen:
            raise ValueError('invalid or duplicate qid')
        seen.add(qid)
        if not isinstance(c.get('question'), str) or not c['question'].strip():
            raise ValueError('missing question: ' + qid)
        messages = c.get('messages')
        if not isinstance(messages, list) or not messages:
            raise ValueError('missing messages: ' + qid)
        for m in messages:
            if not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ('system', 'user', 'assistant') or not isinstance(m['content'], str):
                raise ValueError('invalid saved message: ' + qid)
        if sha(canonical(messages)) != c.get('messages_sha256'):
            raise ValueError('messages hash mismatch: ' + qid)
        # Conservative UTF-8 byte bound, including output and framing allowance.
        if len(canonical(messages)) + cfg['max_tokens'] + 512 >= cfg['context_limit']:
            raise ValueError('context capacity exceeded: ' + qid)
        if not any(k in c for k in ('gold', 'gold_refs')):
            raise ValueError('missing gold reference: ' + qid)
        if not any(k in c for k in ('context_sha256', 'source_refs', 'context_refs')):
            raise ValueError('missing source reference: ' + qid)
    return cfg


def private(path, directory=False):
    path = Path(path)
    if directory:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700 if directory else 0o600)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with open(temp, 'x', encoding='utf-8') as f:
        private(temp)
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    private(path)


def open_ledger(output, manifest):
    os.umask(0o077)
    private(output, directory=True)
    path = Path(output) / 'ledger.sqlite3'
    db = sqlite3.connect(path)
    private(path)
    db.execute('pragma synchronous=FULL')
    db.executescript('''
    create table if not exists metadata (key text primary key, value text not null);
    create table if not exists attempts (
      qid text, role text, state text not null, info text not null,
      primary key(qid,role));
    create table if not exists events (
      id integer primary key, qid text, role text, state text, info text,
      recorded_at text default CURRENT_TIMESTAMP);
    create trigger if not exists events_no_update before update on events
      begin select raise(abort, 'append-only events'); end;
    create trigger if not exists events_no_delete before delete on events
      begin select raise(abort, 'append-only events'); end;
    ''')
    fingerprint = sha(canonical(manifest))
    old = db.execute("select value from metadata where key='manifest_sha256'").fetchone()
    if old and old[0] != fingerprint:
        db.close()
        raise ValueError('ledger belongs to a different frozen manifest')
    db.execute("insert or ignore into metadata values ('manifest_sha256', ?)", (fingerprint,))
    db.commit()
    return db


def record(db, qid, role, state, info, initial=False):
    encoded = canonical(info).decode('utf-8')
    with db:
        if initial:
            db.execute('insert into attempts values (?,?,?,?)', (qid, role, state, encoded))
        else:
            db.execute('update attempts set state=?,info=? where qid=? and role=?', (state, encoded, qid, role))
        db.execute('insert into events(qid,role,state,info) values (?,?,?,?)', (qid, role, state, encoded))


def credentials(path, cfg):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute('select model_name,base_url,api_key from custom_models where model_name in (?,?)',
                          (cfg['cheap_model'], cfg['expensive_model'])).fetchall()
    result = {}
    for model, url, key in rows:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'api.siliconflow.cn' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443) or not key:
            raise ValueError('unapproved endpoint or missing credential')
        if model in result:
            raise ValueError('duplicate provider model credential')
        result[model] = (url.rstrip('/'), key)
    if set(result) != {cfg['cheap_model'], cfg['expensive_model']}:
        raise ValueError('missing configured model credentials')
    return result


def load_reviews(path):
    result = {}
    if path:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if line.strip():
                r = json.loads(line)
                if r['qid'] in result:
                    raise ValueError('duplicate reviewed qid')
                result[r['qid']] = r
    return result


def reviewed_failure(case, cheap, review, manifest_hash):
    if not review or cheap[0] != 'COMPLETED':
        return False
    info = json.loads(cheap[1])
    return (bool(info.get('text', '').strip()) and info.get('finish_reason') == 'stop'
            and case.get('expensive_eligible', True) is True
            and review.get('qid') == case['qid']
            and review.get('answer_sha256') == info.get('answer_sha256')
            and review.get('manifest_sha256') == manifest_hash
            and review.get('core_correctness') == 'FAIL'
            and review.get('failure_kind') == 'GENERATION_QUALITY'
            and (review.get('evidence_status') == 'PRESENT'
                 or (review.get('evidence_status') == 'PARTIAL' and review.get('evidence_reliable') is True))
            and review.get('answerability') is True
            and isinstance(review.get('reviewer'), str) and bool(review['reviewer'].strip())
            and isinstance(review.get('reason'), str) and bool(review['reason'].strip())
            and isinstance(review.get('evidence_refs'), list) and bool(review['evidence_refs'])
            and not case.get('excluded_from_core_accuracy', False))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(url, key, payload, timeout):
    request = urllib.request.Request(url + '/chat/completions', data=canonical(payload),
          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    # Explicit opener prevents authorization-bearing redirects; urllib has no retries.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        return response.status, response.read()


def trace_context(case, role, model, metadata):
    try:
        from routing_trace_bridge import trace_call
        return trace_call(case, role, model, metadata)
    except Exception:
        return nullcontext(None)


def run(manifest, output, mode='preflight', db_path='/opt/amrag/backend/local.db',
        reviews_path=None, continue_unvisited=False):
    cfg = validate_manifest(manifest)
    if mode not in ('preflight', 'cheap', 'expensive'):
        raise ValueError('invalid mode')
    if mode == 'preflight':
        return {'status': 'PASS', 'cases': len(manifest['cases']), 'model_calls': 0,
                'manifest_sha256': sha(canonical(manifest)), 'config': cfg}
    if not manifest.get('authorization_id'):
        raise ValueError('missing owner authorization reference')
    reviews = load_reviews(reviews_path)
    if mode == 'expensive' and not reviews:
        raise ValueError('expensive lane requires reviewed failures')
    providers = credentials(db_path, cfg)
    db = open_ledger(output, manifest)
    try:
        fingerprint = sha(canonical(manifest))
        def status():
            counts = {r + ':' + s: n for r, s, n in db.execute('select role,state,count(*) from attempts group by role,state')}
            value = dict(status='RECORDED', manifest_sha256=fingerprint, counts=counts,
                         telemetry='optional-existing-bridge', cost_status='UNKNOWN')
            atomic_json(Path(output) / 'status.json', value)
            return value
        status()
        if not continue_unvisited and db.execute("select 1 from attempts where state != 'COMPLETED' limit 1").fetchone():
            return dict(status(), stopped='visited failure or UNKNOWN; explicit continuation required')
        for case in manifest['cases']:
            qid = case['qid']
            if db.execute('select 1 from attempts where qid=? and role=?', (qid, mode)).fetchone():
                continue
            if mode == 'expensive':
                cheap = db.execute("select state,info from attempts where qid=? and role='cheap'", (qid,)).fetchone()
                if qid not in reviews:
                    continue
                if not cheap or not reviewed_failure(case, cheap, reviews[qid], fingerprint):
                    raise ValueError('invalid expensive review: ' + qid)
            used = db.execute('select count(*) from attempts where role=?', (mode,)).fetchone()[0]
            if used >= cfg['max_' + mode + '_calls']:
                break
            model = cfg[mode + '_model']
            payload = dict(model=model, messages=case['messages'], temperature=cfg['temperature'],
                           max_tokens=cfg['max_tokens'], stream=False)
            meta = dict(manifest_sha256=fingerprint, messages_sha256=case['messages_sha256'],
                        request_hash=sha(canonical(payload)), model=model, cost_status='UNKNOWN',
                        endpoint_sha256=sha(providers[model][0].encode('utf-8')),
                        attempt_id=fingerprint + ':' + qid + ':' + mode,
                        router_mode=mode.upper() + '_ONLY')
            record(db, qid, mode, 'UNKNOWN_PENDING', meta, initial=True)
            status()
            start = time.monotonic()
            state = 'UNKNOWN'
            info = dict(meta)
            trace_meta = dict(meta, request_payload=payload, run_id=fingerprint,
                              dataset_version=manifest.get('dataset_version'))
            with trace_context(case, mode, model, trace_meta) as trace:
                try:
                    code, raw = send(*providers[model], payload, 120 if mode == 'cheap' else 180)
                    info.update(http_status=code, raw_response=raw.decode('utf-8', errors='replace'),
                                response_sha256=sha(raw))
                    data = json.loads(raw)
                    choices = data.get('choices')
                    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                    text = (choice.get('message') or {}).get('content')
                    info.update(response_payload=data, usage=data.get('usage'),
                                provider_request_id=data.get('id'), finish_reason=choice.get('finish_reason'),
                                text=text if isinstance(text, str) else '', actual_model=data.get('model'))
                    info['answer_sha256'] = sha(info['text'].encode('utf-8'))
                    finish = choice.get('finish_reason')
                    if data.get('error') or code != 200:
                        state = 'PROVIDER_FAILURE'
                    elif finish == 'length':
                        state = 'TRUNCATED'
                    elif finish == 'stop':
                        state = 'COMPLETED' if info['text'].strip() else 'EMPTY'
                    else:
                        state = 'UNKNOWN'
                except urllib.error.HTTPError as exc:
                    # HTTP responses are known failures; no retry, even for 429.
                    state = 'HTTP_FAILURE'
                    info.update(http_status=exc.code, error_type=type(exc).__name__)
                    try:
                        raw = exc.read()
                        info.update(raw_response=raw.decode('utf-8', errors='replace'), response_sha256=sha(raw))
                    except Exception:
                        pass
                except Exception as exc:
                    info['error_type'] = type(exc).__name__
                info.update(state=state, latency_ms=round((time.monotonic() - start) * 1000, 3))
                if trace is not None:
                    try:
                        trace.finish(dict(info, model=info.get('actual_model')))
                        info['trace_id'] = trace.trace_id
                    except Exception:
                        pass
                record(db, qid, mode, state, info)
            status()
            if state != 'COMPLETED' and not continue_unvisited:
                break
        return status()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'cheap', 'expensive'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--db', default='/opt/amrag/backend/local.db')
    parser.add_argument('--reviews')
    parser.add_argument('--continue-unvisited', action='store_true')
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
        result = run(manifest, args.output, args.mode, args.db, args.reviews, args.continue_unvisited)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('status') == 'PASS' or not any(not k.endswith(':COMPLETED') for k in result.get('counts', {})) else 2
    except Exception as exc:
        # Never print credential-bearing exceptions or provider bodies.
        print(json.dumps({'status': 'FAIL', 'error_type': type(exc).__name__}))
        return 1
    finally:
        try:
            from routing_trace_bridge import flush
            flush()
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
