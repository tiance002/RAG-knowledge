"""Fresh M2 parser-version evaluation under the reconciled aggregate budget.

Preflight is offline. Execute requires an immutable, explicit manifest and a fresh
process; paid calls are never made by preflight or tests.
"""
import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

MODULE = importlib.util.spec_from_file_location('m2_current_continue', Path(__file__).with_name('m2_preference_continue.py'))
cont = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(cont)
base = cont.base
SCHEMA = 'm2-current-ab/v1'
LIMIT = Decimal('2')


def code_snapshot():
    paths = sorted((base.ROOT / 'backend/app').rglob('*.py'))
    paths += [Path(__file__), Path(cont.__file__), Path(cont.safety.__file__), Path(base.__file__)]
    return {str(p.relative_to(base.ROOT)).replace('\\', '/'): base.digest(p.read_bytes()) for p in paths}


def trial_plan(manifest):
    for repetition in (1, 2):
        for case in manifest["cases"]:
            for mode in base.MODES:
                for arm, preferences in base.ARMS.items():
                    yield case, mode, repetition, arm, preferences


def dataset_snapshot(fresh):
    return {"dataset_version": fresh["dataset_version"], "cases": fresh["cases"],
            "sources": fresh["sources"], "chunks": fresh["chunks"],
            "repetitions": fresh["repetitions"], "arms": base.ARMS, "modes": list(base.MODES),
            "fixed_search": base.FIXED_SEARCH, "model": fresh["model"],
            "max_output_tokens": fresh["max_output_tokens"]}


def validate(spec):
    if (spec.get('schema_version') != SCHEMA or spec.get('authorized') is not True
            or spec.get('purpose') != 'CURRENT_PARSER_FRESH_96'
            or not spec.get('authorization_id') or not spec.get('identity_prefix')
            or Decimal(str(spec.get('global_limit_cny'))) != LIMIT):
        raise ValueError('Invalid fresh-run authorization')
    history_path = base.private_path(spec['history_path'])
    raw = cont.read_hashed(history_path, spec['history_sha256'])
    history = json.loads(raw)
    parent, old_plan, budget = cont.verify_history(history)
    if old_plan or Decimal(budget['prior_combined_committed_cny']) != Decimal('0.8010920'):
        raise ValueError('Prior history differs from reconciled baseline')
    cont.validate_reuse(history, parent)
    if spec.get('code_snapshot') != code_snapshot():
        raise ValueError('Source code snapshot changed')
    if spec.get('fixed_search') != base.FIXED_SEARCH or len(parent['cases']) != 6:
        raise ValueError('Retrieval configuration or cases differ from frozen plan')
    if parent['max_output_tokens'] != 4096 or parent['model'] != 'zai-org/GLM-5.3-Flash':
        raise ValueError('Model or cap differs from authorization')
    fresh = {**parent, 'dataset_version': spec['identity_prefix'],
             'budget_cny': str(LIMIT - Decimal(budget['prior_combined_committed_cny'])),
             'cases': [{**case, 'qid': spec['identity_prefix'] + ':' + case['qid']} for case in parent['cases']]}
    if spec.get("dataset_sha256") != base.digest(base.canonical(dataset_snapshot(fresh))):
        raise ValueError("Frozen derived dataset hash mismatch")
    plan = list(trial_plan(fresh))
    ids = [cont.identity({'qid': c['qid'], 'mode': m, 'arm': a, 'repetition': r}) for c,m,r,a,_ in plan]
    if len(ids) != 96 or len(set(ids)) != 96 or any(c['qid'] in {x['qid'] for x in parent['cases']} for c in fresh['cases']):
        raise ValueError('Fresh identities are not unique')
    return history, parent, fresh, budget, plan


class CurrentTransport(base.BudgetTransport):
    def __init__(self, manifest, output, spec, lease_path, lease, allowed, *, transport=None):
        super().__init__(manifest, output, transport=transport)
        self.spec, self.lease_path, self.lease = spec, lease_path, lease
        self.allowed = set(allowed)

    async def send(self, *args, **kwargs):
        try:
            _, _, fresh, _, plan = validate(self.spec)
            if (not self.lease_path.exists() or self.lease_path.read_bytes() != self.lease
                    or self.limit != Decimal(fresh['budget_cny'])
                    or self.allowed != {cont.identity({'qid': c['qid'], 'mode': m, 'arm': a, 'repetition': r}) for c,m,r,a,_ in plan}
                    or cont.identity(self.context) not in self.allowed):
                raise ValueError('Lease, budget or identity changed')
            trials = self.output / 'trials.jsonl'
            if trials.exists() and cont.identity(self.context) in cont.indexed_trials(trials.read_bytes()):
                raise ValueError('Completed or failed identity cannot be reissued')
        except Exception:
            self.stopped = True
            raise base.GuardStop('Pre-send history, code, lease or identity validation failed') from None
        return await super().send(*args, **kwargs)


def preflight(spec):
    _, _, fresh, budget, plan = validate(spec)
    return {'status': 'PASS', 'model_calls': 0, 'planned_trials': len(plan),
            'global_limit_cny': str(LIMIT), 'prior_committed_cny': budget['prior_combined_committed_cny'],
            'remaining_cny': fresh['budget_cny'], 'provider_bill': 'UNKNOWN',
            'fixed_search': base.FIXED_SEARCH, 'max_output_tokens': fresh['max_output_tokens'],
            'dataset_version': fresh['dataset_version'], 'ordering': 'repetition, case, mode, arm', 'supplement': 'NOT RUN'}


def execute(spec, output):
    result = preflight(spec)
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('M2_AB_API_KEY missing; zero sends')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Fresh process required')
    history, parent, fresh, budget, plan = validate(spec)
    lock_dir = base.private_path(base.ROOT / 'var/preferences-acceptance/m2-continuation-locks')
    lock_dir.mkdir(parents=True, exist_ok=True)
    active = lock_dir / (history['parent_manifest_sha256'] + '.active')
    grant = lock_dir / ('current-' + history['parent_manifest_sha256'] + '.started')
    lease = secrets.token_hex(24).encode()
    with active.open('xb') as stream:
        stream.write(lease); stream.flush(); os.fsync(stream.fileno())
    retained = False
    guard = None
    original_cwd = Path.cwd()
    try:
        if grant.exists():
            raise FileExistsError('Fresh run for this historical chain already started')
        output.mkdir(parents=True, exist_ok=False)
        (output / 'manifest.json').write_bytes(base.canonical(spec))
        (output / 'preflight.json').write_bytes(base.canonical(result))
        (output / 'dataset.json').write_bytes(base.canonical(dataset_snapshot(fresh)))
        cont.backup_reuse(history, parent, output / 'isolated.db')
        allowed = {cont.identity({'qid': c['qid'], 'mode': m, 'arm': a, 'repetition': r}) for c,m,r,a,_ in plan}
        guard = CurrentTransport(fresh, output, spec, active, lease, allowed)
        summary = {'status': 'NOT_RUN', 'prior_budget': budget, 'planned_trials': len(plan),
                   'fixed_search': base.FIXED_SEARCH, 'supplement': 'NOT RUN'}
        try:
            with base.isolated_redis(output) as redis_url:
                base.configure_runtime(fresh, output, redis_url)
                with (output / 'runtime.log').open('w', encoding='utf-8') as log:
                    with redirect_stdout(log), redirect_stderr(log), guard.installed():
                        cont.safety.assert_runtime_isolation(output, redis_url)
                        with grant.open('xb') as stream:
                            stream.write(base.canonical({'authorization_id': spec['authorization_id'], 'output': str(output),
                                                         'spec_sha256': base.digest(base.canonical(spec))}))
                            stream.flush(); os.fsync(stream.fileno())
                        original_plan = base.trial_plan
                        try:
                            base.trial_plan = trial_plan
                            asyncio.run(base.run_trials(fresh, output, guard))
                        finally:
                            base.trial_plan = original_plan
            summary['status'] = 'RECORDED_REQUIRES_EVIDENCE_REVIEW'
        except base.GuardStop as exc:
            summary.update(status='STOPPED', stop_reason=str(exc))
        except Exception as exc:
            summary.update(status='FAILED', error_type=type(exc).__name__)
        finally:
            summary.update(guard.summary())
            summary['global_committed_including_unknown_cny'] = str(Decimal(budget['prior_combined_committed_cny']) + guard.committed)
            trials = output / 'trials.jsonl'
            summary['recorded_trials'] = len(cont.rows(trials.read_bytes())) if trials.exists() else 0
            retained = bool(guard.calls)
            summary['lease'] = 'RETAINED_PENDING_RECONCILIATION' if retained else 'RELEASED_IF_OWNED'
            (output / 'summary.json').write_bytes(base.canonical(summary))
        return summary
    finally:
        os.chdir(original_cwd)
        retained = retained or bool(guard and guard.calls)
        if not retained and active.exists() and active.read_bytes() == lease:
            active.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'execute'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        spec = json.loads(base.private_path(args.manifest).read_bytes())
        output = base.private_path(args.output)
        result = preflight(spec) if args.mode == 'preflight' else execute(spec, output)
        print(json.dumps(result, ensure_ascii=False))
        if result.get('status') not in ('PASS', 'RECORDED_REQUIRES_EVIDENCE_REVIEW'):
            return 1
        if args.mode == 'execute' and result.get('recorded_trials') != result.get('planned_trials'):
            return 1
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)}, ensure_ascii=False))
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
