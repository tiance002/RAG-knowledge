"""Versioned authority for settings exposed by the settings page.

Environment values seed a missing installation once. They never overwrite an
existing database value. The process snapshot is a last-known-good fallback,
not a second persistent configuration store.
"""
from dataclasses import dataclass, replace
import json
from typing import Optional

from pydantic import BaseModel, Field as ValidationField, model_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import SQLModel, Field, Session

from app.core.config import get_settings


class PersistedSystemSettings(SQLModel, table=True):
    __tablename__ = 'system_settings'
    id: str = Field(default='system', primary_key=True)
    data: str = '{}'


class RuntimeValues(BaseModel):
    default_llm_model: Optional[str] = None
    default_embedding_model: Optional[str] = None
    default_vlm_model: Optional[str] = None
    max_concurrency: int = ValidationField(default=1, ge=1, le=32)
    chunk_size: int = ValidationField(default=500, ge=128, le=2048)
    chunk_overlap: int = ValidationField(default=50, ge=0, le=512)
    max_chat_history_rounds: int = ValidationField(default=20, ge=1, le=100)

    @model_validator(mode='after')
    def valid_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError('chunk_overlap must be smaller than chunk_size')
        return self


NUMERIC_DEFAULTS = {key: value for key, value in RuntimeValues().model_dump().items()
                    if not key.startswith('default_')}
SETTING_FIELDS = {
    'default_llm_model': 'DEFAULT_LLM_MODEL_ID',
    'default_embedding_model': 'DEFAULT_EMBEDDING_MODEL_ID',
    'default_vlm_model': 'DEFAULT_VLM_MODEL_ID',
    'max_concurrency': 'MAX_PARSING_WORKERS',
    'chunk_size': 'CHUNK_SIZE', 'chunk_overlap': 'CHUNK_OVERLAP',
    'max_chat_history_rounds': 'MAX_CHAT_HISTORY_ROUNDS',
}


@dataclass(frozen=True)
class RuntimeSnapshot:
    default_llm_model: Optional[str]
    default_embedding_model: Optional[str]
    default_vlm_model: Optional[str]
    max_concurrency: int
    chunk_size: int
    chunk_overlap: int
    max_chat_history_rounds: int
    revision: int
    degraded: bool = False
    config_error: Optional[str] = None


class RuntimeSettingsUnavailable(RuntimeError):
    pass


class SettingsConflict(RuntimeError):
    pass


_last_good = {}


def _cache_key(session):
    return session.get_bind()


def _seed_values(session):
    settings = get_settings()
    values = {key: getattr(settings, attr) for key, attr in SETTING_FIELDS.items()}
    # Current Worker concurrency is the real deployment value, unlike the old
    # unused MAX_PARSING_WORKERS UI default.
    values['max_concurrency'] = settings.CELERY_WORKER_CONCURRENCY
    for role in ('llm', 'embedding', 'vlm'):
        field = 'default_' + role + '_model'
        if values[field]:
            continue
        prefix = role.upper()
        model = getattr(settings, prefix + '_MODEL').strip()
        if not model:
            values[field] = None
            continue
        # Reuse a user-provided model record only when it identifies the exact
        # same configured provider and credentials. Never guess from a name or
        # from the presence of a single unrelated model in the model list.
        from sqlmodel import select
        from app.models import CustomModel, ModelType
        candidates = session.exec(select(CustomModel).where(
            CustomModel.model_type == ModelType(role), CustomModel.is_active == True)).all()
        matches = [row for row in candidates if row.model_name.strip() == model
                   and row.base_url.rstrip('/') == getattr(settings, prefix + '_BASE_URL').rstrip('/')
                   and row.api_key == getattr(settings, prefix + '_API_KEY')]
        values[field] = matches[0].id if len(matches) == 1 else 'sys_' + role
    return values


def _decode(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('settings record must be an object')
    if 'schema_version' in data:
        if data['schema_version'] != 1 or not isinstance(data.get('values'), dict):
            raise ValueError('unsupported settings schema')
        revision = data.get('revision')
        if type(revision) is not int or revision < 1:
            raise ValueError('invalid settings revision')
        return dict(data['values']), revision, False
    return dict(data), 0, True


def _encode(values, revision):
    return json.dumps({'schema_version': 1, 'revision': revision, 'values': values},
                      ensure_ascii=False, sort_keys=True)


def _validate(values, session, model_fields=()):
    """Validate structure globally, but only edited model selections on writes.

    A missing unused VLM must not disable a healthy chat or prevent repairing
    that VLM selection. The actual consumer always resolves its own role.
    """
    checked = RuntimeValues.model_validate(values)
    from app.services.model_bindings import resolve_model
    for role in ('llm', 'embedding', 'vlm'):
        key = 'default_' + role + '_model'
        identity = getattr(checked, key)
        if key in model_fields and identity:
            resolve_model(session, role, identity, use_default=False)
    return checked


def _remember(session, checked, revision):
    from app.services.model_bindings import resolve_model, ModelBindingError
    warnings = []
    for role in ('llm', 'embedding', 'vlm'):
        identity = getattr(checked, 'default_' + role + '_model')
        if identity:
            try:
                resolve_model(session, role, identity, use_default=False)
            except ModelBindingError as exc:
                # A readable config with an invalid role binding is not a DB
                # outage. Preserve the ID and expose the role-specific warning.
                warnings.append(str(exc))
    snapshot = RuntimeSnapshot(**checked.model_dump(), revision=revision,
                               config_error='; '.join(warnings) or None)
    _last_good[_cache_key(session)] = snapshot
    # Compatibility consumers read the same loaded snapshot. New entry points
    # obtain an explicit immutable snapshot at the request/task boundary.
    settings = get_settings()
    for field, attr in SETTING_FIELDS.items():
        value = getattr(snapshot, field)
        setattr(settings, attr, value if value is not None else '')
    return snapshot


def _read(session):
    row = session.get(PersistedSystemSettings, 'system', populate_existing=True)
    if row is None:
        values = _seed_values(session)
        checked = _validate(values, session)
        row = PersistedSystemSettings(data=_encode(values, 1))
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = session.get(PersistedSystemSettings, 'system', populate_existing=True)
            if row is None:
                raise
        else:
            return _remember(session, checked, 1)
    raw = row.data
    values, revision, legacy = _decode(raw)
    missing = [field for field in SETTING_FIELDS if field not in values]
    if missing:
        seeds = _seed_values(session)
        for field in missing:
            values[field] = seeds[field]
    # An old explicit empty model string means no selection; preserve that intent.
    for role in ('llm', 'embedding', 'vlm'):
        field = 'default_' + role + '_model'
        if values[field] == '':
            values[field] = None
    checked = _validate(values, session)
    if legacy or missing:
        changed = session.exec(update(PersistedSystemSettings).where(
            PersistedSystemSettings.id == 'system', PersistedSystemSettings.data == raw
        ).values(data=_encode(values, revision + 1)))
        if changed.rowcount != 1:
            session.rollback()
            raise SettingsConflict('Settings changed during migration; reload settings')
        session.commit()
        revision += 1
    return _remember(session, checked, revision)


def get_runtime_settings(session=None):
    if session is None:
        from app.core.database import engine
        with Session(engine) as own_session:
            return get_runtime_settings(own_session)
    try:
        return _read(session)
    except Exception as exc:
        # Do not modify a corrupt record and do not manufacture an initial model.
        if isinstance(exc, SettingsConflict):
            raise
        previous = _last_good.get(_cache_key(session))
        if previous is not None:
            return replace(previous, degraded=True, config_error=type(exc).__name__)
        raise RuntimeSettingsUnavailable('Settings could not be loaded: ' + type(exc).__name__) from exc


def update_runtime_settings(session, changes, expected_revision):
    # Writes require a fresh valid DB record, never a degraded memory snapshot.
    current = _read(session)
    if expected_revision != current.revision:
        raise SettingsConflict('Settings were changed by another request; reload before saving')
    row = session.get(PersistedSystemSettings, 'system', populate_existing=True)
    raw = row.data
    values, revision, _ = _decode(raw)
    for field, value in changes.items():
        if field not in SETTING_FIELDS:
            raise ValueError('Unknown setting')
        values[field] = NUMERIC_DEFAULTS[field] if value is None and field in NUMERIC_DEFAULTS else value
    edited_models = {key for key in changes if key.startswith('default_')
                     and getattr(current, key) != values[key]}
    checked = _validate(values, session, model_fields=edited_models)
    changed = session.exec(update(PersistedSystemSettings).where(
        PersistedSystemSettings.id == 'system', PersistedSystemSettings.data == raw
    ).values(data=_encode(values, revision + 1)))
    if changed.rowcount != 1:
        session.rollback()
        raise SettingsConflict('Settings were changed by another request; reload before saving')
    session.commit()
    return _remember(session, checked, revision + 1)
