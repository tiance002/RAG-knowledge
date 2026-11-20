"""Controlled F2 receipts use synthetic inputs and the approved formal product."""
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import rsi_quality_execute as execute
import rsi_quality_registry as registry
from rsi_candidates import canonical, sha
from rsi_quality_controlled_worker import verify_approved_source


def artifact(top_k):
    return {'prompt_addendum': '', 'memory_addendum': '',
            'workflow': {'mode': 'chat', 'use_rewrite': False},
            'retrieval': {'search_mode': 'fulltext', 'top_k': top_k, 'context_window': 0,
                          'rerank_enabled': False, 'score_threshold': 0,
                          'vector_weight': 0.7, 'bm25_weight': 0.3}}


def test_controlled_invalid_artifact_rejected_before_source_access(tmp_path):
    candidate = artifact(1)
    candidate['retrieval']['rerank_enabled'] = True
    with pytest.raises(ValueError):
        execute.execute_controlled_artifact(candidate, tmp_path / 'missing', tmp_path / 'out',
            product_root=tmp_path / 'not-source', source_fingerprint={})
    assert not (tmp_path / 'out').exists()


def test_controlled_guard_denies_external_and_unapproved_loopback_and_writes(tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    code = '''
import socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from rsi_quality_controlled_worker import install_guards
install_guards(Path(sys.argv[2]), Path(sys.argv[3]))
checks = 0
for address in [('203.0.113.1', 443), ('127.0.0.1', 1)]:
    connection = socket.socket()
    try:
        connection.connect(address)
        raise AssertionError('network guard did not deny')
    except RuntimeError as error:
        assert 'denied network' in str(error)
        checks += 1
    finally:
        connection.close()
try:
    Path(sys.argv[2], 'forbidden.txt').write_text('forbidden')
    raise AssertionError('write guard did not deny')
except RuntimeError as error:
    assert 'denied write' in str(error)
    checks += 1
print(checks)
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'scripts'),
                             str(tmp_path), str(output)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == '3'
    assert not (tmp_path / 'forbidden.txt').exists()
