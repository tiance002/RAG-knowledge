"""Explicit, typed preference memory backed by a Redis hash."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.asyncio.retry import Retry

from app.core.config import get_settings

logger = logging.getLogger(__name__)
AnswerStyle = Literal['concise', 'normal', 'detailed']
KnowledgeLevel = Literal['beginner', 'intermediate', 'expert']
OutputPreference = Literal['step_by_step', 'table', 'code_example', 'principle_first']
FIELDS = ('answer_style', 'knowledge_level', 'output_preferences')


class PreferenceValues(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answer_style: AnswerStyle = 'normal'
    knowledge_level: KnowledgeLevel = 'intermediate'
    output_preferences: list[OutputPreference] = Field(default_factory=list)

    @field_validator('output_preferences')
    @classmethod
    def unique_outputs(cls, value):
        return list(dict.fromkeys(value))


class PreferenceChanges(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answer_style: AnswerStyle | None = None
    knowledge_level: KnowledgeLevel | None = None
    output_preferences: list[OutputPreference] | None = None

    @field_validator('answer_style', 'knowledge_level', 'output_preferences', mode='before')
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError('null is not a preference value')
        return value

    @field_validator('output_preferences')
    @classmethod
    def unique_outputs(cls, value):
        return list(dict.fromkeys(value)) if value is not None else value


class PreferencesSnapshot(BaseModel):
    values: PreferenceValues = Field(default_factory=PreferenceValues)
    sources: dict[str, Literal['default', 'explicit']] = Field(
        default_factory=lambda: {field: 'default' for field in FIELDS}
    )
    redis_fallback: bool = False


class PreferenceScope(BaseModel):
    tenant_id: str = 'default'
    user_id: str = 'default'
    kb_id: str | None = None

    @field_validator('tenant_id', 'user_id', 'kb_id')
    @classmethod
    def nonempty(cls, value):
        if value is not None and not value.strip():
            raise ValueError('scope identifier cannot be empty')
        return value


def get_preference_scope() -> PreferenceScope:
    """Personal identity boundary; replace with server-authenticated identity later."""
    return PreferenceScope()


def _key(scope: PreferenceScope | None) -> str:
    scope = scope or get_preference_scope()
    if scope.tenant_id == 'default' and scope.user_id == 'default' and scope.kb_id is None:
        return 'rag:memory:preferences:default'
    tenant = quote(scope.tenant_id, safe='')
    user = quote(scope.user_id, safe='')
    key = f'rag:memory:preferences:{tenant}:{user}'
    return f'{key}:kb:{quote(scope.kb_id, safe="")}' if scope.kb_id is not None else key


class MemoryUnavailable(RuntimeError):
    """A write or the subsequent read could not be confirmed."""

    def __init__(self, message: str, *, write_acknowledged: bool = False):
        super().__init__(message)
        self.write_acknowledged = write_acknowledged


# Lua makes read-modify-write of output tags a single Redis operation.
_OUTPUT_SCRIPT = '''
local key = KEYS[1]
local raw = redis.call('HGET', key, 'output_preferences')
local current = {}
if raw then
  local ok, decoded = pcall(cjson.decode, raw)
  if not ok or type(decoded) ~= 'table' or not string.match(raw, '^%s*%[') then
    return redis.error_reply('invalid stored output preferences')
  end
  local allowed = {step_by_step=true, table=true, code_example=true, principle_first=true}
  for k, v in pairs(decoded) do
    if type(k) ~= 'number' or type(v) ~= 'string' or not allowed[v] then
      return redis.error_reply('invalid stored output preferences')
    end
  end
  current = decoded
end
local add = cjson.decode(ARGV[1])
local remove = cjson.decode(ARGV[2])
local removed = {}
for _, v in ipairs(remove) do removed[v] = true end
local result, seen = {}, {}
for _, v in ipairs(current) do
  if not removed[v] and not seen[v] then table.insert(result, v); seen[v] = true end
end
for _, v in ipairs(add) do
  if not seen[v] then table.insert(result, v); seen[v] = true end
end
local encoded = '[]'
if #result > 0 then encoded = cjson.encode(result) end
redis.call('HSET', key, 'output_preferences', encoded, 'schema_version', '1', 'updated_at', ARGV[3])
return encoded
'''

_DELETE_SCRIPT = '''
redis.call('HDEL', KEYS[1], ARGV[1])
if redis.call('HEXISTS', KEYS[1], 'answer_style') == 0 and
   redis.call('HEXISTS', KEYS[1], 'knowledge_level') == 0 and
   redis.call('HEXISTS', KEYS[1], 'output_preferences') == 0 then
  redis.call('DEL', KEYS[1])
else
  redis.call('HSET', KEYS[1], 'updated_at', ARGV[2], 'schema_version', '1')
end
return 1
'''


def _updated_at():
    return datetime.now(timezone.utc).isoformat()


class MemoryService:
    def __init__(self, client=None, timeout: float = 0.7):
        self.timeout = timeout
        self.client = client
        if client is None:
            try:
                self.client = Redis.from_url(
                    get_settings().MEMORY_REDIS_URL or get_settings().REDIS_URL,
                    decode_responses=True, socket_connect_timeout=timeout,
                    socket_timeout=timeout, retry=Retry(NoBackoff(), 0), retry_on_timeout=False,
                )
            except Exception:
                logger.warning('Preference Redis client configuration unavailable')

    async def _call(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=self.timeout)

    async def get_preferences(self, scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        try:
            raw = await self._call(self.client.hgetall(_key(scope)))
        except Exception:
            logger.warning('Preference Redis read failed; using defaults')
            return PreferencesSnapshot(redis_fallback=True)
        values = PreferenceValues().model_dump()
        sources = {field: 'default' for field in FIELDS}
        for field in FIELDS:
            if field not in raw:
                continue
            try:
                value = json.loads(raw[field]) if field == 'output_preferences' else raw[field]
                validated = PreferenceChanges.model_validate({field: value})
                values[field] = getattr(validated, field)
                sources[field] = 'explicit'
            except (ValidationError, ValueError, TypeError):
                logger.warning('Invalid stored preference field %s; using default', field)
        return PreferencesSnapshot(values=PreferenceValues(**values), sources=sources)

    async def resolve_preferences(self, scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        return await self.get_preferences(scope)

    async def _snapshot_after_write(self, scope):
        snapshot = await self.get_preferences(scope)
        if snapshot.redis_fallback:
            raise MemoryUnavailable('Preference change was acknowledged but current values could not be read', write_acknowledged=True)
        return snapshot

    async def update_preference(self, changes: dict, scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        parsed = PreferenceChanges.model_validate(changes).model_dump(exclude_unset=True)
        if not parsed:
            raise ValueError('at least one preference is required')
        mapping = {k: json.dumps(v, ensure_ascii=False) if k == 'output_preferences' else v for k, v in parsed.items()}
        mapping['schema_version'] = '1'
        mapping['updated_at'] = _updated_at()
        try:
            await self._call(self.client.hset(_key(scope), mapping=mapping))
        except Exception as exc:
            logger.warning('Preference Redis write failed')
            raise MemoryUnavailable('Preference write could not be confirmed') from exc
        return await self._snapshot_after_write(scope)

    async def delete_preference(self, field: str, scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        if field not in FIELDS:
            raise ValueError('Unknown preference field')
        try:
            await self._call(self.client.eval(_DELETE_SCRIPT, 1, _key(scope), field, _updated_at()))
        except Exception as exc:
            logger.warning('Preference Redis delete failed')
            raise MemoryUnavailable('Preference deletion could not be confirmed') from exc
        return await self._snapshot_after_write(scope)

    async def reset_preferences(self, scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        try:
            await self._call(self.client.delete(_key(scope)))
        except Exception as exc:
            logger.warning('Preference Redis reset failed')
            raise MemoryUnavailable('Preference reset could not be confirmed') from exc
        return await self._snapshot_after_write(scope)

    async def change_output_preferences(self, *, add=(), remove=(), scope: PreferenceScope | None = None) -> PreferencesSnapshot:
        additions = PreferenceChanges(output_preferences=list(add)).output_preferences
        removals = PreferenceChanges(output_preferences=list(remove)).output_preferences
        if not additions and not removals:
            raise ValueError('at least one output preference is required')
        try:
            await self._call(self.client.eval(_OUTPUT_SCRIPT, 1, _key(scope), json.dumps(additions), json.dumps(removals), _updated_at()))
        except Exception as exc:
            logger.warning('Preference Redis output update failed')
            raise MemoryUnavailable('Preference write could not be confirmed') from exc
        return await self._snapshot_after_write(scope)


@lru_cache(maxsize=1)
def get_memory_service() -> MemoryService:
    return MemoryService()
