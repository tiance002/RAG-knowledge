"""Regression checks for exact delivered-context extraction, without providers."""
import importlib.util
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[2] / 'scripts/dynamic_router_freeze12.py'
spec = importlib.util.spec_from_file_location('dynamic_router_freeze12', path)
freeze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freeze)


def test_selected_blocks_preserve_separators_and_ignore_internal_brackets():
    context = '[1] 来源: report.pdf\nvalue [15] here\n\n[2] 来源: sheet.xlsx\n| A | 2 |\n'
    system = 'assistant\n参考资料：\n' + context + '\n要求：\nanswer'
    actual, citations = freeze.selected_citations(system)
    assert actual == context
    rebuilt = ''.join('[' + c['citation_label'] + '] 来源: ' + c['source_file'] + '\n' + c['content'] for c in citations)
    assert rebuilt == context
    assert len(citations) == 2
    assert citations[1]['source_metadata']['source_format'] == 'xlsx'
    assert all('score' not in c and 'chunk_id' not in c for c in citations)
    assert citations[0]['content_sha256'] == freeze.sha(citations[0]['content'].encode('utf-8'))


def test_missing_reference_blocks_fail_closed():
    with pytest.raises(AssertionError, match='no recoverable'):
        freeze.selected_citations('参考资料：\nnot a captured reference\n要求：\nanswer')
