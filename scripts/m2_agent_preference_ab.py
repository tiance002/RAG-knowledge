"""Fresh, isolated real-provider A/B. Preflight never calls a provider.

Retrieval runs independently for each trial; compare captured events and requests,
not merely configuration. This is not a fixed-context experiment.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / 'var/preferences-acceptance'
QIDS = ('m2-supp-current:quarterly-report-english-procedure-v1', 'office50:031',
        'hotpotqa:5a729fe55542991f9a20c53b')
MAX_CALLS = 80


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


previous = load(ROOT / 'scripts/m2_hybrid_postfix_continuation.py', 'm2_previous_ab')
old = previous.old


def unknown_hashes(root):
    """Read old durable markers; pending, transport-unknown and 503 are forbidden.

    Missing request bodies make the evidence insufficient, so fail closed.
    """
    found = set()
    for marker in Path(root).rglob('*.started.json'):
        if marker.parent.name != 'calls':
            continue  # Ignore experiment grants, which are not provider receipts.
        stem = marker.name[:-len('.started.json')]
        finish = marker.with_name(stem + '.finished.json')
        row = json.loads(finish.read_text(encoding='utf8')) if finish.exists() else {}
        if row.get('state') in ('UNKNOWN', 'UNKNOWN_PENDING') or not finish.exists() or row.get('status_code') == 503:
            start = json.loads(marker.read_text(encoding='utf8'))
            request = marker.with_name(stem + '.request.json')
            if not request.exists() or hashlib.sha256(request.read_bytes()).hexdigest() != start.get('request_sha256'):
                raise ValueError('Historical unknown request cannot be verified')
            found.add(request_fingerprint(request.read_bytes()))
    return sorted(found)


def request_fingerprint(body):
    value = json.loads(body)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def continuation_proof(directory):
    directory = Path(directory).resolve(strict=True)
    if directory.parent != VAR.resolve() or not directory.name.startswith('m2-agent-preference-ab-'):
        raise ValueError('Prior run must be a private experiment directory')
    def rows(name):
        return [json.loads(line) for line in (directory / name).read_text(encoding='utf8').splitlines() if line]
    previous_plan = json.loads((directory / 'plan.json').read_bytes())
    started, trials, summary = rows('started.jsonl'), rows('trials.jsonl'), rows('summary.jsonl')
    if (previous_plan.get('schema') != 'm2-agent-preference-ab/v1'
            or previous_plan.get('continuation') is not None
            or previous_plan.get('run_id') != directory.name
            or previous_plan['sources'] != old.snapshot()
            or previous_plan['runner_sha256'] != previous.sha(VAR / 'm2-agent-preference-ab-original-runner-v1.py')
            or previous_plan['manifest_sha256'] != previous.sha(old.MANIFEST)
            or previous_plan['supplement_sha256'] != previous.sha(old.SUPPLEMENT)
            or len(started) != 2 or started != previous_plan['identities'][:2]
            or len(trials) != 2 or [x['state'] for x in trials] != ['RETURNED', 'STOPPED']
            or len(summary) != 1 or summary[0]['state'] != 'STOPPED'
            or summary[0]['calls'] != 6):
        raise ValueError('Previous run identities, source or terminal state differ')
    if [(x['qid'], x['mode'], x['arm']) for x in started] != [(QIDS[0], 'chat', 'A'), (QIDS[0], 'chat', 'B')]:
        raise ValueError('Unexpected previous started identities')
    receipts = sorted((directory / 'calls').glob('*.started.json'))
    if len(receipts) != 6:
        raise ValueError('Unexpected previous receipt count')
    statuses = []
    for marker in receipts:
        sent = json.loads(marker.read_bytes())
        request = marker.with_name(marker.name.replace('.started.json', '.request.json'))
        finish = marker.with_name(marker.name.replace('.started.json', '.finished.json'))
        if not request.exists() or not finish.exists() or previous.sha(request) != sent['request_sha256']:
            raise ValueError('Previous receipt incomplete')
        result = json.loads(finish.read_bytes())
        statuses.append((result['state'], result.get('status_code')))
    if statuses.count(('COMPLETED', 200)) != 5 or statuses.count(('FAILED_HTTP', 503)) != 1:
        raise ValueError('Unexpected previous provider outcomes')
    files = [directory / n for n in ('plan.json', 'started.jsonl', 'trials.jsonl', 'summary.jsonl', 'usage.jsonl', 'policies.jsonl', 'events.jsonl')]
    files += sorted(p for p in (directory / 'calls').iterdir() if p.is_file())
    return {'directory': str(directory), 'previous_run_id': directory.name,
            'prior_runner_sha256': previous_plan['runner_sha256'],
            'evidence_sha256': {str(p.relative_to(directory)).replace('\\', '/'): previous.sha(p) for p in files},
            'excluded': [{'qid': x['qid'], 'mode': x['mode'], 'arm': x['arm']} for x in started]}


def make_plan(run_id, continue_from=None):
    if not run_id.startswith('m2-agent-preference-ab-') or len(run_id) > 100:
        raise ValueError('Invalid fresh run identity')
    prior_plan, proof = previous.prior_check()
    cases = frozen_cases()
    if any(q not in cases for q in QIDS):
        raise ValueError('Frozen cases missing')
    continuation = continuation_proof(continue_from) if continue_from else None
    excluded = {(x['qid'], x['mode'], x['arm']) for x in continuation['excluded']} if continuation else set()
    identities = [{'run_identity': f'{run_id}:{q}:{m}:{a}', 'qid': q, 'mode': m, 'arm': a}
                  for q in QIDS for m in ('chat', 'agent') for a in ('A', 'B') if (q, m, a) not in excluded]
    return {'schema': 'm2-agent-preference-ab/v1', 'run_id': run_id,
            'continuation': continuation, 'identities': identities,
            'preference': {'answer_style': 'detailed'}, 'search': old.app.FIXED_SEARCH,
            'models': prior_plan['models'], 'manifest_sha256': previous.sha(old.MANIFEST),
            'supplement_sha256': previous.sha(old.SUPPLEMENT),
            'source_proof': proof, 'sources': old.snapshot(), 'runner_sha256': previous.sha(Path(__file__)),
            'unknown_request_hashes': unknown_hashes(VAR), 'max_calls': MAX_CALLS,
            'model_calls': 0, 'provider_bill': 'UNKNOWN',
            'context_contract': 'independent retrieval; measure actual context per arm',
            'grant_available': not (VAR / (run_id + '.started')).exists()}


def frozen_cases():
    return {x['qid']: x for x in json.loads(old.MANIFEST.read_bytes())['cases'] + json.loads(old.SUPPLEMENT.read_bytes())['cases']}


class Guard(old.LimitedCalls):
    def __init__(self, directory, plan):
        super().__init__(directory)
        self.frozen = plan

    def begin(self, identity, request):
        digest = request_fingerprint(request.content)
        if (not identity.startswith(self.frozen['run_id'] + ':')
                or digest in self.frozen['unknown_request_hashes']
                or old.snapshot() != self.frozen['sources']
                or previous.sha(old.MANIFEST) != self.frozen['manifest_sha256']
                or previous.sha(old.SUPPLEMENT) != self.frozen['supplement_sha256']
                or previous.sha(Path(__file__)) != self.frozen['runner_sha256']):
            self.stopped = True
            raise old.guardmod.GuardStop('Identity, source or historical unknown request conflict')
        if self.count >= MAX_CALLS:
            self.stopped = True
            raise old.guardmod.GuardStop('Experiment call limit reached')
        return super().begin(identity, request)


class Adapter(old.CappedAdapter):
    def _begin(self, request):
        return self.guard.begin(self.guard.frozen['run_id'] + ':' + self.context + ':call-' + format(next(self.serial), '06d'), request)


async def run(plan, output, adapter, api_key):
    from app.schemas.schemas import Message
    from app.services.chat import ChatService
    from app.services.memory import PreferenceScope, get_memory_service
    from app.services.memory_commands import resolve_display_preferences
    from app.services.trace_schema import capture_scope
    from app.services.vector_store import VectorStoreService
    cases = frozen_cases()
    memory = get_memory_service()
    try:
        async with old.app.bound_provider(adapter, api_key):
            if VectorStoreService().get_count(old.app.KB_ID) != 80:
                raise ValueError('Copied index count differs')
            for item in plan['identities']:
                rid = item['run_identity']
                scope = PreferenceScope(tenant_id='m2-agent-preference-ab', user_id=hashlib.sha256(rid.encode()).hexdigest())
                old.persist(output / 'started.jsonl', item)
                start = time.monotonic()
                try:
                    await memory.reset_preferences(scope=scope)
                    if item['arm'] == 'B':
                        await memory.update_preference(plan['preference'], scope=scope)
                    snapshot = await memory.get_preferences(scope=scope)
                    policy, values, sources = resolve_display_preferences(snapshot, cases[item['qid']]['question'])
                    if bool(policy) != (item['arm'] == 'B') or (item['arm'] == 'B' and values['answer_style'] != 'detailed'):
                        raise ValueError('Preference resolution differs from plan')
                    old.persist(output / 'policies.jsonl', {'run_identity': rid, 'policy': policy, 'values': values, 'sources': sources})
                    adapter.context = f"{item['qid']}:{item['mode']}:{item['arm']}"
                    events = []
                    async def collect():
                        async for event, data in ChatService().chat_stream(messages=[Message(role='user', content=cases[item['qid']]['question'])], kb_ids=[old.app.KB_ID], mode=item['mode'], model_id=None, memory_scope=scope, **old.app.FIXED_SEARCH):
                            row = {'event': event, 'data': data}
                            old.persist(output / 'events.jsonl', {'run_identity': rid, **row})
                            events.append(row)
                    with capture_scope(run_id=plan['run_id'], qid=item['qid'], attempt_id=rid, dataset_version='m2-20261104-dev-v3', capture_mode='BASIC'):
                        await asyncio.wait_for(collect(), 480)
                    if adapter.guard.stopped or not any(e['event'] == 'done' for e in events) or any(e['event'] == 'error' for e in events):
                        raise old.guardmod.GuardStop('Trial incomplete')
                    old.persist(output / 'trials.jsonl', {**item, 'state': 'RETURNED', 'elapsed_seconds': time.monotonic()-start})
                except BaseException as exc:
                    old.persist(output / 'trials.jsonl', {**item, 'state': 'STOPPED', 'error_type': type(exc).__name__, 'elapsed_seconds': time.monotonic()-start})
                    raise
    finally:
        await memory.client.aclose()


def execute(plan, output):
    import httpx
    prior = plan['continuation']['directory'] if plan.get('continuation') else None
    if make_plan(plan['run_id'], prior) != plan or not plan['grant_available']:
        raise ValueError('Plan, evidence or grant changed')
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('Cloud credential absent')
    output = Path(output).resolve()
    if output.parent != VAR.resolve() or output.name != plan['run_id'] or output.exists():
        raise ValueError('Fresh matching private output required')
    if any(n == 'app' or n.startswith('app.') for n in sys.modules):
        raise ValueError('Fresh process required')
    recovery = load(ROOT / 'scripts/m2_hybrid_postfix_recovery.py', 'm2_recovery_checks')
    recovery.verify_redis_env()
    recovery.verify_output_encoding()
    grant = VAR / (plan['run_id'] + '.started')
    with grant.open('xb') as f:
        f.write(hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest().encode())
        f.flush(); os.fsync(f.fileno())
    output.mkdir()
    (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf8')
    calls = Guard(output / 'calls', plan)
    network = old.http.BoundedNetwork()
    ai = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    si = httpx.HTTPTransport(retries=0, trust_env=False)
    adapter = Adapter(calls, old.NetworkTransport(ai, network), old.NetworkTransport(si, network))
    async def go(url):
        try:
            with network.installed(), old.restrict_loopback(url):
                await run(plan, output, adapter, os.environ['M2_AB_API_KEY'])
        finally:
            await ai.aclose(); si.close()
    status = {'state': 'STOPPED', 'provider_bill': 'UNKNOWN'}
    try:
        with old.base.isolated_redis(output) as url:
            old.app.configure_runtime(output, url, os.environ['M2_AB_API_KEY'])
            previous.copy_reuse(output, plan['source_proof'])
            asyncio.run(go(url))
        status['state'] = 'RECORDED_REQUIRES_REVIEW'
    except BaseException as exc:
        status['error_type'] = type(exc).__name__
    finally:
        status['calls'] = calls.count
        for record in old.usage_records(calls.directory):
            old.persist(output / 'usage.jsonl', record)
        old.persist(output / 'summary.jsonl', status)
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('preflight', 'execute'))
    p.add_argument('--plan', required=True)
    p.add_argument('--output')
    p.add_argument('--continue-from')
    args = p.parse_args()
    try:
        path = Path(args.plan).resolve()
        if path.parent != VAR.resolve() or not path.name.startswith('m2-agent-preference-ab-'):
            raise ValueError('Private plan required')
        if args.mode == 'preflight':
            plan = make_plan('m2-agent-preference-ab-' + uuid.uuid4().hex, args.continue_from)
            with path.open('x', encoding='utf8') as f:
                json.dump(plan, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
            result = {'state': 'PASS', 'model_calls': 0, 'run_id': plan['run_id'], 'identities': len(plan['identities'])}
        else:
            if args.continue_from:
                raise ValueError('Continuation is frozen in the plan')
            if not args.output:
                raise ValueError('Fresh output required')
            result = execute(json.loads(path.read_bytes()), args.output)
        print(json.dumps(result))
        return 0 if result['state'] in ('PASS', 'RECORDED_REQUIRES_REVIEW') else 1
    except Exception as exc:
        print(json.dumps({'state': 'BLOCKED', 'error_type': type(exc).__name__, 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
