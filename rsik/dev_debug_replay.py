"""Bounded DEV-only replay of an entire validated query-aware selector.

No archived Worker module is imported or executed. Only its guarded six-argument
function executes with fixed trusted helpers in an isolated, disposable child.
Retention remains UNKNOWN unless an explicit audited member predicate is supplied.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

from .query_aware import apply_patch, validate_function
from run_rsi_loop import selector

SCHEMA = 'rsi-dev-debug-replay/v1'
METRIC = 'sum_selected_member_content_chars'

# This code has no application imports, filesystem access or network operations.
# The candidate namespace has no import/open/eval/exec capabilities.
_CHILD = r'''
import json, sys
# Trusted runtime resource setup, never exposed to selector globals.
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    class BASIC(ctypes.Structure):
        _fields_ = [('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('LimitFlags',wintypes.DWORD),
                    ('MinWorkingSet',ctypes.c_size_t),('MaxWorkingSet',ctypes.c_size_t),('ActiveProcesses',wintypes.DWORD),
                    ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
    class EXTENDED(ctypes.Structure):
        _fields_ = [('Basic',BASIC),('IO',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                    ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None,None)
    limits = EXTENDED()
    limits.Basic.LimitFlags = 0x100 | 0x2000
    limits.ProcessMemoryLimit = 256 * 1024 * 1024
    if not job or not kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):
        raise RuntimeError('DEV_MEMORY_ISOLATION_UNAVAILABLE')
else:
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(256 * 1024 * 1024,256 * 1024 * 1024))
request = json.loads(sys.stdin.read())
def require(value, reason):
    if not value: raise ValueError(reason)
def duplicate_key(member, chunk, file):
    loc = chunk.get('source_locator')
    if not isinstance(loc, dict) or not file['name'].lower().endswith('.pdf'):
        return None
    page, bbox = loc.get('page'), loc.get('bbox')
    if not (type(page) is int and page > 0 and isinstance(bbox, list) and len(bbox) == 4
            and all(type(x) in (int, float) for x in bbox) and bbox[0] < bbox[2] and bbox[1] < bbox[3]
            and isinstance(chunk.get('parent_id'), str) and chunk['parent_id']
            and isinstance(chunk.get('heading_text'), str) and chunk['heading_text']):
        return None
    return (member['file_id'], member['kb_id'], member['content'], page, tuple(bbox),
            chunk['parent_id'], chunk['heading_text'], chunk.get('heading_level'))
def bounded_range(*args):
    result = range(*args)
    if len(result) > 20000: raise ValueError('DEV_RANGE_BUDGET')
    return result
safe = {k: getattr(__builtins__, k) for k in ('any','all','len','enumerate','list','tuple','set','sorted','min','max','sum','abs')}
safe.update(range=bounded_range, require=require, duplicate_key=duplicate_key, __builtins__={})
# The dense archived member pool exceeds 200k opcodes on the unchanged baseline.
remaining = 2000000
def budget(frame, event, arg):
    global remaining
    if frame.f_code.co_filename == '<validated-dev-selector>':
        frame.f_trace_opcodes = True
        remaining -= 1
        if remaining <= 0: raise ValueError('DEV_INSTRUCTION_BUDGET')
    return budget
try:
    exec(compile(request['source'], '<validated-dev-selector>', 'exec'), safe)
    args = request['args']
    before = json.dumps(args, sort_keys=True)
    sys.settrace(budget)
    result = safe['select_member_indices'](*args)
    sys.settrace(None)
    size = len(args[0])
    require(isinstance(result, list) and all(type(i) is int and 0 <= i < size for i in result), 'DEV_INVALID_INDICES')
    require(result == sorted(set(result)), 'DEV_ORDER_OR_DUPLICATE_INDICES')
    ids = [args[0][i]['id'] for i in result]
    require(all(s in ids for s in args[3]), 'DEV_SEED_LOSS')
    require(before == json.dumps(args, sort_keys=True), 'DEV_INPUT_MUTATION')
    print(json.dumps({'status':'DEV_EXECUTED','indices':result}))
except Exception as error:
    sys.settrace(None)
    print(json.dumps({'status':'DEV_EXECUTION_FAILED','reason':type(error).__name__ + ':' + str(error)}))
'''


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _config(config):
    if isinstance(config, bytes):
        config = json.loads(config)
    if not isinstance(config, dict) or config.get('selection') not in ('all', 'exact_duplicate'):
        raise ValueError('DEV_INVALID_SELECTION_CONFIG')
    return config


def _execute(source, args, timeout_seconds):
    validate_function(source)
    if len(source.encode('utf-8')) > 65536:
        raise ValueError('DEV_SOURCE_SIZE_BUDGET')
    payload = json.dumps({'source': source, 'args': args}, ensure_ascii=True)
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError('DEV_INPUT_SIZE_BUDGET')
    try:
        proc = subprocess.run([sys.executable, '-I', '-S', '-c', _CHILD], input=payload,
                              text=True, capture_output=True, timeout=timeout_seconds,
                              encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return {'status': 'DEV_EXECUTION_FAILED', 'reason': 'DEV_TIMEOUT', 'exit_code': None}
    evidence = {'exit_code': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
    if proc.returncode != 0:
        return {'status': 'DEV_EXECUTION_FAILED', 'reason': 'DEV_CHILD_EXIT', **evidence}
    try:
        result = json.loads(proc.stdout)
        if result.get('status') not in ('DEV_EXECUTED', 'DEV_EXECUTION_FAILED'):
            raise ValueError('unexpected child result')
    except (ValueError, TypeError, AttributeError):
        return {'status': 'DEV_EXECUTION_FAILED', 'reason': 'DEV_INVALID_CHILD_OUTPUT', **evidence}
    return {**result, **evidence}


def _retention(case, selected_ids):
    atoms = case.get('atoms')
    if not isinstance(atoms, list) or not atoms:
        return {'status': 'UNKNOWN', 'atoms': [], 'reason': 'NO_AUDITED_MEMBER_PREDICATE'}
    rows = []
    for atom in atoms:
        ids = atom.get('member_ids') if isinstance(atom, dict) else None
        audited = isinstance(atom, dict) and atom.get('audit_status') == 'AUDITED'
        if not audited or not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            rows.append({'atom_id': atom.get('atom_id') if isinstance(atom, dict) else None, 'status': 'UNKNOWN'})
            continue
        mode = atom.get('predicate')
        if mode not in ('all_members', 'any_member'):
            rows.append({'atom_id': atom.get('atom_id'), 'status': 'UNKNOWN'})
            continue
        present = [i in selected_ids for i in ids]
        kept = all(present) if mode == 'all_members' else any(present)
        rows.append({'atom_id': atom.get('atom_id'), 'status': 'RETAINED' if kept else 'MISSING'})
    status = 'UNKNOWN' if any(r['status'] == 'UNKNOWN' for r in rows) else ('MISSING' if any(r['status'] == 'MISSING' for r in rows) else 'RETAINED')
    return {'status': status, 'atoms': rows, 'scope': 'audited member predicate only; not answer correctness'}


def replay_candidate(worker, config, candidate_worker, cases, *, candidate_config=None, timeout_seconds=5):
    """Pair baseline/candidate on identical actual frozen member inputs."""
    if not 0 < timeout_seconds <= 30:
        raise ValueError('DEV_TIMEOUT_BUDGET')
    baseline_config = _config(config)
    candidate_config = _config(config if candidate_config is None else candidate_config)
    sources = [selector(w.decode('utf-8'))[1] for w in (worker, candidate_worker)]
    for source in sources:
        validate_function(source)
    if not isinstance(cases, list) or not cases or len(cases) > 500:
        raise ValueError('DEV_CASE_COUNT_BUDGET')
    rows = []
    for case in cases:
        if case.get('partition', 'DEV') != 'DEV':
            raise ValueError('DEV_PARTITION_REQUIRED')
        if case.get('selector_input_status') == 'UNKNOWN':
            unavailable = {'status': 'DEV_INPUT_UNAVAILABLE', 'reason': case.get('selector_input_reason', 'INCOMPLETE_ARCHIVED_SELECTOR_INPUT'),
                           'retention': {'status': 'UNKNOWN', 'atoms': []}, 'exit_code': None}
            rows.append({'case_id': case.get('case_id', case.get('qid')), 'pair_status': 'DEV_INPUT_UNAVAILABLE',
                         'baseline': dict(unavailable), 'candidate': dict(unavailable), 'chars_delta': None})
            continue
        members = case['members']
        if not isinstance(members, list) or len(members) > 5000:
            raise ValueError('DEV_MEMBER_COUNT_BUDGET')
        ids = [m['id'] for m in members]
        if len(ids) != len(set(ids)) or any(not isinstance(m.get('content'), str) for m in members):
            raise ValueError('DEV_INVALID_MEMBERS')
        args = [members, case['chunks'], case['files'], case['seed_ids'], None, case['query']]
        if not isinstance(args[-1], str) or any(s not in ids for s in args[3]):
            raise ValueError('DEV_INVALID_QUERY_OR_SEEDS')
        arms = []
        for source, selected_config in zip(sources, (baseline_config, candidate_config)):
            args[4] = selected_config['selection']
            result = _execute(source, args, timeout_seconds)
            if result['status'] == 'DEV_EXECUTED':
                selected = [members[i] for i in result['indices']]
                result.update(member_ids=[m['id'] for m in selected], chars=sum(len(m['content']) for m in selected),
                              retention=_retention(case, {m['id'] for m in selected}))
            arms.append(result)
        paired = all(arm['status'] == 'DEV_EXECUTED' for arm in arms)
        rows.append({'case_id': case.get('case_id', case.get('qid')), 'input_sha256': _digest(case),
                     'pair_status': 'DEV_PAIRED' if paired else 'DEV_FAILED',
                     'baseline': arms[0], 'candidate': arms[1],
                     'chars_delta': arms[1]['chars'] - arms[0]['chars'] if paired else None})
    paired = [r for r in rows if r['pair_status'] == 'DEV_PAIRED']
    return {'schema': SCHEMA, 'status': 'DEV_REPLAY_COMPLETE' if len(paired) == len(rows) else 'DEV_REPLAY_INCOMPLETE',
            'formal_admission': False, 'production_changed': False, 'external_calls': 0,
            'metric': METRIC, 'metric_scope': 'member content only; excludes numbered prompt headers and generation',
            'baseline_worker_sha256': hashlib.sha256(worker).hexdigest(),
            'candidate_worker_sha256': hashlib.sha256(candidate_worker).hexdigest(),
            'baseline_config': baseline_config, 'candidate_config': candidate_config,
            'baseline_config_sha256': _digest(baseline_config), 'candidate_config_sha256': _digest(candidate_config),
            'planned_cases': len(rows), 'paired_cases': len(paired), 'failed_cases': len(rows)-len(paired),
            'baseline_chars': sum(r['baseline']['chars'] for r in paired),
            'candidate_chars': sum(r['candidate']['chars'] for r in paired), 'per_case': rows}


def replay_patch(worker, config, patch, cases, *, candidate_config=None, timeout_seconds=5):
    candidate = apply_patch(worker, patch)
    return replay_candidate(worker, config, candidate, cases, candidate_config=candidate_config,
                            timeout_seconds=timeout_seconds)
