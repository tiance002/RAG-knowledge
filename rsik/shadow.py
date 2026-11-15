"""Isolated shadow selector. No Gold, production clients, or dynamic code loading.

The runner is a trusted isolated execution adapter; this module never executes
model source in the request process. Structural checks cannot certify semantics.
"""
from copy import deepcopy
from dataclasses import dataclass
import time
from .errors import StageFailure
from .state import digest


REASONS = {'ARTIFACT_CHANGED','OUTPUT_CONTRACT','INPUT_MUTATED','OUTPUT_BUDGET',
    'REQUEST_DEADLINE','RUNNER_FAILURE','SOURCE_AUTHORIZATION','QUALIFICATION_UNKNOWN',
    'UNSUPPORTED_DISTRIBUTION'}


@dataclass(frozen=True)
class ShadowArtifact:
    candidate_id: str
    worker: bytes
    config: bytes
    qualification: dict

    def validate(self, *, control=False):
        from .experiment import bytes_hash
        q=self.qualification
        expected={'VERIFIED','BASELINE_QUALIFIED'} if not control else {'CONTROL_VERIFIED','CONTROL_BASELINE_QUALIFIED'}
        if (q.get('candidate_id')!=self.candidate_id or q.get('verdict') not in expected
                or q.get('production_changed') is not False):
            raise StageFailure('CONTRACT_ERROR','QUALIFICATION_UNKNOWN',global_stop=True)
        if q.get('worker_sha256')!=bytes_hash(self.worker) or q.get('config_sha256')!=bytes_hash(self.config):
            raise StageFailure('CONTRACT_ERROR','ARTIFACT_CHANGED',global_stop=False)
        return self


def check_indices(indices, before, after, *, authorized_files, context_budget):
    if before!=after:
        raise StageFailure('CONTRACT_ERROR','INPUT_MUTATED')
    members=before['members']
    if (not isinstance(indices,list) or any(type(i)is not int or i<0 or i>=len(members) for i in indices)
            or indices!=sorted(set(indices))):
        raise StageFailure('CONTRACT_ERROR','OUTPUT_CONTRACT')
    selected=[members[i] for i in indices]
    if (any(m['file_id'] not in authorized_files for m in selected)
            or any(m['file_id'] not in authorized_files for m in members)):
        raise StageFailure('INPUT_ERROR','SOURCE_AUTHORIZATION',global_stop=True)
    ids={m['id'] for m in selected}
    if not set(before['seed_ids'])<=ids:
        raise StageFailure('CONTRACT_ERROR','OUTPUT_CONTRACT')
    size=sum(len(m['content']) for m in selected)+max(0,len(selected)-1)
    if size>context_budget:
        raise StageFailure('CONTRACT_ERROR','OUTPUT_BUDGET')
    return size


def shadow_request(request, *, primary, baseline, runner, deadline, context_budget,
                   authorized_files, authorized_tenant, control=False, clock=time.monotonic,
                   qualification_validator=None, supported=True, primary_seconds=30):
    """At most one primary and one baseline run within one shared deadline.

    Real qualifications must be revalidated by a trusted archive reader before
    any runner call. Metadata alone is insufficient. Tenant/source failures
    never fall back across authorization boundaries.
    """
    start=clock();result={'mode':'SHADOW_ONLY','production_changed':False,
        'primary_strategy':primary.candidate_id,'served_strategy':None,'fallback_reason':None,
        'candidate_success':False,'status':'REQUEST_FAILED','attempts':[]}
    if request.get('tenant_id')!=authorized_tenant or any(m['file_id'] not in authorized_files for m in request['members']):
        result['fallback_reason']='SOURCE_AUTHORIZATION';result['status']='AUTHORIZATION_FAILURE';return result
    if not control and qualification_validator is None:
        result['fallback_reason']='QUALIFICATION_UNKNOWN';return result
    if not control and type(runner) is not CapsuleShadowRunner:
        result['fallback_reason']='BOUNDED_ISOLATED_RUNNER_REQUIRED';return result
    def qualified(artifact):
        artifact.validate(control=control)
        if not control:
            proof=qualification_validator(artifact)
            if (not isinstance(proof,dict) or proof.get('candidate_id')!=artifact.candidate_id
                    or proof.get('evidence_mode')!='REAL_REPLAY' or proof.get('status')!='PASS'):
                raise StageFailure('CONTRACT_ERROR','QUALIFICATION_UNKNOWN',global_stop=True)
    artifacts=[primary,baseline] if supported else [baseline]
    if not supported:result['fallback_reason']='UNSUPPORTED_DISTRIBUTION'
    for artifact in artifacts:
        t=clock()
        if t>=deadline:
            result['fallback_reason']=result['fallback_reason'] or 'REQUEST_DEADLINE';break
        try:
            qualified(artifact)
            before=deepcopy(request);actual=deepcopy(request)
            step_deadline=min(deadline,t+primary_seconds) if artifact is primary else deadline
            indices=runner(artifact,actual,deadline=step_deadline)
            if clock()>step_deadline:raise StageFailure('TIMEOUT','REQUEST_DEADLINE')
            size=check_indices(indices,before,actual,authorized_files=authorized_files,context_budget=context_budget)
            result['attempts'].append({'strategy':artifact.candidate_id,'status':'COMPLETED',
                'seconds':clock()-t,'context_chars':size,'output_hash':digest(indices)})
            result.update(status='COMPLETED',served_strategy=artifact.candidate_id,
                candidate_success=artifact is primary and supported,indices=indices)
            break
        except StageFailure as error:
            result['attempts'].append({'strategy':artifact.candidate_id,'status':'FAILED',
                'reason':error.reason_code,'seconds':clock()-t})
            result['fallback_reason']=result['fallback_reason'] or error.reason_code
            if error.global_stop:break
        except (TimeoutError,OSError,RuntimeError):
            result['attempts'].append({'strategy':artifact.candidate_id,'status':'FAILED',
                'reason':'RUNNER_FAILURE','seconds':clock()-t})
            result['fallback_reason']=result['fallback_reason'] or 'RUNNER_FAILURE'
    result['seconds']=clock()-start
    result['scope']='structural shadow availability only; semantic quality not certified'
    return result


class CapsuleShadowRunner:
    """Prepared private Capsule, remote worker deadline and bounded RPC wait.

    Twenty seconds remote cleanup plus five seconds transport cancellation must
    fit the assigned strategy deadline. Shorter deadlines do not start a job.
    Setup/qualification is done before entering the request, never on a user
    traffic path. This adapter is not mounted into any production request route.
    """
    def __init__(self,capsule,directory):
        from pathlib import Path
        from .capsule import Capsule
        import threading
        if not isinstance(capsule,Capsule) or not capsule.ready:
            raise ValueError('PREPARED_PRIVATE_CAPSULE_REQUIRED')
        self.capsule=capsule;self.directory=Path(directory);self.number=0
        self.lock=threading.Lock()

    def __call__(self,artifact,request,*,deadline):
        from .locking import atomic_write
        from .state import canonical
        from .experiment import load
        if deadline-time.monotonic()<=25:raise TimeoutError('REQUEST_DEADLINE')
        if not self.lock.acquire(blocking=False):raise RuntimeError('SHADOW_CAPSULE_ALREADY_IN_USE')
        old=self.capsule.rpc
        try:
            self.number+=1;out=self.directory/str(self.number);out.mkdir(parents=True,exist_ok=False)
            for name,data in [('worker.py',artifact.worker),('config.json',artifact.config),
                              ('input.json',canonical(request['snapshot']))]:
                atomic_write(out/name,data,exclusive=True)
            def rpc(message,*,timeout=115):
                left=deadline-time.monotonic()-5
                if left<=0:raise TimeoutError('REQUEST_DEADLINE')
                return old(message,timeout=min(timeout,left))
            self.capsule.rpc=rpc;self.capsule.deadline=deadline-25
            capture=self.capsule.child('deliver',[out/'input.json',out/'config.json',out/'capture.json'],out,
                worker=out/'worker.py',business_root=self.capsule.business_root)
            positions={m['id']:i for i,m in enumerate(request['members'])}
            if capture.get('status')!='OK' or any(m['id'] not in positions for m in capture['members']):
                raise StageFailure('CONTRACT_ERROR','OUTPUT_CONTRACT')
            return [positions[m['id']] for m in capture['members']]
        finally:
            self.capsule.rpc=old;self.lock.release()
