"""Zero-network acceptance harness exercises real HTTP/SSE with fake providers."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/run_e12_acceptance.py'
spec = importlib.util.spec_from_file_location('e12_harness', SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_network_guard_denies_external_requests():
    import socket
    import pytest
    with harness.NetworkGuard(False) as guard:
        with pytest.raises(OSError, match='network denied'):
            socket.getaddrinfo('example.com', 443)
        assert guard.denied == 1


def test_http_fulltext_e12_off_and_unknown(tmp_path):
    results = []
    for schema, scenario in [('e12','fulltext'), ('off','fulltext'), ('e12','unknown')]:
        output = tmp_path / (schema + '-' + scenario)
        command = [sys.executable, str(SCRIPT), '--execute', '--schema', schema, '--case', scenario, '--output-dir', str(output)]
        env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'
        completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = json.loads((output / 'result.json').read_text(encoding='utf8'))
        assert result['status'] == 'PASS' and result['external_model_requests'] == 0 and result['network_denials'] == 0
        results.append(result)
    observed, disabled, unknown = results
    assert observed['answer'] == disabled['answer'] and observed['citations'] == disabled['citations']
    assert observed['provider_request_sha256'] == disabled['provider_request_sha256']
    assert observed['fake_calls'] == disabled['fake_calls'] and disabled['observations_per_trace'] == 0
    assert observed['generation_count'] == 1 and not observed['missing_parents']
    assert unknown['fake_calls']['fake_provider'] == 1 and unknown['errors'][-1]['status'] == 'UNKNOWN' and not unknown['done']


def test_real_agent_graph_two_rounds(tmp_path):
    env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'
    output = tmp_path/'agent'
    cmd = [sys.executable, str(SCRIPT), '--execute', '--schema', 'e12', '--case', 'agent_two_rounds', '--output-dir', str(output)]
    completed = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    row = json.loads((output/'result.json').read_text(encoding='utf8'))
    assert row['status'] == 'PASS' and row['fake_calls']['tool'] == 2
    assert row['fake_calls']['fake_agent_model'] == 3 and row['fake_calls']['fake_grade_model'] == 1
    assert row['generation_count'] == 4 and not row['missing_parents'] and row['external_model_requests'] == 0


def test_http_request_cancellation_is_not_reported_as_http_success(tmp_path):
    env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'; output = tmp_path/'cancel'
    cmd = [sys.executable, str(SCRIPT), '--execute', '--schema', 'e12', '--case', 'cancel', '--output-dir', str(output)]
    completed = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    row=json.loads((output/'result.json').read_text(encoding='utf8'))
    assert row['status']=='PASS' and row['http_status'] == 200 and row['transport_state']=='CANCELLED'
    assert row['fake_calls']['fake_provider']==1 and not row['done'] and row['external_model_requests']==0
    assert any(s['name']=='qa.request' and s['execution_status']=='CANCELLED' for s in row['observation_statuses'])
    assert any(s['name']=='answer.generate' and s['execution_status']=='CANCELLED' for s in row['observation_statuses'])


def test_authorized_inline_agent_keeps_sensitive_sentinel_out_of_export(tmp_path):
    env = os.environ.copy(); env['PYTHONIOENCODING'] = 'utf-8'; output = tmp_path/'inline-agent'
    cmd = [sys.executable, str(SCRIPT), '--execute', '--schema', 'e12', '--content-policy', 'INLINE', '--case', 'agent_two_rounds', '--output-dir', str(output)]
    completed = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=90)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    row=json.loads((output/'result.json').read_text(encoding='utf8'))
    assert row['status']=='PASS' and row['content_policy']=='INLINE' and row['fake_calls']['tool']==2
    assert row['generation_count']==4 and not row['telemetry_errors']
    assert harness.SENTINEL not in (output/'observations.json').read_text(encoding='utf8')
