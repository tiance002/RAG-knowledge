"""Pinned Chroma HTTP bootstrap with exact ownership, used only by P2 reads."""
import importlib.metadata
import threading

import chromadb
import httpx
from chromadb.config import Settings as ChromaSettings
from app.services.bounded_chroma_executor import ChromaExecutorCapabilityError

_hook_lock = threading.Lock()


class OwnedChromaHTTP:
    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        # Fresh identity is never passed outside this constructor.
        self.settings = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
        self.systems = {}
        self.sessions = {}
        self.client = None
        self.cleanup_failed = False

    def _capture(self, identifier, shared):
        system = shared._identifier_to_system.get(identifier)
        if system is not None and system.settings is self.settings:
            self.systems[id(system)] = system
        return system

    def create(self):
        from chromadb.api import ServerAPI
        from chromadb.api.fastapi import FastAPI
        from chromadb.api.shared_system_client import SharedSystemClient
        if importlib.metadata.version('chromadb') != '1.3.7':
            raise ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_HTTP_TIMEOUT_ADAPTER_VERSION')
        if self.cleanup_failed:
            raise ChromaExecutorCapabilityError('CHROMA_RESOURCE_CLEANUP_FAILED')
        try:
            with _hook_lock:
                descriptor = SharedSystemClient.__dict__.get('_create_system_if_not_exists')
                if not isinstance(descriptor, classmethod):
                    raise ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_SYSTEM_CREATION_SHAPE')
                original = SharedSystemClient._create_system_if_not_exists
                original_init = FastAPI.__dict__.get('__init__')
                if not callable(original_init):
                    raise ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_HTTP_CONSTRUCTOR_SHAPE')
                def initialize(api, system):
                    try:
                        original_init(api, system)
                    finally:
                        if system.settings is self.settings:
                            session = getattr(api, '_session', None)
                            if session is not None:
                                self.sessions[id(session)] = (session, system)
                def create(cls, identifier, selected_settings):
                    try:
                        system = original(identifier, selected_settings)
                    finally:
                        if selected_settings is self.settings:
                            self._capture(identifier, SharedSystemClient)
                    if selected_settings is self.settings:
                        if system.settings is not self.settings:
                            raise ChromaExecutorCapabilityError('CHROMA_SETTINGS_IDENTITY_MISMATCH')
                        api = system.instance(ServerAPI)
                        session = getattr(api, '_session', None)
                        if type(api) is not FastAPI or type(session) is not httpx.Client:
                            raise ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_HTTP_SESSION_SHAPE')
                        session.timeout = httpx.Timeout(self.timeout)
                    return system
                installed = classmethod(create)
                SharedSystemClient._create_system_if_not_exists = installed
                FastAPI.__init__ = initialize
                try:
                    self.client = chromadb.HttpClient(host=self.host, port=self.port, settings=self.settings)
                    # Verify both sessions actually passed through the exact-settings hook.
                    sessions = [getattr(getattr(self.client, '_server', None), '_session', None),
                                getattr(getattr(getattr(self.client, '_admin_client', None), '_server', None), '_session', None)]
                    owned_sessions = [getattr(system._instances.get(FastAPI), '_session', None) for system in self.systems.values()]
                    if len(self.systems) < 2 or any(type(session) is not httpx.Client or
                            not any(session is own for own in owned_sessions) for session in sessions):
                        raise ChromaExecutorCapabilityError('CHROMA_BOOTSTRAP_OWNERSHIP_UNVERIFIED')
                finally:
                    changed = False
                    if FastAPI.__dict__.get('__init__') is initialize:
                        FastAPI.__init__ = original_init
                    else:
                        changed = True
                    if SharedSystemClient.__dict__.get('_create_system_if_not_exists') is installed:
                        SharedSystemClient._create_system_if_not_exists = descriptor
                    else:
                        changed = True
                    if changed:
                        raise ChromaExecutorCapabilityError('CHROMA_BOOTSTRAP_HOOK_CHANGED')
        except BaseException:
            try:
                self.close()
            except Exception as exc:
                self.cleanup_failed = True
                raise ChromaExecutorCapabilityError('CHROMA_RESOURCE_CLEANUP_FAILED') from exc
            raise
        return self.client

    def close(self):
        from chromadb.api.fastapi import FastAPI
        from chromadb.api.shared_system_client import SharedSystemClient
        failures = []
        failed_systems = set()
        for identity, (session, system) in list(self.sessions.items()):
            if system.settings is not self.settings:
                failures.append('identity')
                failed_systems.add(id(system))
                continue
            try:
                if type(session) is not httpx.Client:
                    raise ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_HTTP_SESSION_SHAPE')
                session.close()
            except Exception as exc:
                failures.append(type(exc).__name__)
                failed_systems.add(id(system))
                continue
            self.sessions.pop(identity, None)
        for identity, system in list(self.systems.items()):
            if system.settings is not self.settings or identity in failed_systems:
                failures.append('identity_or_session')
                continue
            try:
                system.stop()
            except Exception as exc:
                failures.append(type(exc).__name__)
                continue
            # Remove aliases only while their value is exactly our owned system.
            cache = SharedSystemClient._identifier_to_system
            for key, value in list(cache.items()):
                if value is system and cache.get(key) is system:
                    del cache[key]
            self.systems.pop(identity, None)
        if failures:
            self.cleanup_failed = True
            raise ChromaExecutorCapabilityError('CHROMA_RESOURCE_CLEANUP_FAILED')
        self.client = None
        self.cleanup_failed = False
