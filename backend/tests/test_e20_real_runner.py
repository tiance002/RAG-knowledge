"""The approved experiment's network guard must accept actual AnyIO DNS input."""
import importlib.util
from pathlib import Path
import socket
import pytest


spec = importlib.util.spec_from_file_location('e20_real_runner', Path(__file__).resolve().parents[2]/'scripts/run_e20_real_pair.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('host', ['api.siliconflow.cn', b'api.siliconflow.cn', 'us.cloud.langfuse.com', b'us.cloud.langfuse.com'])
def test_guard_accepts_anyio_bytes_and_string_hosts(monkeypatch, host):
    calls = []
    def dns(name, port, *args, **kwargs):
        calls.append((name,port))
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.8',443))]
    monkeypatch.setattr(socket,'getaddrinfo',dns)
    with runner.AllowedNetwork():
        result = socket.getaddrinfo(host,443)
        assert result[0][4][0]=='203.0.113.8'
    assert calls == [(host.decode() if isinstance(host,bytes) else host,443)]


@pytest.mark.parametrize('host,port', [('example.com',443),(b'api.siliconflow.cn.evil.test',443),('api.siliconflow.cn',80)])
def test_guard_rejects_other_destinations_before_original_dns(monkeypatch,host,port):
    calls=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:calls.append(a))
    with runner.AllowedNetwork(),pytest.raises(RuntimeError,match='before resolution'):
        socket.getaddrinfo(host,port)
    assert calls==[]


def test_continuation_skips_unknown_case_and_retains_original_four_attempt_cap():
    cases=[{'qid':'excel','arm':'A'},{'qid':'excel','arm':'B'}, {'qid':'word','arm':'A'},{'qid':'word','arm':'B'}]
    prior={'status':'STOPPED','model_requests_sent':1,'results':[{'qid':'excel','arm':'A','provider':{'status':'UNKNOWN'}}], 'not_run_cases':cases[1:]}
    assert runner.continuation_start({'cases':cases},prior)==1
    assert cases[runner.continuation_start({'cases':cases},prior):]==cases[1:]
    invalid={**prior,'not_run_cases':cases}
    with pytest.raises(AssertionError):runner.continuation_start({'cases':cases},invalid)
