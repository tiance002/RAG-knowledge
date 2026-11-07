"""One-shot current-code procedural A/D supplement; preflight never sends.

Retains every historical grant and the frozen shared lease unchanged. The new
persistent grant is also the exclusive execution lock; it is never auto-reset.
"""
import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'var/preferences-acceptance'
module = importlib.util.spec_from_file_location('supplement_reconcile', ROOT / 'scripts/m2_current_reconcile.py')
recon = importlib.util.module_from_spec(module)
module.loader.exec_module(recon)
base, cont = recon.base, recon.cont
safety = cont.safety
SCHEMA = 'm2-current-supplement/v1'
GRANT = P / 'm2-current-supplement.started'
LIMIT = Decimal('2')
REMAINING = Decimal('0.2838884')
QID = 'm2-supp-current:quarterly-report-instructions-v1'


def prior_grants():
    paths = list(P.glob('m2-current-resume*-once.started'))
    for folder in ('m2-supplement-locks', 'm2-continuation-locks', 'm2-http-smoke-real-locks'):
        paths += list((P / folder).glob('*.started'))
    return {str(p.relative_to(ROOT)).replace('\\', '/'): recon.sha(p) for p in sorted(paths)}


def no_writers():
    evidence = recon.no_writers()
    if evidence['process_query_exit_code'] != 0:
        raise ValueError('Prior writer check failed closed')
    command = "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_preference_supplement.py|m2_http_real_smoke.py' }).Count) { exit 4 }"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError('Cannot exclude legacy supplement/HTTP writer')
    return evidence


def frozen_parent():
    spec = json.loads((recon.r.RUN / 'manifest.json').read_bytes())
    history, parent, _, _, _ = recon.r.current.validate(spec)
    return history, parent


def case_from(parent):
    chunk = next(x for x in parent['chunks'] if x['id'] == 'word-paragraph-6')
    return {'qid': QID,
            'question': '根据材料中的 INSTRUCTIONS，项目负责人应如何填写每季度的研究项目进度报告？请说明任务范围、进度与完成比例，以及状态说明的要求。',
            'expected_chunk_ids': [chunk['id']], 'source_quote': chunk['content'],
            'answer_points': ['each calendar quarter', 'each task', 'percentage completion',
                              '2 or 3 sentences', 'accomplishments and problems',
                              'even if no work was done'],
            'kb_scope': [base.KB_ID], 'negative': False,
            'gold_locator': 'word / paragraph 6 / INSTRUCTIONS; exact frozen source text'}


def make_spec():
    audit = recon.reconcile()
    _, parent = frozen_parent()
    return {'schema_version': SCHEMA, 'authorized': True,
            'authorization_id': 'm2-owner-20261104-current-procedural-ad-2cny-v1',
            'purpose': 'CURRENT_PROCEDURAL_AD_ONLY', 'global_limit_cny': '2',
            'repetitions': 2, 'case': case_from(parent),
            'reconciliation_sha256': base.digest(base.canonical(audit)),
            'prior_grants': prior_grants(), 'runner_sha256': recon.sha(Path(__file__)),
            'fixed_search': base.FIXED_SEARCH, 'lease_sha256': audit['lease_sha256']}


def validate(spec):
    if (spec.get('schema_version') != SCHEMA or spec.get('authorized') is not True
            or spec.get('authorization_id') != 'm2-owner-20261104-current-procedural-ad-2cny-v1'
            or spec.get('purpose') != 'CURRENT_PROCEDURAL_AD_ONLY'
            or spec.get('global_limit_cny') != '2' or spec.get('repetitions') != 2
            or spec.get('runner_sha256') != recon.sha(Path(__file__))
            or spec.get('fixed_search') != base.FIXED_SEARCH):
        raise ValueError('Supplement authorization/code/configuration mismatch')
    audit = recon.reconcile()
    if (base.digest(base.canonical(audit)) != spec.get('reconciliation_sha256')
            or audit['lease_sha256'] != spec.get('lease_sha256')
            or Decimal(audit['remaining_cny']) != REMAINING
            or Decimal(audit['committed_cny']) + REMAINING != LIMIT
            or audit['unknown_171_full_reservation_cny'] != '0.075968'
            or prior_grants() != spec.get('prior_grants')):
        raise ValueError('Frozen shared history, UNKNOWN, grants or lease changed')
    history, parent = frozen_parent()
    case = case_from(parent)
    if spec.get('case') != case or any(row['identity'][0] == case['qid'] for row in audit['identities']):
        raise ValueError('Procedural source/Gold or disjoint identity changed')
    if any(point.lower() not in case['source_quote'].lower() for point in case['answer_points']):
        raise ValueError('Gold absent from frozen source')
    resolution = {arm: safety.policy_resolution(case['question'], prefs) for arm, prefs in safety.ARMS.items()}
    if resolution['A']['policy_active'] or not resolution['D']['policy_active']:
        raise ValueError('A/D applicability check failed')
    fresh = {**parent, 'cases': [case], 'budget_cny': str(REMAINING),
             'dataset_version': 'm2-current-procedural-ad-20261104-v1'}
    return history, parent, fresh, audit, resolution


def preflight(spec):
    _, _, fresh, audit, resolutions = validate(spec)
    process = no_writers()
    return {'status': 'PASS', 'model_calls': 0, 'planned_trials': 8,
            'prior_committed_cny': audit['committed_cny'], 'remaining_cny': str(REMAINING),
            'global_limit_cny': '2', 'unknown_reserved_cny': audit['unknown_171_full_reservation_cny'],
            'grant_available': not GRANT.exists(), 'provider_bill': 'UNKNOWN',
            'policy_resolutions': resolutions, 'process_check': process,
            'case_qid': fresh['cases'][0]['qid'], 'entry': 'ChatService.chat_stream; no HTTP server',
            'scope': 'A/D procedural supplement; no four-arm or production Hybrid conclusion'}


def claim(spec, output):
    value = base.canonical({'authorization_id': spec['authorization_id'], 'output': str(output),
                            'manifest_sha256': base.digest(base.canonical(spec))})
    with GRANT.open('xb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    return value


class Transport(base.BudgetTransport):
    def __init__(self, fresh, output, spec, grant_bytes, *, transport=None):
        super().__init__(fresh, output, transport=transport)
        self.spec, self.grant_bytes = spec, grant_bytes
        self.allowed = {(fresh['cases'][0]['qid'], mode, arm, rep)
                        for mode in base.MODES for arm in safety.ARMS for rep in (1, 2)}

    def diagnostic(self, code, request):
        record = {'code': code, 'identity': self.context}
        if request is not None:
            payload = json.loads(request.content)
            cap = self.manifest['max_output_tokens']
            payload['max_tokens'] = min(payload.get('max_tokens', cap), cap)
            if 'max_completion_tokens' in payload:
                payload['max_completion_tokens'] = min(payload['max_completion_tokens'], cap)
            if payload.get('stream'):
                payload['stream_options'] = {**payload.get('stream_options', {}), 'include_usage': True}
            body = base.canonical(payload)
            name = f'rejected-request-{len(self.calls) + 1:04d}.json'
            (self.output / name).write_bytes(body)
            record.update(request_path=name, request_sha256=base.digest(body),
                          reservation_cny=str(self.cost(len(body) * 4 + 4096, cap)))
        with (self.output / 'guard-diagnostics.jsonl').open('ab') as stream:
            stream.write(base.canonical(record) + b'\n'); stream.flush(); os.fsync(stream.fileno())

    async def send(self, sender, client, request, *args, **kwargs):
        try:
            validate(self.spec)
            if GRANT.read_bytes() != self.grant_bytes or self.limit != REMAINING:
                raise ValueError('Grant or budget changed')
            if cont.identity(self.context) not in self.allowed:
                raise ValueError('Identity outside one-shot plan')
            path = self.output / 'trials.jsonl'
            if path.exists() and cont.identity(self.context) in cont.indexed_trials(path.read_bytes()):
                raise ValueError('Started identity cannot be replayed')
            no_writers()
        except Exception:
            self.stopped = True
            self.diagnostic('PRE_SEND_VALIDATION_FAILED', None)
            raise base.GuardStop('Supplement frozen history, lease, grant, process or identity changed') from None
        try:
            return await super().send(sender, client, request, *args, **kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc), request)
            raise


def execute(spec, output):
    result = preflight(spec)
    if not result['grant_available']:
        raise FileExistsError('Supplement already claimed; no replay')
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('M2_AB_API_KEY missing; zero sends')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise ValueError('Fresh process required before runtime isolation')
    output = base.private_path(output)
    if output.parent != P or not output.name.startswith('m2-current-supplement-real-') or output.exists():
        raise ValueError('Fresh private supplement output required')
    history, parent, fresh, audit, _ = validate(spec)
    # Atomic permanent grant also excludes competing adapter processes. Existing
    # shared lease stays byte-for-byte intact; consumed old grants prevent sends.
    grant_bytes = claim(spec, output)
    guard = None
    original_cwd, original_plan = Path.cwd(), base.trial_plan
    summary = {'status': 'NOT_RUN', 'planned_trials': 8, 'prior_committed_cny': audit['committed_cny'],
               'lease': 'FROZEN_LEASE_RETAINED_UNCHANGED', 'one_shot_grant': 'RETAINED',
               'entry': result['entry'], 'correctness': 'REQUIRES_INDEPENDENT_REVIEW'}
    try:
        output.mkdir()
        (output / 'manifest.json').write_bytes(base.canonical(spec))
        (output / 'preflight.json').write_bytes(base.canonical(result))
        (output / 'dataset.json').write_bytes(base.canonical(fresh))
        cont.backup_reuse(history, parent, output / 'isolated.db')
        guard = Transport(fresh, output, spec, grant_bytes)
        with base.isolated_redis(output) as redis_url:
            base.configure_runtime(fresh, output, redis_url)
            with (output / 'runtime.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log), guard.installed():
                safety.assert_runtime_isolation(output, redis_url)
                base.trial_plan = safety.supplement_plan
                asyncio.run(base.run_trials(fresh, output, guard))
        summary['status'] = 'RECORDED_REQUIRES_EVIDENCE_REVIEW'
    except base.GuardStop as exc:
        summary.update(status='STOPPED', reason=str(exc))
    except Exception as exc:
        summary.update(status='FAILED', error_type=type(exc).__name__)
    finally:
        base.trial_plan = original_plan
        os.chdir(original_cwd)
        if guard:
            summary.update(guard.summary())
            summary['global_committed_including_unknown_cny'] = str(Decimal(audit['committed_cny']) + guard.committed)
        path = output / 'trials.jsonl'
        summary['recorded_trials'] = len(cont.indexed_trials(path.read_bytes())) if path.exists() else 0
        if output.exists():
            (output / 'summary.json').write_bytes(base.canonical(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'execute'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(base.private_path(args.manifest).read_bytes())
        output = base.private_path(args.output)
        if args.mode == 'preflight':
            result = preflight(spec)
            with output.open('xb') as stream:
                stream.write(base.canonical(result))
        else:
            result = execute(spec, output)
        print(json.dumps({k: result[k] for k in ('status', 'model_calls', 'planned_trials', 'remaining_cny',
                          'recorded_trials', 'global_committed_including_unknown_cny') if k in result}))
        return 0 if result['status'] in ('PASS', 'RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
