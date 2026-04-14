"""SkillContext: injectable context providers for skills that need repo grounding.

Problem this solves: skills that produce output referencing repo contents
(file paths, symbol names, existing classes/functions) cannot be trusted
when the LLM extrapolates from training-data priors without knowing what
files actually exist in the target repo. This was the hallucination-in-
plan_draft_trio issue — the skill produced structurally-valid but
semantically-fictional target_impl paths.

Design: the caller constructs a SkillContext once per harness session and
passes it to skills that need it, at skill CONSTRUCTION time (not per
invocation). Skills that need context take a `context: SkillContext` in
their `__init__`. Skills that don't need context ignore it entirely and
their protocol is unchanged.

    context = build_default_skill_context(workspace_root)
    registry = build_default_registry(skill_context=context)
    # internally: registry.register(PlanDraftTrioSkill(context=context))

The `RepoInventoryProvider` is the first real provider. It exposes:
    - list of files in the repo (git ls-files, filtered to source+tests)
    - map of file path -> set of symbols defined in that file (Python AST)
    - file_exists() predicate for validation
    - get_prompt_block() formatter for inclusion in LLM prompts

Caching is per-provider-instance so repeated calls within a skill
invocation don't re-walk the filesystem.
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path


_SOURCE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".md", ".yml", ".yaml", ".toml", ".sql",
)
_MAX_PROMPT_FILES = 400  # cap for the prompt block to keep prompt budget bounded


class RepoInventoryProvider:
    """Git-backed inventory of files + Python symbols in the target repo.

    All reads are lazy + cached. Instantiate once per session; skills
    share the cache across invocations.
    """

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self._files: set[str] | None = None
        self._symbols_cache: dict[str, set[str]] = {}

    @property
    def files(self) -> set[str]:
        """Return the set of source/test files in the repo as relative paths.

        Uses `git ls-files` filtered by extension. Paths are relative to
        repo_root. Cached after first call.
        """
        if self._files is not None:
            return self._files
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            all_files = result.stdout.splitlines()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            all_files = []
        self._files = {
            f.strip()
            for f in all_files
            if f.strip() and f.strip().endswith(_SOURCE_EXTENSIONS)
        }
        return self._files

    def file_exists(self, path: str) -> bool:
        """Return True if `path` is a tracked file in the repo.

        Accepts both repo-relative (`src/foo/bar.py`) and absolute paths.
        Compares normalized repo-relative form.
        """
        if not path:
            return False
        normalized = path.strip().lstrip("./")
        return normalized in self.files

    def symbols_in_file(self, path: str) -> set[str]:
        """Return the set of top-level class + function names defined in `path`.

        Only applies to Python files. Returns an empty set for non-Python
        files or files that cannot be parsed.
        """
        if not path.endswith(".py"):
            return set()
        if path in self._symbols_cache:
            return self._symbols_cache[path]
        abs_path = self.repo_root / path
        if not abs_path.is_file():
            self._symbols_cache[path] = set()
            return self._symbols_cache[path]
        try:
            tree = ast.parse(abs_path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            self._symbols_cache[path] = set()
            return self._symbols_cache[path]
        symbols: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.add(node.name)
        self._symbols_cache[path] = symbols
        return symbols

    def symbol_exists(self, path: str, symbol: str) -> bool:
        """Return True if `symbol` is defined at the top level of `path`."""
        return symbol in self.symbols_in_file(path)

    def get_prompt_block(self, focus_prefixes: tuple[str, ...] = ()) -> str:
        """Render a file listing + symbol inventory for inclusion in an LLM prompt.

        `focus_prefixes` optionally narrows the listing (e.g. ("src/", "tests/"))
        to keep prompt token cost bounded when the repo is large. Capped at
        _MAX_PROMPT_FILES entries total.
        """
        files = sorted(self.files)
        if focus_prefixes:
            files = [f for f in files if any(f.startswith(p) for p in focus_prefixes)]
        truncated = files[:_MAX_PROMPT_FILES]
        more_count = max(0, len(files) - len(truncated))

        lines: list[str] = ["REPOSITORY INVENTORY (existing files):"]
        for f in truncated:
            symbols = self.symbols_in_file(f) if f.endswith(".py") else set()
            if symbols:
                sym_list = ", ".join(sorted(symbols)[:12])
                more = f" (+{len(symbols) - 12} more)" if len(symbols) > 12 else ""
                lines.append(f"  {f}   [{sym_list}{more}]")
            else:
                lines.append(f"  {f}")
        if more_count:
            lines.append(f"  ... and {more_count} more files (truncated for prompt budget)")
        return "\n".join(lines)

    @cached_property
    def impl_root_candidates(self) -> tuple[str, ...]:
        """Plausible roots for new implementation files, inferred from existing layout."""
        candidates: list[str] = []
        if any(f.startswith("src/accruvia_harness/") for f in self.files):
            candidates.append("src/accruvia_harness/")
        elif any(f.startswith("src/") for f in self.files):
            candidates.append("src/")
        if any(f.startswith("frontend/src/") for f in self.files):
            candidates.append("frontend/src/")
        return tuple(candidates) if candidates else ("src/",)

    @cached_property
    def test_root_candidates(self) -> tuple[str, ...]:
        candidates: list[str] = []
        if any(f.startswith("tests/") for f in self.files):
            candidates.append("tests/")
        if any(f.startswith("frontend/tests/") for f in self.files):
            candidates.append("frontend/tests/")
        return tuple(candidates) if candidates else ("tests/",)

    def path_matches_impl_convention(self, path: str) -> bool:
        """True if `path` looks like a valid new implementation file location."""
        return any(path.startswith(root) for root in self.impl_root_candidates)

    def path_matches_test_convention(self, path: str) -> bool:
        """True if `path` looks like a valid new test file location."""
        return any(path.startswith(root) for root in self.test_root_candidates)


@dataclass(slots=True)
class SkillContext:
    """Bag of providers skills can consume.

    Passed to skill constructors at default-registry build time. Skills
    that don't need context simply don't accept it in `__init__` and
    remain context-agnostic.
    """

    repo: RepoInventoryProvider


def build_default_skill_context(repo_root: Path | str) -> SkillContext:
    """Construct the default SkillContext for a given repo root.

    The repo_root is typically the project's git working tree. For the
    harness itself, this is the accruvia-harness repo root; for other
    projects it is the project_adapter's project workspace.
    """
    return SkillContext(repo=RepoInventoryProvider(Path(repo_root)))
