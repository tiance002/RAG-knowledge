import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'run_e11_cloud_acceptance.py'
spec = importlib.util.spec_from_file_location('e11_harness_test', SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_guard_denies_network_in_offline_mode():
    with harness.NetworkGuard(False) as guard:
        try:
            socket.getaddrinfo('example.com', 443)
        except OSError:
            pass
        else:
            raise AssertionError('network was not blocked')
        assert guard.denied == 1


def test_preflight_no_network():
    with harness.NetworkGuard(False) as guard:
        result = harness.preflight()
    assert result['network_requests'] == 0 and result['provider_requests'] == 0
    assert guard.denied == 0


def test_isolated_http_sse_actual_path(tmp_path):
    output = tmp_path / 'run'
    result = subprocess.run([sys.executable, str(SCRIPT), '--execute', '--output-dir', str(output)],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'result.json').read_text(encoding='utf-8'))
    assert report['status'] == 'PASS' and report['external_model_requests'] == 0
    assert report['cloud_export'] == 'NOT_RUN' and report['synthetic_fake_provider_calls'] == 1
    rows = [json.loads(x) for x in (output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert sum(row.get('stage') == 'observation.io' for row in rows) >= 2
    assert harness.SENTINEL not in (output / 'events.jsonl').read_text(encoding='utf-8')
    assert 'event: done' in (output / 'sse.txt').read_text(encoding='utf-8')
