"""User preference management. Identity is determined on the server."""
from fastapi import APIRouter, Depends, HTTPException
from app.services.memory import (
    FIELDS, MemoryService, MemoryUnavailable, PreferenceChanges, PreferenceScope,
    PreferencesSnapshot, get_memory_service, get_preference_scope,
)

router = APIRouter(prefix='/api/memory/preferences', tags=['memory'])


@router.get('', response_model=PreferencesSnapshot)
async def read_preferences(service: MemoryService = Depends(get_memory_service), scope: PreferenceScope = Depends(get_preference_scope)):
    return await service.get_preferences(scope)


@router.put('', response_model=PreferencesSnapshot)
async def update_preferences(changes: PreferenceChanges, service: MemoryService = Depends(get_memory_service), scope: PreferenceScope = Depends(get_preference_scope)):
    values = changes.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail='At least one preference is required')
    try:
        return await service.update_preference(values, scope)
    except MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete('/{field}', response_model=PreferencesSnapshot)
async def delete_preference(field: str, service: MemoryService = Depends(get_memory_service), scope: PreferenceScope = Depends(get_preference_scope)):
    if field not in FIELDS:
        raise HTTPException(status_code=422, detail='Unknown preference field')
    try:
        return await service.delete_preference(field, scope)
    except MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete('', response_model=PreferencesSnapshot)
async def reset_preferences(service: MemoryService = Depends(get_memory_service), scope: PreferenceScope = Depends(get_preference_scope)):
    try:
        return await service.reset_preferences(scope)
    except MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
