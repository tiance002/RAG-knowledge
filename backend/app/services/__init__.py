"""
服务模块导出
"""

from importlib import import_module

_EXPORTS = {'FileParser': 'parser', 'TextSplitter': 'parser', 'ParsedChunk': 'parser',
            'VLMService': 'vlm', 'EmbeddingService': 'embedding',
            'VectorStoreService': 'vector_store', 'LLMService': 'llm', 'ChatService': 'chat'}


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(import_module('app.services.' + _EXPORTS[name]), name)
    raise AttributeError(name)

__all__ = [
    "FileParser",
    "TextSplitter",
    "ParsedChunk",
    "VLMService",
    "EmbeddingService",
    "VectorStoreService",
    "LLMService",
    "ChatService",
]
