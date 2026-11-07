"""Distinct English procedural supplement after the frozen stopped first run.

Reuses the first adapter in a private module with explicit version-specific
validation hooks. The separately loaded prior module remains unmodified and
validates the first run. No frozen file, grant or shared lease is rewritten.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'var/preferences-acceptance'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/m2_current_supplement.py')
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


prior, runner = load('english_prior_frozen'), load('english_execution_adapter')
base, cont = runner.base, runner.cont
RUN = P / 'm2-current-supplement-real-v1'
REPORT = P / 'm2-current-supplement-independent-reconciliation-v1.json'
GRANT = P / 'm2-current-supplement-english.started'
SCHEMA = 'm2-current-supplement-english/v1'
REMAINING = Decimal('0.2772044')
QID = 'm2-supp-current:quarterly-report-english-procedure-v1'
QUESTION = '根据材料：How to prepare the quarterly progress report for each task, including schedule, completion percentage and current status?'
AUTH = 'm2-owner-20261104-current-english-procedural-ad-2cny-v1'


def audit_prior(report_sha256):
    if prior.recon.sha(REPORT) != report_sha256:
        raise ValueError('Independent first-supplement reconciliation changed')
    report = json.loads(REPORT.read_bytes())
    for path, expected in report['artifact_sha256'].items():
        if prior.recon.sha(ROOT / path) != expected:
            raise ValueError('First supplement artifact, grant or lease changed')
    first_spec = json.loads((RUN / 'manifest.json').read_bytes())
    history, parent, first, original, _ = prior.validate(first_spec)
    trials = cont.indexed_trials((RUN / 'trials.jsonl').read_bytes())
    calls, _ = prior.recon.audit_run(RUN, trials, cont.rows((RUN / 'provider-ledger.jsonl').read_bytes()), first, 5, '0.0066840')
    if (report['global_committed_including_unknown_cny'] != '1.7227956'
            or report['remaining_cny'] != str(REMAINING)
            or len(trials) != 5 or len(calls) != 5
            or original['committed_cny'] != '1.7161116'):
        raise ValueError('Supplement combined accounting mismatch')
    combined = {**original, 'committed_cny': '1.7227956', 'remaining_cny': str(REMAINING)}
    return history, parent, combined, trials


def case_from(parent):
    return {**prior.case_from(parent), 'qid': QID, 'question': QUESTION}


def retrieval_preflight(history, parent):
    """Read the existing frozen SQLite/BM25 index; never copy or write it."""
    source = base.private_path(history['source_db'])
    before = prior.recon.sha(source)
    code = r'''
import hashlib,json,sqlite3,socket,sys
from pathlib import Path
def deny(*a,**kw):raise RuntimeError('Network forbidden in read-only retrieval preflight')
socket.socket.connect=deny;socket.socket.connect_ex=deny
sys.path.insert(0,sys.argv[1])
from sqlalchemy import create_engine
from sqlmodel import Session
from app.services.bm25 import BM25Index
from app.models import DocumentChunk
from app.core.database import engine
assert str(engine.url)=='sqlite://'
data=json.load(sys.stdin);source=Path(data['source']);uri=source.as_uri()+'?mode=ro'
db=create_engine('sqlite://',creator=lambda:sqlite3.connect(uri,uri=True))
with Session(db) as session:
 results=BM25Index().search(session,data['question'],data['kb_id'],top_k=10)
 gold=session.get(DocumentChunk,'word-paragraph-6')
 assert gold and gold.content==data['quote']
 print(json.dumps({'status':'PASS','model_calls':0,'access':'SQLite mode=ro','results':results,'gold_rank':next((i+1 for i,r in enumerate(results) if r['chunk_id']=='word-paragraph-6'),None),'full_gold_chunk_matches_frozen_quote':True},ensure_ascii=False))
db.dispose()
'''
    environment = {**os.environ, 'DATABASE_URL': 'sqlite://', 'ENV': 'test',
                   'REDIS_URL': 'redis://127.0.0.1:1/0', 'MEMORY_REDIS_URL': 'redis://127.0.0.1:1/0',
                   'LANGFUSE_ENABLED': 'false', 'RAG_OBSERVABILITY_ENABLED': 'false', 'PYTHONIOENCODING': 'utf-8'}
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'backend')],
        input=json.dumps({'source': str(source), 'question': QUESTION, 'kb_id': base.KB_ID,
                          'quote': case_from(parent)['source_quote']}),
        capture_output=True, text=True, encoding='utf-8', timeout=30, env=environment)
    if result.returncode:
        raise ValueError('Read-only retrieval preflight failed')
    evidence = json.loads(result.stdout.splitlines()[-1])
    if evidence['gold_rank'] != 1 or prior.recon.sha(source) != before or before != history['source_db_sha256']:
        raise ValueError('Gold not first or frozen SQLite changed')
    return {**evidence, 'process_exit_code': result.returncode, 'source_path': str(source),
            'source_sha256_before': before, 'source_sha256_after': before,
            'delivery_limit': 'Retrieval preflight; actual provider evidence delivery still requires request review'}


def make_spec():
    history, parent, _, _ = audit_prior(prior.recon.sha(REPORT))
    return {'schema_version': SCHEMA, 'authorized': True, 'authorization_id': AUTH,
            'purpose': 'DISTINCT_ENGLISH_PROCEDURAL_AD_ONLY', 'global_limit_cny': '2',
            'repetitions': 2, 'case': case_from(parent), 'prior_report_sha256': prior.recon.sha(REPORT),
            'runner_sha256': prior.recon.sha(Path(__file__)), 'fixed_search': base.FIXED_SEARCH,
            'retrieval_preflight': retrieval_preflight(history, parent)}


def validate(spec):
    if (spec.get('schema_version') != SCHEMA or spec.get('authorized') is not True
            or spec.get('authorization_id') != AUTH or spec.get('purpose') != 'DISTINCT_ENGLISH_PROCEDURAL_AD_ONLY'
            or spec.get('global_limit_cny') != '2' or spec.get('repetitions') != 2
            or spec.get('runner_sha256') != prior.recon.sha(Path(__file__))
            or spec.get('fixed_search') != base.FIXED_SEARCH):
        raise ValueError('English supplement authorization/code/configuration mismatch')
    history, parent, combined, old_trials = audit_prior(spec['prior_report_sha256'])
    case = case_from(parent)
    if (spec.get('case') != case or case['qid'] in {key[0] for key in old_trials}
            or case['question'] == json.loads((RUN / 'manifest.json').read_bytes())['case']['question']):
        raise ValueError('Distinct question or source/Gold mismatch')
    retrieval = spec.get('retrieval_preflight', {})
    if (retrieval.get('status') != 'PASS' or retrieval.get('gold_rank') != 1
            or retrieval.get('source_sha256_before') != history['source_db_sha256']
            or retrieval.get('source_sha256_after') != history['source_db_sha256']
            or retrieval.get('full_gold_chunk_matches_frozen_quote') is not True):
        raise ValueError('Frozen full-source retrieval preflight missing')
    policy = {arm: prior.safety.policy_resolution(QUESTION, prefs) for arm, prefs in prior.safety.ARMS.items()}
    if policy['A']['policy_active'] or not policy['D']['policy_active']:
        raise ValueError('English procedural D policy not applicable')
    fresh = {**parent, 'cases': [case], 'budget_cny': str(REMAINING),
             'dataset_version': 'm2-current-english-procedural-ad-20261104-v1'}
    return history, parent, fresh, combined, policy


def no_writers():
    evidence = prior.no_writers()
    command = "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_current_supplement.py' }).Count) { exit 4 }"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError('Cannot exclude first supplement writer; exit=' + str(result.returncode))
    return {**evidence, 'first_supplement_process_query_exit_code': result.returncode}


def preflight(spec):
    history, parent, fresh, audit, policy = validate(spec)
    retrieval = retrieval_preflight(history, parent)
    if retrieval != spec['retrieval_preflight']:
        raise ValueError('Read-only retrieval differs from frozen preflight')
    return {'status': 'PASS', 'model_calls': 0, 'planned_trials': 8,
            'prior_committed_cny': audit['committed_cny'], 'remaining_cny': str(REMAINING),
            'global_limit_cny': '2', 'unknown_reserved_cny': audit['unknown_171_full_reservation_cny'],
            'grant_available': not GRANT.exists(), 'provider_bill': 'UNKNOWN', 'policy_resolutions': policy,
            'process_check': no_writers(), 'retrieval_preflight': retrieval, 'case_qid': QID,
            'entry': 'ChatService.chat_stream; no HTTP server',
            'scope': 'Distinct question and identities; cannot repair or replace first supplement results'}


class Transport(runner.Transport):
    async def send(self, sender, client, request, *args, **kwargs):
        stage = 'HISTORY'
        try:
            validate(self.spec)
            stage = 'GRANT_OR_BUDGET'
            if GRANT.read_bytes() != self.grant_bytes or self.limit != REMAINING:
                raise ValueError('Grant or budget changed')
            stage = 'IDENTITY'
            if cont.identity(self.context) not in self.allowed:
                raise ValueError('Identity outside distinct English plan')
            stage = 'TRIAL_RECORD'
            path = self.output / 'trials.jsonl'
            if path.exists() and cont.identity(self.context) in cont.indexed_trials(path.read_bytes()):
                raise ValueError('Started identity cannot replay')
            stage = 'PROCESS'
            no_writers()
        except Exception as exc:
            self.stopped = True
            self.diagnostic('PRE_SEND_' + stage + '_' + type(exc).__name__ + ': ' + str(exc), request)
            raise base.GuardStop('English supplement pre-send ' + stage + ' validation failed') from None
        try:
            return await base.BudgetTransport.send(self, sender, client, request, *args, **kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc), request)
            raise


# Explicit hooks affect this private module instance only. Its source remains
# frozen, and `prior` above continues to verify the original first-run contract.
runner.validate, runner.preflight, runner.no_writers = validate, preflight, no_writers
runner.REMAINING, runner.GRANT, runner.Transport = REMAINING, GRANT, Transport
execute = runner.execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'execute'))
    parser.add_argument('--manifest', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
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
