"""Public core exports, loaded lazily to keep storage imports independent."""
from importlib import import_module

_EXPORTS = {'ConfigManager': '.base', 'ConfigurationError': '.base', 'InitializationError': '.base', 'LivingMemoryException': '.base', 'ProviderNotReadyError': '.base', 'ConversationManager': '.managers', 'GraphMemoryManager': '.managers', 'MemoryEngine': '.managers', 'ExtractedGraph': '.models', 'GraphEdge': '.models', 'GraphEntry': '.models', 'GraphNode': '.models', 'MemoryEvent': '.models', 'Message': '.models', 'Session': '.models', 'ChatroomContextParser': '.processors', 'EntityResolver': '.processors', 'GraphExtractor': '.processors', 'MemoryProcessor': '.processors', 'TextProcessor': '.processors', 'IndexValidator': '.validators'}
__all__ = list(_EXPORTS)

def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
