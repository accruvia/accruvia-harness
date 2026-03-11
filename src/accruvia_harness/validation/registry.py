from __future__ import annotations

import importlib

from .base import PromotionValidator
from .validators import validators_for_profile as builtin_validators_for_profile


class PromotionValidatorRegistry:
    def __init__(self) -> None:
        self._profile_factories: list = [builtin_validators_for_profile]

    def register_profile_factory(self, factory) -> None:
        self._profile_factories.append(factory)

    def validators_for_profile(self, profile: str) -> list[PromotionValidator]:
        validators: list[PromotionValidator] = []
        for factory in self._profile_factories:
            validators.extend(factory(profile))
        return validators


def build_validator_registry(module_names: tuple[str, ...] = ()) -> PromotionValidatorRegistry:
    registry = PromotionValidatorRegistry()
    for module_name in module_names:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_validators", None)
        if register is None:
            raise ValueError(f"Validator module '{module_name}' does not define register_validators(registry)")
        register(registry)
    return registry
