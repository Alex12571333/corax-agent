"""Corax Agent — minimal agent scaffold.

This package is intentionally a *scaffold*. It provides the runtime,
configuration, settings, registries and extension points that real
modules (LLM planners, connectors, memory backends, capabilities) will
plug into later — without changing this structure.

Nothing here talks to an LLM, Telegram, MCP, a shell or a real memory
store. Those arrive in later stages through the registries.
"""

__version__ = "0.1.0"
__agent_name__ = "corax"

__all__ = ["__version__", "__agent_name__"]
