"""Resolve model identities once, without silently changing providers."""
from dataclasses import dataclass, field
from app.core.config import get_settings
from app.models import CustomModel, ModelType


class ModelBindingError(ValueError):
    pass


@dataclass(frozen=True)
class ModelBinding:
    binding_id: str
    model_name: str
    base_url: str
    api_key: str = field(repr=False)
    model_type: str = ''
    context_length: int = 4096
    source: str = 'custom_model'


def resolve_model(session, model_type, model_id=None, *, allow_unset=False, use_default=True):
    role = model_type.value if isinstance(model_type, ModelType) else str(model_type).lower()
    if role not in ('llm', 'chat', 'embedding', 'vlm', 'rerank'):
        raise ModelBindingError('MODEL_ROLE_INVALID')
    identity = model_id
    if identity is None and use_default and role != 'rerank':
        from app.services.runtime_settings import get_runtime_settings
        identity = getattr(get_runtime_settings(session), 'default_' + ('llm' if role == 'chat' else role) + '_model')
    if identity in (None, ''):
        if allow_unset:
            return None
        raise ModelBindingError('MODEL_NOT_SELECTED: ' + role)
    settings = get_settings()
    if identity == 'default' and role == 'vlm':
        identity = 'sys_vlm'
    system_role = 'llm' if role == 'chat' else role
    if identity == 'sys_' + system_role and role != 'rerank':
        prefix = system_role.upper()
        name = getattr(settings, prefix + '_MODEL').strip()
        url = getattr(settings, prefix + '_BASE_URL').strip()
        if not name or not url:
            raise ModelBindingError('SYSTEM_MODEL_NOT_CONFIGURED: ' + role)
        limit = settings.EMBEDDING_MAX_CONTEXT_LENGTH if role == 'embedding' else 4096
        return ModelBinding(identity, name, url, getattr(settings, prefix + '_API_KEY'),
                            system_role, limit, source='deployment')
    row = session.get(CustomModel, identity)
    if row is None:
        raise ModelBindingError('MODEL_REFERENCE_MISSING: ' + role)
    valid_roles = ('llm', 'vlm') if role == 'chat' else (role,)
    if not row.is_active or row.model_type.value not in valid_roles or not row.model_name.strip() or not row.base_url.strip():
        raise ModelBindingError('MODEL_REFERENCE_INVALID: ' + role)
    return ModelBinding(row.id, row.model_name, row.base_url, row.api_key, row.model_type.value, row.context_length)


def model_references(session, model_id):
    """Return public reference labels; never include credentials or full content."""
    from sqlmodel import select
    from app.models import KnowledgeBase
    from app.services.runtime_settings import PersistedSystemSettings, _decode
    refs = []
    for kb in session.exec(select(KnowledgeBase).where(
            (KnowledgeBase.embedding_model == model_id) | (KnowledgeBase.vlm_model == model_id))).all():
        refs.append('knowledge_base:' + kb.id)
    row = session.get(PersistedSystemSettings, 'system')
    if row is not None:
        values, _, _ = _decode(row.data)
        refs.extend('settings:' + key for key in ('default_llm_model', 'default_embedding_model', 'default_vlm_model')
                    if values.get(key) == model_id)
    # Queued jobs freeze model IDs even after their global default is cleared.
    # Query the optional table directly to avoid importing dispatch/Celery while
    # resolving a model, and to remain usable before task-table migration.
    from sqlalchemy import inspect, text
    if inspect(session.connection()).has_table('file_task_receipts'):
        active = session.execute(text(
            "SELECT task_id, payload FROM file_task_receipts "
            "WHERE active_file IS NOT NULL OR status IN "
            "('QUEUED', 'PUBLISH_UNKNOWN', 'RUNNING', 'UNKNOWN')"
        )).all()
        for task_id, raw_payload in active:
            import json
            try:
                payload = json.loads(raw_payload)
                if not isinstance(payload, dict):
                    raise ValueError('task payload must be an object')
            except (ValueError, TypeError) as exc:
                # Do not expose stored data or guess that unreadable jobs are
                # unrelated to the model whose identity is about to change.
                raise ModelBindingError('ACTIVE_TASK_REFERENCES_UNREADABLE') from exc
            if any(payload.get(key) == model_id for key in
                   ('vector_model', 'embedding_model', 'vlm_model_id')):
                refs.append('task:' + task_id)
    return refs
