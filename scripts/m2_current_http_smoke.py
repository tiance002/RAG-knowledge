"""Two fresh current-code HTTP/SSE D identities under the remaining M2 budget.

Reuses the existing loopback HTTP server/client and model accounting transport.
Historical source, runs, grants and the retained shared lease stay unchanged.
"""
import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'var/preferences-acceptance'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


source = load('http_current_frozen_agents', ROOT / 'scripts/m2_current_supplement_english_agents.py')
http = load('http_current_tcp_primitives', ROOT / 'scripts/m2_http_real_smoke.py')
base, cont = source.base, source.cont
RUN = P / 'm2-current-supplement-real-english-agents-v1'
REPORT = P / 'm2-current-supplement-english-agents-independent-reconciliation-v1.json'
GRANT = P / 'm2-current-http.started'
SCHEMA = 'm2-current-http/v1'
AUTH = 'm2-owner-20261104-current-http-chat-agent-D-v1'
QID = 'm2-http-current:quarterly-report-english-v1'
REMAINING = Decimal('0.2055224')


def identities():
    return [{'qid': QID, 'mode': mode, 'arm': 'D', 'repetition': 1,
             'run_identity': 'm2-http-current:english-D-' + mode + '-v1'} for mode in ('chat', 'agent')]


def audit_prior(expected):
    if source.source.prior.recon.sha(REPORT) != expected:
        raise ValueError('Final independent M2 accounting changed')
    report = json.loads(REPORT.read_bytes())
    for path, sha in report['artifact_sha256'].items():
        if source.source.prior.recon.sha(ROOT / path) != sha:
            raise ValueError('Frozen run, source, grant or shared lease changed')
    spec = json.loads((RUN / 'manifest.json').read_bytes())
    history, parent, fresh, previous, _, _ = source.validate(spec)
    trials = cont.indexed_trials((RUN / 'trials.jsonl').read_bytes())
    calls, _ = source.source.prior.recon.audit_run(RUN, trials,
        cont.rows((RUN / 'provider-ledger.jsonl').read_bytes()), fresh, 15, '0.0576176', (15,))
    if (len(trials) != 4 or len(calls) != 15 or previous['committed_cny'] != '1.7368600'
            or report['global_committed_including_unknown_cny'] != '1.7944776'
            or report['all_unknown_reserved_cny'] != '0.099456'
            or report['remaining_cny'] != str(REMAINING)):
        raise ValueError('Current total or both UNKNOWN reservations mismatch')
    fresh = {**fresh, 'cases': [{**fresh['cases'][0], 'qid': QID}],
             'budget_cny': str(REMAINING), 'dataset_version': 'm2-current-http-20261104-v1'}
    audit = {'committed_cny': '1.7944776', 'remaining_cny': str(REMAINING),
             'global_limit_cny': '2', 'unknown_reserved_cny': '0.099456'}
    return history, parent, fresh, audit


def make_spec():
    audit_prior(source.source.prior.recon.sha(REPORT))
    return {'schema_version': SCHEMA, 'authorized': True, 'authorization_id': AUTH,
            'purpose': 'CURRENT_HTTP_CHAT_AGENT_D_ONLY', 'max_new_http_requests': 2,
            'prior_report_sha256': source.source.prior.recon.sha(REPORT),
            'runner_sha256': source.source.prior.recon.sha(Path(__file__)),
            'http_primitive_sha256': source.source.prior.recon.sha(Path(http.__file__)),
            'identities': identities(), 'fixed_search': base.FIXED_SEARCH,
            'cloud_export_policy': 'OPTIONAL_METADATA_ONLY'}


def validate(spec):
    if (spec.get('schema_version') != SCHEMA or spec.get('authorized') is not True
            or spec.get('authorization_id') != AUTH or spec.get('purpose') != 'CURRENT_HTTP_CHAT_AGENT_D_ONLY'
            or spec.get('max_new_http_requests') != 2 or spec.get('identities') != identities()
            or spec.get('fixed_search') != base.FIXED_SEARCH
            or spec.get('cloud_export_policy') != 'OPTIONAL_METADATA_ONLY'
            or spec.get('runner_sha256') != source.source.prior.recon.sha(Path(__file__))
            or spec.get('http_primitive_sha256') != source.source.prior.recon.sha(Path(http.__file__))):
        raise ValueError('Current HTTP authorization, source or identity changed')
    history, parent, fresh, audit = audit_prior(spec['prior_report_sha256'])
    policy = source.source.prior.safety.policy_resolution(fresh['cases'][0]['question'],
                                                         {'output_preferences': ['step_by_step']})
    if not policy['policy_active']: raise ValueError('D policy not applicable')
    return history, parent, fresh, audit, policy


def preflight(spec, *, cloud=False):
    history, parent, fresh, audit, policy = validate(spec)
    if cloud and not all(os.getenv(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')):
        raise ValueError('Explicit Cloud flag requires existing process-environment credentials before grant')
    return {'status': 'PASS', 'model_calls': 0, 'planned_http_requests': 2,
            'prior_committed_cny': audit['committed_cny'], 'remaining_cny': str(REMAINING),
            'both_unknown_reserved_cny': audit['unknown_reserved_cny'], 'global_limit_cny': '2',
            'policy_resolution': policy, 'grant_available': not GRANT.exists(),
            'retrieval_preflight': source.source.retrieval_preflight(history, parent),
            'cloud': 'EXPLICIT_METADATA_ONLY' if cloud else 'DISABLED',
            'scope': 'Actual loopback HTTP/SSE chat and Agent; two D identities, no A/B claim'}


def claim(spec, output):
    value = base.canonical({'authorization_id': AUTH, 'output': str(output),
                            'manifest_sha256': base.digest(base.canonical(spec))})
    with GRANT.open('xb') as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())
    return value


class Transport(source.Transport):
    def __init__(self, fresh, output, spec, grant_bytes, *, transport=None):
        base.BudgetTransport.__init__(self, fresh, output, transport=transport)
        self.spec, self.grant_bytes = spec, grant_bytes
        self.allowed = {x['run_identity']: x for x in spec['identities']}

    async def send(self, sender, client, request, *args, **kwargs):
        stage = 'HISTORY'
        try:
            validate(self.spec); stage = 'GRANT_OR_BUDGET'
            if GRANT.read_bytes() != self.grant_bytes or self.limit != REMAINING:
                raise ValueError('Current HTTP grant or budget changed')
            stage = 'IDENTITY'; expected = self.allowed.get(self.context.get('run_identity'))
            if expected is None or any(self.context.get(k) != v for k, v in expected.items()):
                raise ValueError('Unapproved current HTTP identity')
            stage = 'TRIAL_RECORD'; path = self.output / 'trials.jsonl'
            if path.exists() and any(x.get('run_identity') == expected['run_identity'] for x in cont.rows(path.read_bytes())):
                raise ValueError('Recorded HTTP identity cannot replay')
        except Exception as exc:
            self.stopped = True
            self.diagnostic('PRE_SEND_' + stage + '_' + type(exc).__name__ + ': ' + str(exc), request)
            raise base.GuardStop('Current HTTP pre-send ' + stage + ' failed') from None
        try: return await base.BudgetTransport.send(self, sender, client, request, *args, **kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc), request); raise


def configure_observability(cloud):
    os.environ.update(RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true' if cloud else 'false',
        RAG_METRICS_ENABLED='false', RAG_TRACE_SCHEMA='e12', RAG_E12_CONTENT_POLICY='LOCAL_ONLY',
        RAG_SNAPSHOTS_ENABLED='false', LANGFUSE_TRACING_ENVIRONMENT='development')


async def serve(fresh, case, spec, output, guard):
    from app.services.memory import get_memory_service
    from app.services.trace_schema import capture_scope
    memory = get_memory_service()
    try:
        await memory.reset_preferences()
        snapshot = await memory.update_preference({'output_preferences': ['step_by_step']})
        (output / 'preference-seed.json').write_bytes(base.canonical(snapshot.model_dump()))
        with capture_scope(run_id=output.name, qid=case['qid'], attempt_id='http-chat-agent-D',
                           dataset_version=fresh['dataset_version'], capture_mode='BASIC'):
            await http.serve(fresh, case, spec, output, guard)
    finally:
        await memory.client.aclose()


def execute(spec, output, *, transport=None, cloud=False):
    check = preflight(spec, cloud=cloud)
    if not check['grant_available']: raise FileExistsError('Current HTTP run already claimed')
    if not os.getenv('M2_AB_API_KEY'): raise ValueError('M2_AB_API_KEY missing; zero sends')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise ValueError('Fresh process required before isolation')
    output = base.private_path(output)
    if output.parent != P or not output.name.startswith('m2-current-http-real-') or output.exists():
        raise ValueError('Fresh private current HTTP output required')
    history, parent, fresh, audit, _ = validate(spec)
    grant_bytes = claim(spec, output); guard = None; previous = Path.cwd()
    summary = {'status': 'NOT_RUN', 'purpose': spec['purpose'], 'planned_http_requests': 2,
        'prior_committed_cny': audit['committed_cny'], 'lease': 'FROZEN_LEASE_RETAINED_UNCHANGED',
        'one_shot_grant': 'RETAINED', 'correctness': 'REQUIRES_INDEPENDENT_REVIEW',
        'cloud': check['cloud'], 'cloud_readback': 'NOT_RUN'}
    try:
        output.mkdir()
        (output / 'manifest.json').write_bytes(base.canonical(spec))
        (output / 'preflight.json').write_bytes(base.canonical(check))
        (output / 'dataset.json').write_bytes(base.canonical(fresh))
        cont.backup_reuse(history, parent, output / 'isolated.db')
        guard = Transport(fresh, output, spec, grant_bytes, transport=transport)
        with base.isolated_redis(output) as url:
            base.configure_runtime(fresh, output, url)
            configure_observability(cloud)
            with (output / 'runtime.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
                with guard.installed():
                    source.source.prior.safety.assert_runtime_isolation(output, url)
                    asyncio.run(serve(fresh, fresh['cases'][0], spec, output, guard))
        summary['status'] = 'RECORDED_REQUIRES_EVIDENCE_REVIEW'
    except (base.GuardStop, http.base.GuardStop) as exc:
        summary.update(status='STOPPED', reason=str(exc))
    except Exception as exc: summary.update(status='FAILED', error_type=type(exc).__name__)
    finally:
        os.chdir(previous)
        if cloud and 'app.services.observability' in sys.modules:
            sys.modules['app.services.observability'].flush_observations()
            summary['cloud_flush'] = 'REQUESTED; helper fail-open, readback required'
        if guard:
            summary.update(guard.summary())
            summary['global_committed_including_unknown_cny'] = str(Decimal(audit['committed_cny']) + guard.committed)
        path = output / 'trials.jsonl'
        summary['recorded_http_requests'] = len(cont.rows(path.read_bytes())) if path.exists() else 0
        if output.exists(): (output / 'summary.json').write_bytes(base.canonical(summary))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('mode', choices=('preflight', 'execute'))
    p.add_argument('--manifest', required=True); p.add_argument('--output', required=True)
    p.add_argument('--cloud', action='store_true', help='Explicit metadata-only Cloud; existing environment credentials required')
    args = p.parse_args()
    try:
        spec = json.loads(base.private_path(args.manifest).read_bytes()); output = base.private_path(args.output)
        if args.mode == 'preflight':
            result = preflight(spec, cloud=args.cloud)
            with output.open('xb') as stream: stream.write(base.canonical(result))
        else: result = execute(spec, output, cloud=args.cloud)
        print(json.dumps({k: result[k] for k in ('status', 'model_calls', 'planned_http_requests', 'remaining_cny',
            'recorded_http_requests', 'global_committed_including_unknown_cny', 'cloud') if k in result}))
        return 0 if result['status'] in ('PASS', 'RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)})); return 1


if __name__ == '__main__': raise SystemExit(main())
