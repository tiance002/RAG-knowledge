"""Isolated runner observability checks; no network or provider calls."""
import importlib
import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / 'scripts' / 'run_p1_http_regression.py'
spec = importlib.util.spec_from_file_location('p1_observability_runner', path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

CREDS = {'LANGFUSE_PUBLIC_KEY': 'public', 'LANGFUSE_SECRET_KEY': 'secret',
         'LANGFUSE_BASE_URL': 'https://us.cloud.langfuse.com'}


def test_prerequisites_do_not_claim_cloud_delivery(monkeypatch):
    monkeypatch.setattr(importlib, 'import_module', lambda name: object())
    status = runner.observability_preflight(CREDS, True)
    assert status == {'langfuse': 'AVAILABLE', 'opentelemetry.sdk.trace': 'AVAILABLE', 'prometheus_client': 'AVAILABLE',
                      'cloud_config': 'CONFIGURED', 'metrics_capture': 'ENABLED',
                      'cloud_delivery': 'NOT_VERIFIED', 'status': 'PASS'}


@pytest.mark.parametrize('missing', ['langfuse', 'opentelemetry.sdk.trace', 'prometheus_client'])
def test_missing_package_blocks_before_execution(monkeypatch, missing, capsys):
    def fake_import(name):
        if name == missing:
            raise ImportError('do not expose details')
        return object()
    monkeypatch.setattr(importlib, 'import_module', fake_import)
    with pytest.raises(RuntimeError, match='observability preflight failed'):
        runner.execute(None, [], {}, {}, {}, 'model', 'https://example.invalid', CREDS,
                       capture_metrics=True, require_observability=True)
    output = capsys.readouterr().out
    assert 'IMPORT_FAILED' in output and 'secret' not in output


@pytest.mark.parametrize('credentials, capture', [({}, True), (CREDS, False),
    ({**CREDS, 'LANGFUSE_BASE_URL': 'https://invalid.example'}, True)])
def test_missing_configuration_or_capture_fails(monkeypatch, credentials, capture):
    monkeypatch.setattr(importlib, 'import_module', lambda name: object())
    assert runner.observability_preflight(credentials, capture)['status'] == 'FAIL'


def test_flag_is_opt_in():
    args = runner.parse_args(['--backend', '/tmp', '--code-sha256', 'x', '--llm-model', 'm', '--embedding-model', 'e'])
    assert args.require_observability is False


def test_explicit_frozen_pdf_selection_keeps_defaults_and_excludes_unselected():
    pdf = {qid: {} for qid in (*runner.PDF, 'selected-pdf', 'unselected-pdf')}
    registry = {qid: {'profile': 'pdf-profile'} for qid in pdf}
    office = {qid: {} for qid in runner.OFFICE}
    default_pdf, default_office = runner.selected_corpus_qids(None, pdf, registry, office)
    assert default_pdf == runner.PDF and default_office == runner.OFFICE
    pdf_qids, office_qids = runner.selected_corpus_qids(['selected-pdf'], pdf, registry, office)
    assert pdf_qids == (*runner.PDF, 'selected-pdf')
    assert office_qids == runner.OFFICE
    assert 'unselected-pdf' not in pdf_qids


@pytest.mark.parametrize('qid', ['unknown', 'qrel-only', 'gold-without-registry'])
def test_selection_rejects_unknown_or_non_gold_registered_pdf(qid):
    pdf = {**{item: {} for item in runner.PDF}, 'gold-without-registry': {}}
    registry = {**{item: {} for item in runner.PDF}, 'qrel-only': {}}
    office = {item: {} for item in runner.OFFICE}
    with pytest.raises(ValueError, match='unknown case filter'):
        runner.selected_corpus_qids([qid], pdf, registry, office)
