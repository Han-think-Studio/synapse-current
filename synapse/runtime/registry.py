"""Explicit registry for built-in runtime adapters.

Core code may request a built-in compatibility adapter by provider name, while
new providers can implement the RuntimeAdapter contract without changing core.
"""

from __future__ import annotations

from collections.abc import Callable

from synapse.runtime.antigravity import AntigravityAdapter
from synapse.runtime.cli import ClaudeCodeAdapter
from synapse.runtime.contracts import RuntimeAdapter
from synapse.runtime.lmstudio import LMStudioAdapter
from synapse.runtime.ollama import OllamaAdapter

AdapterFactory = Callable[[], RuntimeAdapter]

BUILTIN_ADAPTERS: dict[str, AdapterFactory] = {
    "lmstudio": LMStudioAdapter,
    "ollama": OllamaAdapter,
    "claude_code": ClaudeCodeAdapter,
    "antigravity": AntigravityAdapter,
}


def get_builtin_adapter(provider: str, *, base_url: str | None = None) -> RuntimeAdapter | None:
    """Return an explicit compatibility adapter for a known local provider.

    base_url is passed through to the adapter, which validates it against its
    own endpoint policy. Adapter construction stays owned here so a caller never
    grows a second way to build one.
    """

    factory = BUILTIN_ADAPTERS.get(provider.strip().lower())
    if factory is None:
        return None
    return factory() if base_url is None else factory(base_url)


__all__ = ["BUILTIN_ADAPTERS", "get_builtin_adapter"]
