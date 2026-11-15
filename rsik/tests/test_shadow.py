from copy import deepcopy
import pytest
from rsik.shadow import ShadowArtifact,shadow_request
from rsik.experiment import bytes_hash


def artifact(name):
    return ShadowArtifact(name,b'SYNTHETIC_WORKER',b'{}',{'candidate_id':name,
        'verdict':'CONTROL_BASELINE_QUALIFIED','worker_sha256':bytes_hash(b'SYNTHETIC_WORKER'),
        'config_sha256':bytes_hash(b'{}'),'production_changed':False})


def request():
    return {'tenant_id':'t','members':[{'id':'s','file_id':'f','content':'fact'},
        {'id':'n','file_id':'f','content':'neighbor'}],'seed_ids':['s']}


@pytest.mark.parametrize('fault',['indices','bool','duplicate','order','mutate','seed','budget','timeout','hash'])
def test_candidate_failure_fallback_never_candidate_success(fault):
    a,b=artifact('candidate'),artifact('baseline');now=[0]
    def run(art,req,deadline):
        if art is b:return [0]
        if fault=='indices':return [2]
        if fault=='bool':return [True]
        if fault=='duplicate':return [0,0]
        if fault=='order':return [1,0]
        if fault=='mutate':req['members'][0]['content']='changed';return [0]
        if fault=='seed':return [1]
        if fault=='budget':return [0,1]
        if fault=='timeout':raise TimeoutError()
        raise RuntimeError('isolated artifact hash check failed')
    before=request();out=shadow_request(before,primary=a,baseline=b,runner=run,deadline=10,
        context_budget=5,authorized_files={'f'},authorized_tenant='t',control=True,clock=lambda:now[0])
    assert out['status']=='COMPLETED' and out['served_strategy']=='baseline'
    assert not out['candidate_success'] and len(out['attempts'])==2
    assert before==request() and out['production_changed'] is False


def test_total_deadline_exhausted_does_not_restart_timeout():
    now=[0];calls=[]
    def run(a,r,deadline):calls.append(a.candidate_id);now[0]=11;return [0]
    out=shadow_request(request(),primary=artifact('c'),baseline=artifact('b'),runner=run,
        deadline=10,context_budget=30,authorized_files={'f'},authorized_tenant='t',control=True,clock=lambda:now[0])
    assert calls==['c'] and out['status']=='REQUEST_FAILED'


def test_no_cross_tenant_fallback_and_no_real_qualification_from_control():
    def never(*a,**k):pytest.fail('must not execute')
    for control,tenant in [(True,'other'),(False,'t')]:
        out=shadow_request(request(),primary=artifact('c'),baseline=artifact('b'),runner=never,
            deadline=10,context_budget=30,authorized_files={'f'},authorized_tenant=tenant,control=control,clock=lambda:0)
        assert not out['candidate_success'] and out['served_strategy'] is None


def test_baseline_failure_no_fabricated_answer():
    out=shadow_request(request(),primary=artifact('c'),baseline=artifact('b'),runner=lambda *a,**k:[],
        deadline=10,context_budget=30,authorized_files={'f'},authorized_tenant='t',control=True,clock=lambda:0)
    assert out['status']=='REQUEST_FAILED' and len(out['attempts'])==2 and 'indices' not in out


def test_uncovered_distribution_uses_only_baseline():
    calls=[]
    def run(a,*args,**kw):calls.append(a.candidate_id);return [0]
    out=shadow_request(request(),primary=artifact('c'),baseline=artifact('b'),runner=run,
        deadline=10,context_budget=30,authorized_files={'f'},authorized_tenant='t',control=True,supported=False,clock=lambda:0)
    assert calls==['b'] and not out['candidate_success'] and out['fallback_reason']=='UNSUPPORTED_DISTRIBUTION'


def test_corrupt_primary_artifact_is_not_a_global_authorization_failure():
    good=artifact('c');bad=ShadowArtifact('c',b'CORRUPT',good.config,good.qualification)
    out=shadow_request(request(),primary=bad,baseline=artifact('b'),runner=lambda *a,**kw:[0],
        deadline=10,context_budget=30,authorized_files={'f'},authorized_tenant='t',control=True,clock=lambda:0)
    assert out['served_strategy']=='b' and out['fallback_reason']=='ARTIFACT_CHANGED' and not out['candidate_success']
