import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.tools import retrieval_tool


class FakeSession:
    def __init__(self, engine):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setattr(retrieval_tool, 'Session', FakeSession)
    service = SimpleNamespace(search=AsyncMock())
    monkeypatch.setattr(retrieval_tool, 'get_retrieval_service', lambda: service)
    return service.search


def run_tool():
    return asyncio.run(retrieval_tool.search_knowledge_base.coroutine(query='q', kb_ids=['kb']))


@pytest.mark.parametrize('status', ['NO_RESULTS', 'RETRIEVAL_ERROR', 'UNKNOWN'])
def test_empty_results_preserve_status(search, status):
    search.return_value = ([], {'retrieval_status': status})
    result = json.loads(run_tool())
    assert result['retrieval_status'] == status
    assert result['citations'] == []


def test_success_preserves_evidence_identity(search):
    search.return_value = ([{'id': 'chunk-1', 'file_id': 'file-1', 'file_name': 'one.pdf',
                             'page_number': 7, 'content': 'Evidence', 'score': 0.8}],
                           {'retrieval_status': 'SUCCESS'})
    result = json.loads(run_tool())
    assert result['retrieval_status'] == 'SUCCESS'
    assert result['citations'][0]['fileId'] == 'file-1'
    assert result['citations'][0]['page_number'] == 7
    assert result['citations'][0]['chunk_id'] == 'chunk-1'
    assert result['citations'][0]['content'] == 'Evidence'


def test_spreadsheet_location_uses_sheet_and_not_synthetic_page(search):
    search.return_value = ([{'id': 'chunk-1', 'file_id': 'file-1', 'file_name': 'data.xlsx',
                             'page_number': 1, 'location_info': '第 1 页',
                             'content': '[工作表: T01]\n| Alabama | 219 |', 'score': 0.8}],
                           {'retrieval_status': 'SUCCESS'})
    citation = json.loads(run_tool())['citations'][0]
    assert citation['page_number'] is None
    assert citation['location'] == '工作表: T01'


def test_timeout_propagates(search):
    search.side_effect = TimeoutError('provider timeout')
    with pytest.raises(TimeoutError):
        run_tool()


def test_image_type_survives_tool_metadata(search):
    search.return_value = ([{'id': 'image-1', 'file_id': 'file-1', 'file_name': 'one.pdf',
                             'page_number': 7, 'content': 'generated description',
                             'content_type': 'image', 'score': 0.8}], {'retrieval_status': 'SUCCESS'})
    result = json.loads(run_tool())
    assert result['citations'][0]['content_type'] == 'image'


def test_empty_rerank_preserves_real_retrieval_as_unknown(search, monkeypatch):
    search.return_value = ([{'id': 'chunk-1', 'file_id': 'file-1', 'content': 'real evidence', 'score': 0.8}],
                           {'retrieval_status': 'SUCCESS'})
    monkeypatch.setattr(retrieval_tool, 'RerankService', lambda session: SimpleNamespace(rerank=lambda *a, **k: []))
    result = json.loads(asyncio.run(retrieval_tool.search_knowledge_base.coroutine(query='q', kb_ids=['kb'], use_rerank=True)))
    assert result['retrieval_status'] == 'UNKNOWN'
    assert result['citations'][0]['content'] == 'real evidence'


@pytest.mark.parametrize('original', ['page seven evidence', None, ' '])
def test_expanded_tool_context_and_citation_use_original(search, original):
    search.return_value = ([{'id': 'c1', 'file_id': 'f1', 'page_number': 7,
                             'file_name': 'report.pdf', 'score': 0.8,
                             'content': 'page six secret\npage seven evidence\npage eight secret',
                             'original_content': original, 'metadata': {'is_expanded': True}}],
                           {'retrieval_status': 'SUCCESS'})
    result = json.loads(run_tool())
    assert 'page six secret' not in str(result) and 'page eight secret' not in str(result)
    if original and original.strip():
        assert original in result['context']
        assert result['citations'][0]['content'] == original
        assert result['citations'][0]['fileId'] == 'f1'
        assert result['citations'][0]['page_number'] == 7
    else:
        assert result['citations'] == [] and result['context'] == ''
        assert result['retrieval_status'] == 'UNKNOWN'
