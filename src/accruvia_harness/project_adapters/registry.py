from __future__ import annotations

import importlib

from .base import ProjectAdapter
from .builtin import builtin_project_adapters


class ProjectAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProjectAdapter] = {}

    def register(self, adapter: ProjectAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ProjectAdapter:
        adapter = self._adapters.get(name)
        if adapter is not None:
            return adapter
        generic = self._adapters.get("generic")
        if generic is None:
            raise ValueError(f"No project adapter registered for '{name}' and no generic fallback exists")
        return generic

    def names(self) -> list[str]:
        return sorted(self._adapters)


def build_project_adapter_registry(module_names: tuple[str, ...] = ()) -> ProjectAdapterRegistry:
    registry = ProjectAdapterRegistry()
    for adapter in builtin_project_adapters():
        registry.register(adapter)
    for module_name in module_names:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_project_adapters", None)
        if register is None:
            raise ValueError(
                f"Project adapter module '{module_name}' does not define register_project_adapters(registry)"
            )
        register(registry)
    return registry
