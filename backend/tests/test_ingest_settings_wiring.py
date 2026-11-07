"""Isolated contracts; no provider requests or production data."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine


def test_explicit_zero_overlap_survives_parser(tmp_path, monkeypatch):
    from app.services.parser import FileParser, TextSplitter
    assert TextSplitter(chunk_size=128, chunk_overlap=0).chunk_overlap == 0
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), 'IMAGE_DIR', str(tmp_path / 'images'))
    source = tmp_path / 'source.txt'
    source.write_text('One public sentence. ' * 20)
    parser = FileParser('isolated-zero-overlap')
    chunks = parser.parse(str(source), chunk_mode='custom', chunk_size=128, chunk_overlap=0)
    assert chunks
    assert parser.splitter.chunk_overlap == 0


def test_missing_kb_model_never_constructs_another_embedder(tmp_path, monkeypatch):
    from app.models import KnowledgeBase
    from app.services import retrieval
    from app.services.model_bindings import ModelBindingError
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    SQLModel.metadata.create_all(engine)
    calls = []
    monkeypatch.setattr(retrieval, 'EmbeddingService', lambda **kw: calls.append(kw))
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='Public fixture', embedding_model='missing-model', vlm_model='default'))
        session.commit()
        with pytest.raises(ModelBindingError):
            asyncio.run(retrieval.RetrievalService._embed_query_for_kb(None, session, 'kb', 'public question'))
    assert calls == []
    engine.dispose()


def _image_fixture(tmp_path, monkeypatch):
    from app.core.config import get_settings
    from app.services.parser import ParsedChunk
    settings = get_settings()
    monkeypatch.setattr(settings, 'IMAGE_DIR', str(tmp_path / 'images'))
    monkeypatch.setattr(settings, 'PDF_AUDIT_DIR', str(tmp_path / 'audit'))
    (tmp_path / 'images/kb').mkdir(parents=True)
    (tmp_path / 'images/kb/public.png').write_bytes(b'public synthetic image identity')
    return lambda: ParsedChunk('[image: kb/public.png]', 3, 'image', 'kb/public.png',
                               {'source_locator': {'page': 3, 'bbox': [0, 0, 1, 1]}})


def test_completed_caption_is_reused_and_visibly_derived(tmp_path, monkeypatch):
    from app.services.vlm import describe_parsed_images
    fresh = _image_fixture(tmp_path, monkeypatch)
    calls = []
    class Service:
        base_url, model, prompt = 'http://synthetic.invalid', 'synthetic', 'describe'
        async def describe_image(self, path):
            calls.append(path)
            return 'A public chart.'
    first, second = fresh(), fresh()
    describe_parsed_images([first], 'kb', Service(), asyncio.run)
    result = describe_parsed_images([second], 'kb', Service(), asyncio.run)
    assert len(calls) == 1
    assert result['cached'] == 1
    assert second.metadata['content_origin'] == 'model_generated_image_description'
    assert second.metadata['source_locator'] == first.metadata['source_locator']
    assert second.image_path == 'kb/public.png'
    assert second.content.endswith('A public chart.')


def test_unknown_caption_keeps_image_and_does_not_reissue(tmp_path, monkeypatch):
    from app.services.vlm import describe_parsed_images, VLMDescriptionError
    fresh = _image_fixture(tmp_path, monkeypatch)
    calls = []
    class Service:
        base_url, model, prompt = 'http://synthetic.invalid', 'synthetic', 'describe'
        async def describe_image(self, path):
            calls.append(path)
            raise VLMDescriptionError('UNKNOWN')
    first, second = fresh(), fresh()
    assert describe_parsed_images([first], 'kb', Service(), asyncio.run)['UNKNOWN'] == 1
    assert describe_parsed_images([second], 'kb', Service(), asyncio.run)['UNKNOWN'] == 1
    assert len(calls) == 1
    assert second.content == '[image: kb/public.png]'


def test_worker_pool_uses_database_concurrency_after_cli(monkeypatch):
    from app.services import runtime_settings
    from app.tasks.celery_app import apply_runtime_worker_settings
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda: SimpleNamespace(
        max_concurrency=2, revision=7, degraded=False))
    worker = SimpleNamespace(concurrency=1, autoscale=None, app=SimpleNamespace(conf=SimpleNamespace()))
    apply_runtime_worker_settings(sender=worker)
    assert worker.concurrency == 2
    assert worker.app.conf.worker_concurrency == 2


def test_frozen_no_vlm_does_not_read_new_default(monkeypatch):
    from app.tasks.parse_tasks import _resolve_vlm_binding
    from app.services import model_bindings, runtime_settings
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError('frozen no-selection must not read changed defaults')
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', forbidden)
    monkeypatch.setattr(model_bindings, 'resolve_model', lambda session, role, identity, **kw: calls.append((role,identity,kw)) or None)
    assert _resolve_vlm_binding(None, SimpleNamespace(vlm_model='new-kb-model'), None) is None
    assert calls == [('vlm',None,{'allow_unset':True,'use_default':False})]
