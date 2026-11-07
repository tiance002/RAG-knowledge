"""Continue only never-started frozen M2 identities after immutable prior runs.

Primary and D-supplement costs share the original one-yuan allowance. This
runner imports no app module before owned DB/Redis environment configuration.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, redirect_stderr, redirect_stdout
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys

SUPPLEMENT_PATH = Path(__file__).with_name('m2_preference_supplement.py')
MODULE = importlib.util.spec_from_file_location('m2_continue_safety', SUPPLEMENT_PATH)
safety = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(safety)
base = safety.base
ORIGINAL_PLAN = base.trial_plan
SCHEMA = 'm2-preference-continuation/v1'


def identity(row):
    return row['qid'], row['mode'], row['arm'], row['repetition']


def read_hashed(path, expected):
    path = base.private_path(path)
    raw = path.read_bytes()
    if base.digest(raw) != expected:
        raise ValueError('Immutable prior run artifact changed')
    return raw


def rows(raw):
    return [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]


def indexed_trials(raw):
    output = {}
    for row in rows(raw):
        key = identity(row)
        if key in output:
            raise ValueError('Duplicate prior trial identity')
        output[key] = row
    return output


def account_completed_prior(entry, manifest, expected_limit, primary_committed, combined_before=None):
    """Final process evidence required; attributed UNKNOWN retains its reserve."""
    output = base.private_path(entry['output'])
    summary = json.loads(read_hashed(output/'summary.json',entry['summary_sha256']))
    records = rows(read_hashed(output/'provider-ledger.jsonl',entry['ledger_sha256']))
    trials = indexed_trials(read_hashed(output/'trials.jsonl',entry['trials_sha256']))
    kind=entry.get('kind')
    combined_before=primary_committed if combined_before is None else combined_before
    if (kind not in ('D_ELIGIBILITY_ONLY','ONLY_NEVER_STARTED','NEW_HTTP_SSE_ONLY') or summary.get('purpose') != kind
            or summary.get('status') not in ('RECORDED_REQUIRES_EVIDENCE_REVIEW','STOPPED','FAILED')):
        raise ValueError('A final D supplement/continuation summary is required before continuation')
    latest = {}
    for record in records:
        call_id = record['call_id']
        old = latest.get(call_id)
        if old is not None and old['state'] != 'UNKNOWN_PENDING' and old != record:
            raise ValueError('Prior terminal attempt changed')
        latest[call_id] = record
    if set(latest) != set(range(1,len(latest)+1)):
        raise ValueError('Prior provider attempt identities are not contiguous')
    total = known = Decimal(0)
    provider_ids = set()
    for record in latest.values():
        if (record.get('state') not in ('COMPLETED','UNKNOWN','UNKNOWN_PENDING')
                or not isinstance(record.get('qid'),str) or record.get('mode') not in base.MODES
                or record.get('arm') not in (('A','D') if kind=='D_ELIGIBILITY_ONLY' else base.ARMS)
                or record.get('repetition') not in ((3,) if kind=='NEW_HTTP_SSE_ONLY' else (1,2))
                or record.get('requested_model') != manifest['model']
                or type(record.get('input_token_bound')) is not int
                or record['input_token_bound']<0 or record.get('output_token_bound')!=manifest['max_output_tokens']):
            raise ValueError('Prior attempt cannot be attributed to an exact authorized identity')
        if identity(record) in trials and record['call_id'] not in trials[identity(record)].get('provider_call_ids',[]):
            raise ValueError('Prior attempt lacks its exact recorded trial attribution')
        if kind=='D_ELIGIBILITY_ONLY' and identity(record) not in trials:
            raise ValueError('Prior supplement attempt lacks its exact recorded trial attribution')
        reservation=(Decimal(record['input_token_bound'])*safety.decimal(manifest['pricing']['input_cny_per_million'])+
                     Decimal(record['output_token_bound'])*safety.decimal(manifest['pricing']['output_cny_per_million']))/1000000
        if safety.decimal(record['reservation_cny'])!=reservation:
            raise ValueError('Prior reservation and frozen price disagree')
        if record['state']!='COMPLETED':
            total+=reservation
            continue
        usage = record.get('usage') or {}
        prompt,completion = usage.get('prompt_tokens'),usage.get('completion_tokens')
        if (type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0
                or type(record.get('input_token_bound')) is not int or prompt > record['input_token_bound']
                or record.get('output_token_bound') != manifest['max_output_tokens'] or completion > manifest['max_output_tokens']
                or record.get('requested_model') != manifest['model'] or record.get('actual_model') != manifest['model']):
            raise ValueError('Prior completed model usage/identity invalid')
        cost = (Decimal(prompt)*safety.decimal(manifest['pricing']['input_cny_per_million']) +
                Decimal(completion)*safety.decimal(manifest['pricing']['output_cny_per_million']))/1000000
        if safety.decimal(record['price_based_cost_cny']) != cost:
            raise ValueError('Prior usage and cost disagree')
        total += cost
        known += cost
        if record.get('provider_request_id'):
            if record['provider_request_id'] in provider_ids:
                raise ValueError('Duplicate actual provider request identity in prior run')
            provider_ids.add(record['provider_request_id'])
    parent_commitment=(summary.get('parent_budget',{}).get('parent_committed_cny') if kind=='D_ELIGIBILITY_ONLY'
                       else summary.get('prior_budget',{}).get('prior_combined_committed_cny'))
    expected_parent=primary_committed if kind=='D_ELIGIBILITY_ONLY' else combined_before
    if (summary.get('actual_http_calls') != len(latest) or (kind!='NEW_HTTP_SSE_ONLY' and summary.get('recorded_trials') != len(trials))
            or safety.decimal(summary['budget_cny']) != expected_limit
            or safety.decimal(summary['committed_including_unknown_cny']) != total
            or safety.decimal(summary['price_based_known_cost_cny']) != known
            or parent_commitment is None or safety.decimal(parent_commitment) != expected_parent
            or safety.decimal(summary['global_committed_including_unknown_cny']) != combined_before+total):
        raise ValueError('Prior summary and independently computed shared budget disagree')
    return trials,latest,total,provider_ids


def normalized_prior(entry):
    return {**entry,'output':str(base.private_path(entry['output']))}


def verify_prior_continuation(entry,spec,prefix):
    contract=json.loads(read_hashed(base.private_path(entry['output'])/'continuation-manifest.json',
                                    entry['continuation_manifest_sha256']))
    if (base.private_path(contract['parent_output'])!=base.private_path(spec['parent_output'])
            or base.private_path(contract['parent_manifest_path'])!=base.private_path(spec['parent_manifest_path'])
            or any(contract.get(field)!=spec.get(field) for field in (
                'parent_manifest_sha256','parent_summary_sha256','parent_ledger_sha256','parent_trials_sha256','base_runner_sha256'))
            or [normalized_prior(prior) for prior in contract.get('prior_runs',[])]!=[normalized_prior(prior) for prior in prefix]):
        raise ValueError('Prior continuation ancestry differs from immutable ordered history')


def verify_http_run(entry, spec, prefix, manifest, started):
    output = base.private_path(entry['output'])
    contract = json.loads(read_hashed(output/'manifest.json', entry['http_manifest_sha256']))
    history = contract.get('history', {})
    expected_history = {**spec, 'prior_runs': prefix}
    # Reuse metadata belongs to this continuation, not the earlier HTTP grant.
    expected_history.pop('source_db', None)
    expected_history.pop('source_db_sha256', None)
    # Authorization metadata belongs to the original continuation contract.
    for key in ('authorization_id', 'expected_remaining_trials'):
        expected_history[key] = history.get(key)
    if (history != expected_history or history.get('expected_remaining_trials') != len(set(
            (case['qid'], mode, arm, rep) for case, mode, rep, arm, _ in ORIGINAL_PLAN(manifest)) - started)):
        raise ValueError('HTTP run ancestry or remaining identities differ from prior history')
    ids = contract.get('identities', [])
    runner = Path(__file__).with_name('m2_http_real_smoke.py')
    if (contract.get('schema') != 'm2-http-real/v1' or contract.get('purpose') != 'NEW_HTTP_SSE_ONLY'
            or contract.get('authorized') is not True or not contract.get('authorization_id')
            or contract.get('max_new_requests') != 2 or contract.get('qid') != 'office50:031'
            or contract.get('model') != manifest['model'] or contract.get('max_output_tokens') != manifest['max_output_tokens']
            or safety.decimal(contract.get('total_budget_cny')) != safety.decimal(manifest['budget_cny'])
            or contract.get('fixed_search') != base.FIXED_SEARCH or contract.get('synthetic_namespace') is not None
            or contract.get('synthetic_provider_outcome') is not None
            or contract.get('http_runner_sha256') != base.digest(runner.read_bytes())
            or len(ids) != 2 or [item.get('mode') for item in ids] != ['chat', 'agent']
            or any(item.get('qid') != contract['qid'] or item.get('arm') != 'A' or item.get('repetition') != 3
                   or not isinstance(item.get('run_identity'), str) or not item['run_identity'].startswith('m2-http-real:') for item in ids)
            or len({item['run_identity'] for item in ids}) != 2):
        raise ValueError('HTTP authorization, runner, model, configuration or identity mismatch')
    actual_started = rows(read_hashed(output/'started.jsonl', entry['started_sha256']))
    if actual_started != ids:
        raise ValueError('HTTP started identities differ from authorization')
    trials = indexed_trials(read_hashed(output/'trials.jsonl', entry['trials_sha256']))
    if set(trials) != {identity(item) for item in ids}:
        raise ValueError('HTTP trials differ from authorized identities')
    allowed = {identity(item): item['run_identity'] for item in ids}
    records = rows(read_hashed(output/'provider-ledger.jsonl', entry['ledger_sha256']))
    pending = {}
    terminal = set()
    for record in records:
        key = identity(record)
        if (key not in allowed or record.get('run_identity') != allowed[key]
                or record.get('dataset_version') != manifest['dataset_version']
                or trials[key].get('run_identity') != allowed[key]):
            raise ValueError('HTTP provider attempt misattributed')
        call_id = record['call_id']
        if record.get('state') == 'UNKNOWN_PENDING':
            if call_id in pending:
                raise ValueError('HTTP provider attempt repeated')
            pending[call_id] = record
            read_hashed(output/f'request-{call_id:04d}.json', record['request_sha256'])
        else:
            old = pending.get(call_id)
            if call_id in terminal:
                raise ValueError('HTTP provider terminal attempt repeated')
            terminal.add(call_id)
            if old is None or any(record.get(k) != v for k, v in old.items() if k != 'state'):
                raise ValueError('HTTP provider terminal attempt changed its reservation or identity')
    for key, trial in trials.items():
        if sorted(trial.get('provider_call_ids', [])) != sorted(r['call_id'] for r in pending.values() if identity(r) == key):
            raise ValueError('HTTP trial provider attribution mismatch')
    if set(allowed) & started:
        raise ValueError('HTTP identity overlaps original history')
    return set(allowed), set(allowed.values())


def verify_history(spec):
    manifest,budget = safety.verify_parent(spec)
    primary_output = base.private_path(spec['parent_output'])
    primary_trials = indexed_trials(read_hashed(primary_output/'trials.jsonl',spec['parent_trials_sha256']))
    primary_records = rows(read_hashed(primary_output/'provider-ledger.jsonl',spec['parent_ledger_sha256']))
    primary_latest = {row['call_id']:row for row in primary_records}
    planned = {(case['qid'],mode,arm,rep):(case,mode,rep,arm,prefs)
               for case,mode,rep,arm,prefs in ORIGINAL_PLAN(manifest)}
    started = set(primary_trials) | {identity(row) for row in primary_records}
    primary_started_count=len(started)
    if not started <= set(planned):
        raise ValueError('Primary started identities are outside frozen plan')
    primary_summary = json.loads((primary_output/'summary.json').read_bytes())
    if primary_summary.get('recorded_trials') != len(primary_trials):
        raise ValueError('Primary trial count and final summary disagree')
    prior_runs = spec.get('prior_runs')
    if (not isinstance(prior_runs,list) or not prior_runs or prior_runs[0].get('kind')!='D_ELIGIBILITY_ONLY'
            or any(entry.get('kind') not in ('ONLY_NEVER_STARTED','NEW_HTTP_SSE_ONLY') for entry in prior_runs[1:])):
        raise ValueError('One D supplement followed by ordered prior continuations is required')
    # A persistent real-run grant makes omission of its paid ledger invalid.
    grant = base.private_path(base.ROOT/'var/preferences-acceptance/m2-http-smoke-real-locks'/
                              ('chain-'+spec['parent_manifest_sha256']+'.started'))
    if grant.exists():
        recorded = json.loads(grant.read_bytes())
        matches = [entry for entry in prior_runs if entry.get('kind') == 'NEW_HTTP_SSE_ONLY'
                   and base.private_path(entry['output']) == base.private_path(recorded['output'])]
        if len(matches) != 1:
            raise ValueError('Persistent HTTP grant requires its exact ledger in history')
        contract = json.loads(read_hashed(base.private_path(matches[0]['output'])/'manifest.json',
                                          matches[0]['http_manifest_sha256']))
        if (recorded.get('authorization_id') != contract.get('authorization_id')
                or recorded.get('spec_sha256') != base.digest(base.canonical(contract))):
            raise ValueError('HTTP history does not match persistent real-run grant')
    primary_cost = safety.decimal(budget['parent_committed_cny'])
    committed = primary_cost
    request_ids = {row['provider_request_id'] for row in primary_latest.values() if row.get('provider_request_id')}
    outputs = {primary_output}
    continuation_trial_records=0
    http_identities=set()
    http_run_ids=set()
    for index,entry in enumerate(prior_runs):
        prior_output=base.private_path(entry['output'])
        if prior_output in outputs:
            raise ValueError('Duplicate prior output would double count shared budget')
        outputs.add(prior_output)
        if entry['kind']=='NEW_HTTP_SSE_ONLY':
            keys, run_ids=verify_http_run(entry,spec,prior_runs[:index],manifest,started)
            if keys & http_identities or run_ids & http_run_ids:
                raise ValueError('HTTP identity was reused across prior runs')
            http_identities |= keys
            http_run_ids |= run_ids
        if entry['kind']=='ONLY_NEVER_STARTED':
            verify_prior_continuation(entry,spec,prior_runs[:index])
        trials,attempts,cost,prior_request_ids=account_completed_prior(entry,manifest,safety.decimal(manifest['budget_cny'])-committed,
                                                                     primary_cost,committed)
        prior_started=set(trials)|{identity(row) for row in attempts.values()}
        if entry['kind']=='D_ELIGIBILITY_ONLY':
            if prior_started & set(planned):
                raise ValueError('Supplement overlaps original frozen identities')
        elif entry['kind']=='ONLY_NEVER_STARTED':
            if not prior_started<=set(planned) or prior_started&started:
                raise ValueError('Prior continuation repeats a started identity or lies outside frozen plan')
            started|=prior_started
            continuation_trial_records+=len(trials)
        if prior_request_ids & request_ids:
            raise ValueError('Provider request identity was reused across runs')
        request_ids |= prior_request_ids
        committed += cost
    limit=safety.decimal(manifest['budget_cny'])
    if committed>limit:
        raise ValueError('Previous commitments already exceed global limit')
    remaining=[value for key,value in planned.items() if key not in started]
    started_qids={key[0] for key in started}
    # Give a wholly unattempted question coverage before a partially attempted
    # question; stable sort preserves original mode/repetition/A-B-C-D pairs.
    remaining.sort(key=lambda value:value[0]['qid'] in started_qids)
    if len(remaining)!=spec.get('expected_remaining_trials'):
        raise ValueError('Never-started count differs from authorized frozen continuation')
    return manifest,remaining,{'global_limit_cny':str(limit),'primary_committed_cny':str(primary_cost),
                               'prior_combined_committed_cny':str(committed),'remaining_cny':str(limit-committed),
                               'primary_trial_records':len(primary_trials),'primary_started_identities':primary_started_count,
                               'prior_continuation_trial_records':continuation_trial_records,
                               'all_prior_started_original_identities':len(started),'prior_run_count':len(prior_runs),
                               'never_started_trials':len(remaining),'provider_bill':'UNKNOWN'}


def validate_reuse(spec, manifest):
    source = base.private_path(spec['source_db'])
    expected_hash = spec['source_db_sha256']
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError('Source database SHA256 required')
    if any((sidecar := Path(str(source) + suffix)).exists() and sidecar.stat().st_size for suffix in ('-wal', '-journal')):
        raise ValueError('Source database has an unsealed journal')
    if base.digest(source.read_bytes()) != expected_hash:
        raise ValueError('Source database hash mismatch')
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Source database integrity check failed')
        chunks = db.execute('SELECT id,file_id,content FROM document_chunks').fetchall()
        expected = {c['id']: (c['source_id'], c['content']) for c in manifest['chunks']}
        if len(chunks) != len(expected) or {r[0]: (r[1], r[2]) for r in chunks} != expected:
            raise ValueError('Source database differs from frozen chunks')
        kb = db.execute('SELECT id,chunk_count FROM knowledge_bases').fetchall()
        files = db.execute('SELECT id,kb_id FROM file_documents').fetchall()
        entries = db.execute('SELECT chunk_id,kb_id,terms,doc_length FROM chunk_index').fetchall()
        stats = db.execute('SELECT kb_id,total_docs FROM index_stats').fetchall()
        if (kb != [(base.KB_ID, len(expected))] or
                {x[0] for x in files} != {x['id'] for x in manifest['sources']} or
                any(x[1] != base.KB_ID for x in files) or
                len(entries) != len(expected) or {x[0] for x in entries} != set(expected) or
                any(x[1] != base.KB_ID or not x[2] or x[3] < 0 for x in entries) or
                stats != [(base.KB_ID, len(expected))]):
            raise ValueError('Source database KB or BM25 index mismatch')
    return source


def backup_reuse(spec, manifest, destination):
    source = validate_reuse(spec, manifest)
    with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro&immutable=1', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        # Historical retrieval logs are not needed for the new isolated run.
        dst.execute('DELETE FROM retrieval_logs')
        dst.commit()
        # A WAL-mode source retains that mode in the backup. Seal the copied
        # database before hashing and validating its main file.
        if dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
            raise ValueError('Destination database checkpoint failed')
    validate_reuse({**spec, 'source_db': str(destination),
                    'source_db_sha256': base.digest(destination.read_bytes())}, manifest)
    validate_reuse(spec, manifest)


def validate_spec(spec):
    if (spec.get('schema_version')!=SCHEMA or spec.get('authorized') is not True
            or not spec.get('authorization_id') or spec.get('purpose')!='ONLY_NEVER_STARTED'
            or base.digest(Path(base.__file__).read_bytes())!=spec.get('base_runner_sha256')):
        raise ValueError('Explicit authorization and exact frozen base runner required')
    manifest,plan,budget=verify_history(spec)
    if 'source_db' in spec or 'source_db_sha256' in spec:
        validate_reuse(spec, manifest)
    return {'status':'PASS' if safety.decimal(budget['remaining_cny'])>0 else 'BUDGET_EXHAUSTED',
            'purpose':'ONLY_NEVER_STARTED','planned_trials':len(plan),'model_calls':0,'budget':budget,
            'fixed_search':base.FIXED_SEARCH,'max_output_tokens':manifest['max_output_tokens'],
            'ordering':'Wholly unattempted qids first; original mode/repetition/A-B-C-D order within each question',
            'remaining_identities':[{'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep} for case,mode,rep,arm,prefs in plan],
            'policy':'Failed/answered/started primary identities are preserved and never repeated',
            'runner_sha256':base.digest(Path(__file__).read_bytes())}


class HistoryTransport(base.BudgetTransport):
    def __init__(self,manifest,output,spec,allowed,*,transport=None):
        super().__init__(manifest,output,transport=transport)
        self.spec=spec
        self.allowed=set(allowed)
        self.budget_blocked=False
    async def send(self,*args,**kwargs):
        try:
            _,plan,budget=verify_history(self.spec)
            if (safety.decimal(budget['remaining_cny'])!=self.limit or identity(self.context) not in self.allowed
                    or self.allowed!={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan}):
                raise ValueError('Changed history, budget or unstarted identity')
        except Exception:
            self.stopped=True
            raise base.GuardStop('Immutable prior history/budget/identity check failed before send') from None
        try:
            return await super().send(*args,**kwargs)
        except base.GuardStop as exc:
            if str(exc).startswith('BUDGET_BLOCKED:'):
                self.budget_blocked=True
            raise


async def run_plan(manifest,output,guard,plan):
    """Skip a recorded application failure; never retry it or bypass guard stop."""
    skipped=[]
    original_plan=base.trial_plan
    try:
        for value in plan:
            case,mode,rep,arm,prefs=value
            target=(case['qid'],mode,arm,rep)
            before=len(guard.calls)
            base.trial_plan=lambda _manifest,value=value:iter([value])
            try:
                await base.run_trials(manifest,output,guard)
                if guard.stopped:
                    raise base.GuardStop('Transport stopped during trial')
            except base.GuardStop:
                path=Path(output)/'trials.jsonl'
                recorded=[row for row in rows(path.read_bytes()) if identity(row)==target] if path.exists() else []
                budget_blocked = guard.budget_blocked and all(call.get('state') == 'COMPLETED' for call in guard.calls)
                if ((guard.stopped and not budget_blocked) or len(recorded)!=1 or recorded[0].get('state') not in ('GUARD_STOP','FAILED','NO_DONE')
                        or any(call.get('state')!='COMPLETED' for call in guard.calls[before:])):
                    raise
                if budget_blocked:
                    guard.stopped=False
                    guard.budget_blocked=False
                event={'qid':target[0],'mode':mode,'arm':arm,'repetition':rep,
                       'state':'SKIPPED_UNTESTABLE_AFTER_RECORDED_FAILURE',
                       'provider_call_ids':[call['call_id'] for call in guard.calls[before:]],
                       'reason':('BUDGET_BLOCKED before provider send; prior outcomes completed; identity never retried' if budget_blocked else 'Application failed after known provider outcomes; proceed to a different never-started identity without retry')}
                skipped.append(event)
                with (Path(output)/'skipped-untestable.jsonl').open('ab') as stream:
                    stream.write(base.canonical(event)+b'\n');stream.flush();os.fsync(stream.fileno())
    finally:
        base.trial_plan=original_plan
    return skipped


def execute(spec,output):
    preflight=validate_spec(spec)
    if preflight['status']!='PASS':return preflight
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('M2_AB_API_KEY missing; zero continuation sends')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Fresh process required before owned runtime configuration')
    parent,plan,budget=verify_history(spec)
    lock_dir=base.private_path(base.ROOT/'var/preferences-acceptance/m2-continuation-locks')
    lock_dir.mkdir(parents=True,exist_ok=True)
    chain=base.digest(base.canonical({'primary':spec['parent_ledger_sha256'],'trials':spec['parent_trials_sha256'],
                                    'priors':spec['prior_runs']}))
    # Acquire the shared lease before creating any persistent grant or output.
    active=lock_dir/(spec['parent_manifest_sha256']+'.active')
    lease=secrets.token_hex(24).encode()
    with active.open('xb') as stream:stream.write(lease);stream.flush();os.fsync(stream.fileno())
    try:
        if (lock_dir/(chain+'.started')).exists():
            raise FileExistsError('Continuation chain already started')
        validate_reuse(spec, parent)
        output.mkdir(parents=True,exist_ok=False)
        (output/'continuation-manifest.json').write_bytes(base.canonical(spec))
        (output/'manifest.json').write_bytes(base.canonical(parent))
        (output/'preflight.json').write_bytes(base.canonical(preflight))
        backup_reuse(spec,parent,output/'isolated.db')
    except BaseException:
        if active.exists() and active.read_bytes()==lease:active.unlink()
        raise
    runtime_manifest={**parent,'budget_cny':budget['remaining_cny']}
    allowed={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan}
    guard=HistoryTransport(runtime_manifest,output,spec,allowed)
    summary={'status':'NOT_RUN','purpose':'ONLY_NEVER_STARTED','planned_trials':len(plan),
             'prior_budget':budget,'entry':'ChatService.chat_stream; no HTTP server','fixed_search':base.FIXED_SEARCH}
    original_cwd,original_plan=Path.cwd(),base.trial_plan
    try:
        with base.isolated_redis(output) as redis_url:
            base.configure_runtime(runtime_manifest,output,redis_url)
            with (output/'runtime.log').open('w',encoding='utf-8') as log:
                with redirect_stdout(log),redirect_stderr(log),guard.installed():
                    safety.assert_runtime_isolation(output,redis_url)
                    # Persist the one-shot grant only after all startup checks succeed.
                    with (lock_dir/(chain+'.started')).open('xb') as stream:
                        stream.write(base.canonical({'authorization_id':spec['authorization_id'],'output':str(output),
                                                     'spec_sha256':base.digest(base.canonical(spec))}))
                        stream.flush();os.fsync(stream.fileno())
                    asyncio.run(run_plan(runtime_manifest,output,guard,plan))
        summary['status']='RECORDED_REQUIRES_EVIDENCE_REVIEW'
    except base.GuardStop as exc:summary.update(status='STOPPED',stop_reason=str(exc))
    except Exception as exc:summary.update(status='FAILED',error_type=type(exc).__name__)
    finally:
        base.trial_plan=original_plan;os.chdir(original_cwd)
        summary.update(guard.summary())
        trial_path=output/'trials.jsonl'
        recorded=rows(trial_path.read_bytes()) if trial_path.exists() else []
        skipped_path=output/'skipped-untestable.jsonl'
        summary.update(recorded_trials=len(recorded),global_committed_including_unknown_cny=str(safety.decimal(budget['prior_combined_committed_cny'])+guard.committed),
                       skipped_untestable_trials=len(rows(skipped_path.read_bytes())) if skipped_path.exists() else 0,
                       correctness='REQUIRES_INDEPENDENT_REVIEW',prior_evidence='READ_ONLY_UNCHANGED',
                       not_run_identities=[value for value in preflight['remaining_identities'] if identity(value) not in {identity(row) for row in recorded}])
        try:
            (output/'summary.json').write_bytes(base.canonical(summary))
        finally:
            if active.exists() and active.read_bytes()==lease:active.unlink()
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('preflight','execute'));parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        spec=json.loads(base.private_path(args.manifest).read_text(encoding='utf-8-sig'));output=base.private_path(args.output)
        result=validate_spec(spec) if args.mode=='preflight' else execute(spec,output)
        if args.mode=='preflight':
            output.mkdir(parents=True,exist_ok=True);(output/'preflight.json').write_bytes(base.canonical(result))
        print(json.dumps({key:result[key] for key in ('status','purpose','planned_trials','model_calls','budget','recorded_trials','global_committed_including_unknown_cny') if key in result}))
        return 0 if result['status'] in ('PASS','RECORDED_REQUIRES_EVIDENCE_REVIEW') else 2
    except Exception as exc:
        print(json.dumps({'status':'FAIL','error_type':type(exc).__name__}));return 1


if __name__=='__main__':raise SystemExit(main())
