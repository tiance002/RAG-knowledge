"""Real local filesystem/process tests; all monetary records are synthetic."""
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rsik.budgets import Budget
from rsik.events import EventLog
from rsik.locking import WorkspaceLock, atomic_write, process_identity
from rsik.state import StageJournal, transition


def test_atomic_exclusive_commit_never_overwrites_existing_evidence(tmp_path):
    path = tmp_path / 'artifact.json'
    guarantees = atomic_write(path, b'original', exclusive=True)
    assert guarantees['file_fsync'] and guarantees['same_volume']
    with pytest.raises(FileExistsError): atomic_write(path, b'changed', exclusive=True)
    assert path.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.tmp'))


def test_disk_failure_preserves_previous_state_and_removes_uncommitted_temp(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_bytes(b'original')
    def fail(*args): raise OSError(errno.ENOSPC, 'SYNTHETIC disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError): atomic_write(path, b'candidate')
    assert path.read_bytes() == b'original'
    assert len(list(tmp_path.iterdir())) == 1


def test_os_lock_excludes_a_real_competing_process_and_is_reusable_after_release(tmp_path):
    path = tmp_path / 'workspace.lock'
    code = "import sys;from rsik.locking import WorkspaceLock\ntry:\n with WorkspaceLock(sys.argv[1],'child'):print('ACQUIRED')\nexcept RuntimeError:print('BLOCKED')\n"
    def metadata_bytes():
        # Windows mandatory byte locks also deny reads of byte zero from a
        # second handle. Inspect only the unlocked metadata range, not the lock.
        with path.open('rb') as stream:
            stream.seek(1)
            return stream.read()
    with WorkspaceLock(path, 'parent'):
        before = metadata_bytes()
        child = subprocess.run([sys.executable, '-B', '-c', code, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert child.returncode == 0 and child.stdout.strip() == 'BLOCKED', child.stderr
        assert metadata_bytes() == before
    child = subprocess.run([sys.executable, '-B', '-c', code, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert child.returncode == 0 and child.stdout.strip() == 'ACQUIRED', child.stderr
    assert path.exists() and json.loads(path.read_bytes()[1:])['state'] == 'RELEASED'
    assert process_identity(os.getpid())


def test_unknown_or_corrupt_stale_lock_is_never_deleted_or_taken_over(tmp_path):
    path = tmp_path / 'workspace.lock'
    original = b'\0{"hostname":"UNKNOWN","pid":1,"state":"HELD"}'
    path.write_bytes(original)
    with pytest.raises(RuntimeError, match='RECOVERY_ERROR'):
        with WorkspaceLock(path, 'new'): pass
    assert path.read_bytes() == original
    path.write_bytes(b'\0invalid-json')
    with pytest.raises(RuntimeError, match='RECOVERY_ERROR'):
        with WorkspaceLock(path, 'new'): pass
    assert path.read_bytes() == b'\0invalid-json'


@pytest.mark.parametrize('crash', ['after_intent', 'after_action', 'after_receipt', 'after_checkpoint'])
def test_intent_receipt_checkpoint_recovery_does_not_repeat_side_effect(tmp_path, crash):
    target, calls = {}, []
    def action(key):
        calls.append(key)
        target[key] = {'id': 'SYNTHETIC immutable output'}
        return target[key]
    def reconcile(key):
        return {'status': 'MATCHED', 'output': target[key]} if key in target else {'status': 'ABSENT', 'safe_retry': True}
    journal = StageJournal(tmp_path)
    with pytest.raises(RuntimeError, match='INJECTED_CRASH'):
        journal.execute('derive', {'frozen_input': 1}, action, reconcile, crash_at=crash)
    output, _ = StageJournal(tmp_path).execute('derive', {'frozen_input': 1}, action, reconcile)
    assert output == {'id': 'SYNTHETIC immutable output'} and len(calls) == 1
    assert StageJournal(tmp_path).execute('derive', {'frozen_input': 1}, action, reconcile)[1] is True
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match='RECOVERY_ERROR'):
        journal.execute('derive', {'frozen_input': 2}, action, reconcile)


def test_real_process_kill_after_side_effect_reconciles_without_executing_again(tmp_path):
    target, journal_dir = tmp_path / 'target.json', tmp_path / 'journal'
    code = '''import json,sys,time
from pathlib import Path
from rsik.state import StageJournal,canonical
from rsik.locking import atomic_write
target=Path(sys.argv[1])
def action(key):
    atomic_write(target,canonical({'id':key}),exclusive=True)
    print('SIDE_EFFECT_COMMITTED',flush=True)
    time.sleep(60)
    return {'id':key}
StageJournal(sys.argv[2]).execute('derive',{'input':1},action,lambda key:{'status':'ABSENT','safe_retry':True})
'''
    proc = subprocess.Popen([sys.executable, '-B', '-c', code, str(target), str(journal_dir)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'SIDE_EFFECT_COMMITTED'
    finally:
        proc.kill()
        proc.wait(timeout=5)
    before = target.read_bytes()
    def forbidden(key): raise AssertionError('side effect was repeated')
    result, reused = StageJournal(journal_dir).execute('derive', {'input': 1}, forbidden,
        lambda key: {'status': 'MATCHED', 'output': json.loads(target.read_text())})
    assert reused and result['id'] and target.read_bytes() == before


def test_unknown_side_effect_and_tampered_receipt_stop_recovery(tmp_path):
    journal = StageJournal(tmp_path)
    with pytest.raises(RuntimeError, match='RECOVERY_ERROR'):
        journal.execute('derive', {}, lambda key: {'id': key}, lambda key: {'status': 'UNKNOWN'})
    assert not (tmp_path / 'derive/receipt.json').exists()


def test_event_log_is_immutable_idempotent_and_chain_verified(tmp_path):
    log = EventLog(tmp_path / 'events.sqlite')
    first = log.append('one', 'ENTRY_CREATED', {'value': 'SYNTHETIC'})
    assert log.append('one', 'ENTRY_CREATED', {'value': 'SYNTHETIC'}) == first
    with pytest.raises(RuntimeError, match='IDEMPOTENCY_CONFLICT'):
        log.append('one', 'ENTRY_CREATED', {'value': 'different'})
    log.append('two', 'HOLDOUT_PASSED', {'value': 'SYNTHETIC'})
    assert len(log.read()) == 2
    with log.connect() as db:
        with pytest.raises(sqlite3.IntegrityError): db.execute('DELETE FROM events')
        with pytest.raises(sqlite3.IntegrityError): db.execute("UPDATE events SET event_hash='fake'")
    log.anchor(tmp_path / 'separate-anchor.json')
    assert log.verify_anchor(tmp_path / 'separate-anchor.json')
    # Simulate privileged history truncation; a chain alone cannot detect its tail.
    with log.connect() as db:
        db.execute('DROP TRIGGER events_no_delete')
        db.execute('DELETE FROM events WHERE sequence=2')
    with pytest.raises(RuntimeError, match='EVENT_ANCHOR_MISMATCH'):
        log.verify_anchor(tmp_path / 'separate-anchor.json')


def test_no_reservation_and_final_request_may_cross_limit_then_new_calls_stop(tmp_path):
    ledger = Budget(tmp_path / 'budget.sqlite', limit_usd='0.00001')
    ledger.start('one', 'SYNTHETIC', 'gpt-6-luna', 'a' * 64)
    assert ledger.snapshot()['recorded_cost_nano_upper'] == 0
    assert ledger.snapshot()['reservation_usd'] == '0'
    ledger.complete('one', {'input_tokens': 100, 'output_tokens': 50}, status='completed')
    assert ledger.snapshot()['recorded_cost_nano_upper'] > ledger.limit_nano
    with pytest.raises(RuntimeError, match='BUDGET_EXHAUSTED'):
        ledger.start('two', 'SYNTHETIC', 'gpt-6-luna', 'b' * 64)
    assert ledger.snapshot()['attempts'] == 1


def test_request_count_is_unbounded_and_astra_is_never_allowed(tmp_path):
    ledger = Budget(tmp_path / 'budget.sqlite')
    for index in range(12):
        key = str(index)
        ledger.start(key, 'SYNTHETIC', 'gpt-6-luna', 'a' * 64)
        ledger.complete(key, {'input_tokens': 0, 'output_tokens': 0}, status='completed')
    assert ledger.snapshot()['attempts'] == 12 and ledger.snapshot()['count_limit'] is None
    with pytest.raises(ValueError, match='MODEL_NOT_ALLOWED'):
        ledger.start('astra', 'SYNTHETIC', 'gpt-6-astra', 'a' * 64)


def test_unknown_usage_is_not_free_and_unknown_dispatch_is_not_retried(tmp_path):
    ledger = Budget(tmp_path / 'budget.sqlite')
    ledger.start('one', 'SYNTHETIC', 'gpt-6-luna', 'a' * 64)
    fresh = Budget(tmp_path / 'budget.sqlite')
    with pytest.raises(RuntimeError, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        fresh.start('two', 'SYNTHETIC', 'gpt-6-luna', 'b' * 64)
    ledger.complete('one', None, status='UNKNOWN')
    assert ledger.snapshot()['unknown_usage_requests'] == ['one']
    assert ledger.snapshot()['total_cost_status'] == 'UNKNOWN'
    with pytest.raises(RuntimeError, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        ledger.start('two', 'SYNTHETIC', 'gpt-6-luna', 'b' * 64)


def test_three_status_dimensions_do_not_imply_deployment():
    assert transition('quality', 'READY', 'VALIDATED', evidence=['SYNTHETIC']) == 'VALIDATED'
    with pytest.raises(ValueError):
        transition('deployment', 'NOT_PROMOTED', 'PROMOTION_PENDING', evidence=['SYNTHETIC'], quality='VALIDATED', approval_valid=True)
    with pytest.raises(ValueError):
        transition('deployment', 'NOT_PROMOTED', 'PROMOTION_PENDING', evidence=['SYNTHETIC'], quality='VERIFIED', approval_valid=False)
    assert transition('execution', 'RUNNING', 'FAILED', evidence=['SYNTHETIC']) == 'FAILED'


def test_post_failure_unknown_usage_is_bounded_and_charged_without_reservation(tmp_path):
    from rsik.state import digest
    ledger=Budget(tmp_path/'budget.sqlite')
    request={'model':'gpt-6-luna','max_output_tokens':1024,'input':'SYNTHETIC bounded request'}
    ledger.start('one','diagnose','gpt-6-luna',digest(request))
    ledger.complete('one',None,status='UNKNOWN')
    state=ledger.bound_completed_unknown('one',request)
    assert state['unknown_usage_requests']==['one'] and state['unbounded_usage_requests']==[]
    assert state['total_cost_status']=='BOUNDED_USAGE_ESTIMATE' and state['reservation_usd']=='0'
    assert state['recorded_cost_nano_upper']==16384*125+1024*500
    assert ledger.bound_completed_unknown('one',request)['recorded_cost_nano_upper']==state['recorded_cost_nano_upper']
    ledger.start('two','diagnose','gpt-6-luna','a'*64)
    assert ledger.snapshot()['attempts']==2


def test_unbound_or_changed_unknown_request_cannot_receive_a_free_billing_override(tmp_path):
    from rsik.state import digest
    ledger=Budget(tmp_path/'budget.sqlite');request={'model':'gpt-6-luna','max_output_tokens':1024}
    ledger.start('one','diagnose','gpt-6-luna',digest(request));ledger.complete('one',None,status='UNKNOWN')
    with pytest.raises(ValueError):ledger.bound_completed_unknown('one',{**request,'max_output_tokens':1})
    assert ledger.snapshot()['total_cost_status']=='UNKNOWN'
    with pytest.raises(RuntimeError,match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        ledger.start('two','diagnose','gpt-6-luna','b'*64)
