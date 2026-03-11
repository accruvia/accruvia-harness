from __future__ import annotations

import importlib

from .base import WorkloadAdapter
from .builtin import builtin_adapters


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, WorkloadAdapter] = {}

    def register(self, adapter: WorkloadAdapter) -> None:
        self._adapters[adapter.profile] = adapter

    def get(self, profile: str) -> WorkloadAdapter:
        adapter = self._adapters.get(profile)
        if adapter is not None:
            return adapter
        generic = self._adapters.get("generic")
        if generic is None:
            raise ValueError(f"No adapter registered for profile '{profile}' and no generic fallback exists")
        return generic

    def profiles(self) -> list[str]:
        return sorted(self._adapters)


def build_adapter_registry(module_names: tuple[str, ...] = ()) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in builtin_adapters():
        registry.register(adapter)
    for module_name in module_names:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_adapters", None)
        if register is None:
            raise ValueError(f"Adapter module '{module_name}' does not define register_adapters(registry)")
        register(registry)
    return registry
