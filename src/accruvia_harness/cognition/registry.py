from __future__ import annotations

import importlib

from .base import CognitionAdapter, GenericCognitionAdapter


class CognitionAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, CognitionAdapter] = {}

    def register(self, adapter: CognitionAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> CognitionAdapter:
        adapter = self._adapters.get(name)
        if adapter is not None:
            return adapter
        generic = self._adapters.get("generic")
        if generic is None:
            raise ValueError(f"No cognition adapter registered for '{name}' and no generic fallback exists")
        return generic

    def names(self) -> list[str]:
        return sorted(self._adapters)


def build_cognition_registry(module_names: tuple[str, ...] = ()) -> CognitionAdapterRegistry:
    registry = CognitionAdapterRegistry()
    registry.register(GenericCognitionAdapter())
    for module_name in module_names:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_cognition_adapters", None)
        if register is None:
            raise ValueError(
                f"Cognition adapter module '{module_name}' does not define register_cognition_adapters(registry)"
            )
        register(registry)
    return registry
