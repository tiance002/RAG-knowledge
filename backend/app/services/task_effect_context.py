"""Opt-in provider effect fencing for staged ingestion, including semantic splits."""
from contextvars import ContextVar
from contextlib import contextmanager


_runner = ContextVar('ingest_effect_runner', default=None)
_provider_ids = ContextVar('ingest_provider_ids', default=None)


def has_active_effect_scope():
    """Let optional query optimizations preserve ingestion effect accounting."""
    return _runner.get() is not None


@contextmanager
def effect_scope(runner):
    token = _runner.set(runner)
    try:
        yield
    finally:
        _runner.reset(token)


async def invoke_effect(kind, identity, callback):
    runner = _runner.get()
    if runner is None:
        return await callback()
    return await runner(kind, identity, callback)


@contextmanager
def capture_provider_ids():
    values = []
    token = _provider_ids.set(values)
    try:
        yield values
    finally:
        _provider_ids.reset(token)


def record_provider_id(value, status_code=None):
    values = _provider_ids.get()
    if values is not None:
        values.append({'request_id': value, 'status_code': status_code})


def raise_if_effect_error(exc):
    """Parser fallbacks must not turn uncertain paid requests into success."""
    if getattr(exc, 'ingest_effect_error', False):
        raise exc
