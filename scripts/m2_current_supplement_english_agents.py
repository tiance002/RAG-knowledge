"""Only four never-started English Agent identities, with the same frozen case.

All earlier grants and the retained shared lease remain immutable. A fresh
atomic permanent grant excludes competing continuations. Process-name scans
are initial advisory evidence, never a repeated dispatch gate.
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
s = importlib.util.spec_from_file_location('english_agents_frozen_source', ROOT / 'scripts/m2_current_supplement_english.py')
source = importlib.util.module_from_spec(s); s.loader.exec_module(source)
base, cont = source.base, source.cont
RUN = P / 'm2-current-supplement-real-english-v1'
REPORT = P / 'm2-current-supplement-english-independent-reconciliation-v1.json'
GRANT = P / 'm2-current-supplement-english-agents.started'
SCHEMA = 'm2-current-english-agent-continuation/v1'
AUTH = 'm2-owner-20261104-english-never-started-agents-v1'
REMAINING = Decimal('0.2631400')


def audit_prior(report_sha256):
    if source.prior.recon.sha(REPORT) != report_sha256:
        raise ValueError('English reconciliation changed')
    report = json.loads(REPORT.read_bytes())
    for path, expected in report['artifact_sha256'].items():
        if source.prior.recon.sha(ROOT / path) != expected:
            raise ValueError('Frozen source, grant, lease or English result changed')
    spec = json.loads((RUN / 'manifest.json').read_bytes())
    history, parent, fresh, previous, policy = source.validate(spec)
    trials = cont.indexed_trials((RUN / 'trials.jsonl').read_bytes())
    calls, _ = source.prior.recon.audit_run(RUN, trials, cont.rows((RUN / 'provider-ledger.jsonl').read_bytes()), fresh, 3, '0.0140644')
    plan = list(source.prior.safety.supplement_plan(fresh))
    remaining = {(c['qid'], mode, arm, rep) for c, mode, rep, arm, prefs in plan} - set(trials)
    expected = {(source.QID, 'agent', arm, rep) for arm in ('A', 'D') for rep in (1, 2)}
    if (remaining != expected or len(calls) != 3 or len(trials) != 4
            or previous['committed_cny'] != '1.7227956'
            or report['global_committed_including_unknown_cny'] != '1.7368600'
            or report['remaining_cny'] != str(REMAINING)
            or trials[(source.QID, 'chat', 'D', 2)]['state'] != 'GUARD_STOP'
            or trials[(source.QID, 'chat', 'D', 2)]['provider_call_ids'] != []):
        raise ValueError('Only four never-started Agent identities may continue')
    audit = {**previous, 'committed_cny': '1.7368600', 'remaining_cny': str(REMAINING)}
    return history, parent, {**fresh, 'budget_cny': str(REMAINING)}, audit, policy, remaining


def initial_process_check():
    names = r'm2_current_ab\.py|m2_preference_continue\.py|m2_current_resume(?:16|11)?\.py|m2_preference_supplement\.py|m2_http_real_smoke\.py|m2_current_supplement\.py|m2_current_supplement_english\.py'
    command = "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match '" + names + "' }).Count) { exit 4 }"
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, timeout=15)
        code = result.returncode
        return {'exit_code': code, 'status': {0: 'NO_MATCH_AT_CHECK_TIME', 3: 'INACCESSIBLE_COMMAND_LINES',
                4: 'MATCHED_COMMAND_LINE_NOT_PROOF_OF_WRITER'}.get(code, 'PROCESS_QUERY_FAILED'),
                'role': 'INITIAL_ADVISORY_ONLY; immutable grants and atomic one-shot lock enforce dispatch safety'}
    except subprocess.TimeoutExpired:
        return {'exit_code': None, 'status': 'PROCESS_QUERY_TIMEOUT', 'role': 'INITIAL_ADVISORY_ONLY'}


def make_spec():
    _, _, _, _, _, allowed = audit_prior(source.prior.recon.sha(REPORT))
    return {'schema_version': SCHEMA, 'authorized': True, 'authorization_id': AUTH,
            'purpose': 'ONLY_FOUR_NEVER_STARTED_ENGLISH_AGENTS', 'global_limit_cny': '2',
            'prior_report_sha256': source.prior.recon.sha(REPORT),
            'runner_sha256': source.prior.recon.sha(Path(__file__)),
            'allowed_identities': [list(x) for x in sorted(allowed)], 'fixed_search': base.FIXED_SEARCH}


def validate(spec):
    if (spec.get('schema_version') != SCHEMA or spec.get('authorized') is not True
            or spec.get('authorization_id') != AUTH or spec.get('purpose') != 'ONLY_FOUR_NEVER_STARTED_ENGLISH_AGENTS'
            or spec.get('global_limit_cny') != '2' or spec.get('fixed_search') != base.FIXED_SEARCH
            or spec.get('runner_sha256') != source.prior.recon.sha(Path(__file__))):
        raise ValueError('Agent continuation authorization, code or configuration mismatch')
    result = audit_prior(spec['prior_report_sha256'])
    if spec.get('allowed_identities') != [list(x) for x in sorted(result[-1])]:
        raise ValueError('Continuation contains started or unplanned identity')
    return result


def preflight(spec):
    history, parent, fresh, audit, policy, allowed = validate(spec)
    retrieval = source.retrieval_preflight(history, parent)
    return {'status': 'PASS', 'model_calls': 0, 'planned_trials': 4,
            'prior_committed_cny': audit['committed_cny'], 'remaining_cny': str(REMAINING),
            'global_limit_cny': '2', 'unknown_reserved_cny': audit['unknown_171_full_reservation_cny'],
            'grant_available': not GRANT.exists(), 'provider_bill': 'UNKNOWN',
            'allowed_identities': [list(x) for x in sorted(allowed)], 'policy_resolutions': policy,
            'retrieval_preflight': retrieval, 'initial_process_check': initial_process_check(),
            'per_send_process_name_gate': 'DISABLED; immutable consumed grants/lease plus fresh atomic grant retained'}


def claim(spec, output):
    value = base.canonical({'authorization_id': AUTH, 'output': str(output),
                            'manifest_sha256': base.digest(base.canonical(spec))})
    with GRANT.open('xb') as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())
    return value


def agent_plan(manifest):
    return (x for x in source.prior.safety.supplement_plan(manifest) if x[1] == 'agent')


class Transport(source.Transport):
    def __init__(self, fresh, output, spec, grant_bytes, *, transport=None):
        super().__init__(fresh, output, spec, grant_bytes, transport=transport)
        self.allowed = {tuple(x) for x in spec['allowed_identities']}

    async def send(self, sender, client, request, *args, **kwargs):
        stage = 'HISTORY'
        try:
            validate(self.spec)
            stage = 'GRANT_OR_BUDGET'
            if GRANT.read_bytes() != self.grant_bytes or self.limit != REMAINING:
                raise ValueError('Grant or remaining budget changed')
            stage = 'IDENTITY'
            if cont.identity(self.context) not in self.allowed:
                raise ValueError('Only never-started Agent identities may send')
            stage = 'TRIAL_RECORD'
            path = self.output / 'trials.jsonl'
            if path.exists() and cont.identity(self.context) in cont.indexed_trials(path.read_bytes()):
                raise ValueError('Started identity cannot replay')
        except Exception as exc:
            self.stopped = True
            self.diagnostic('PRE_SEND_' + stage + '_' + type(exc).__name__ + ': ' + str(exc), request)
            raise base.GuardStop('Agent continuation pre-send ' + stage + ' validation failed') from None
        try:
            return await base.BudgetTransport.send(self, sender, client, request, *args, **kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc), request); raise


def execute(spec, output):
    check = preflight(spec)
    if not check['grant_available']: raise FileExistsError('Agent continuation already claimed')
    if not os.getenv('M2_AB_API_KEY'): raise ValueError('M2_AB_API_KEY missing; zero sends')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise ValueError('Fresh process required')
    output = base.private_path(output)
    if output.parent != P or not output.name.startswith('m2-current-supplement-real-english-agents-') or output.exists():
        raise ValueError('Fresh private Agent continuation output required')
    history, parent, fresh, audit, _, _ = validate(spec)
    grant_bytes = claim(spec, output); guard = None
    cwd, original_plan = Path.cwd(), base.trial_plan
    summary = {'status': 'NOT_RUN', 'planned_trials': 4, 'prior_committed_cny': audit['committed_cny'],
               'lease': 'FROZEN_LEASE_RETAINED_UNCHANGED', 'one_shot_grant': 'RETAINED',
               'entry': 'ChatService.chat_stream; no HTTP server', 'correctness': 'REQUIRES_INDEPENDENT_REVIEW',
               'initial_process_check': check['initial_process_check'], 'per_send_process_name_gate': 'DISABLED'}
    try:
        output.mkdir()
        (output / 'manifest.json').write_bytes(base.canonical(spec))
        (output / 'preflight.json').write_bytes(base.canonical(check))
        (output / 'dataset.json').write_bytes(base.canonical(fresh))
        cont.backup_reuse(history, parent, output / 'isolated.db')
        guard = Transport(fresh, output, spec, grant_bytes)
        with base.isolated_redis(output) as url:
            base.configure_runtime(fresh, output, url)
            with (output / 'runtime.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log), guard.installed():
                source.prior.safety.assert_runtime_isolation(output, url)
                base.trial_plan = agent_plan
                asyncio.run(base.run_trials(fresh, output, guard))
        summary['status'] = 'RECORDED_REQUIRES_EVIDENCE_REVIEW'
    except base.GuardStop as exc: summary.update(status='STOPPED', reason=str(exc))
    except Exception as exc: summary.update(status='FAILED', error_type=type(exc).__name__)
    finally:
        base.trial_plan = original_plan; os.chdir(cwd)
        if guard:
            summary.update(guard.summary())
            summary['global_committed_including_unknown_cny'] = str(Decimal(audit['committed_cny']) + guard.committed)
        path = output / 'trials.jsonl'
        summary['recorded_trials'] = len(cont.indexed_trials(path.read_bytes())) if path.exists() else 0
        if output.exists(): (output / 'summary.json').write_bytes(base.canonical(summary))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('mode', choices=('preflight', 'execute'))
    p.add_argument('--manifest', required=True); p.add_argument('--output', required=True); args = p.parse_args()
    try:
        spec = json.loads(base.private_path(args.manifest).read_bytes()); output = base.private_path(args.output)
        if args.mode == 'preflight':
            result = preflight(spec)
            with output.open('xb') as stream: stream.write(base.canonical(result))
        else: result = execute(spec, output)
        print(json.dumps({k: result[k] for k in ('status', 'model_calls', 'planned_trials', 'remaining_cny',
                         'recorded_trials', 'global_committed_including_unknown_cny') if k in result}))
        return 0 if result['status'] in ('PASS', 'RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)})); return 1


if __name__ == '__main__': raise SystemExit(main())
