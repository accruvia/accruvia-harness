"""Skill registry for lookup and iteration."""
from __future__ import annotations

from typing import Iterator

from .base import Skill


class SkillRegistry:
    """Simple name -> Skill registry.

    Populated at harness startup. Services look up skills by name rather
    than importing concrete implementations directly.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            available = ", ".join(sorted(self._skills))
            raise KeyError(f"Unknown skill: {name}. Available: {available}")
        return self._skills[name]

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def __iter__(self) -> Iterator[Skill]:
        yield from self._skills.values()

    def __len__(self) -> int:
        return len(self._skills)
