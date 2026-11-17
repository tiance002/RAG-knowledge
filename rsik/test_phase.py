"""Explicit development/test boundary; immutable system code during autonomy runs."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json
from .locking import atomic_write
from .state import canonical


def sha(data):return hashlib.sha256(data).hexdigest()


def freeze(root,files,output,*,baseline,validation,budget_snapshot):
    root=Path(root).resolve();hashes={}
    if validation.get('exit_code')!=0 or validation.get('failed')!=0 or validation.get('skipped')!=0 or validation.get('passed',0)<=0:
        raise ValueError('PASSING_SYSTEM_VALIDATION_REQUIRED')
    for file in files:
        path=Path(file).resolve()
        if not path.is_relative_to(root):raise ValueError('FROZEN_CODE_ESCAPES_ROOT')
        hashes[path.relative_to(root).as_posix()]=sha(path.read_bytes())
    manifest={'schema':'rsi-autonomous-test-freeze/v1','phase':'AUTONOMOUS_TEST',
        'created_at':datetime.now(timezone.utc).isoformat(),'root':str(root),'code_hashes':hashes,
        'baseline':baseline,'system_validation':validation,'budget_at_start':budget_snapshot,
        'ownership':{'system_changes':'DEVELOPMENT_ONLY','candidate_changes':'RSI_MODEL_ONLY',
                     'monitor':'read logs, fees and evidence; no strategy formulas or candidate edits'},
        'claim_scope':'autonomous target-policy optimization; no RSI-system self-modification claim'}
    atomic_write(output,canonical(manifest),exclusive=True)
    return sha(Path(output).read_bytes())


def assert_frozen(path,expected_hash):
    data=Path(path).read_bytes()
    if sha(data)!=expected_hash:raise ValueError('AUTONOMY_FREEZE_MANIFEST_CHANGED')
    manifest=json.loads(data)
    if manifest.get('phase')!='AUTONOMOUS_TEST' or manifest.get('schema')!='rsi-autonomous-test-freeze/v1':
        raise ValueError('INVALID_AUTONOMY_TEST_PHASE')
    root=Path(manifest['root']).resolve()
    for name,expected in manifest['code_hashes'].items():
        code=(root/name).resolve()
        if not code.is_relative_to(root) or not code.is_file() or sha(code.read_bytes())!=expected:
            raise ValueError('FROZEN_SYSTEM_CHANGED')
    return manifest
