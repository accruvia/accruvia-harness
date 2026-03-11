from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..domain import Project


@dataclass(slots=True)
class BrainSource:
    path: str
    kind: str
    summary: str = ""
    content: str = ""


@dataclass(slots=True)
class HeartbeatResult:
    project: dict[str, Any]
    adapter_name: str
    project_root: str
    run_dir: str
    context_path: str
    sources_path: str
    prompt_path: str | None = None
    response_path: str | None = None
    analysis_path: str | None = None
    llm_backend: str | None = None
    analysis: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    created_tasks: list[dict[str, Any]] = field(default_factory=list)
    skipped_tasks: list[dict[str, Any]] = field(default_factory=list)


class CognitionAdapter(Protocol):
    name: str

    def resolve_project_root(self, project: Project) -> Path: ...

    def list_brain_paths(self, project: Project, project_root: Path) -> list[Path]: ...

    def build_context(
        self,
        project: Project,
        project_root: Path,
        project_summary: dict[str, Any],
        context_packet: dict[str, Any],
        source_documents: list[BrainSource],
    ) -> dict[str, Any]: ...

    def build_prompt(self, project: Project, context: dict[str, Any]) -> str: ...

    def parse_response(self, response_text: str) -> dict[str, Any]: ...


class GenericCognitionAdapter:
    name = "generic"

    def resolve_project_root(self, project: Project) -> Path:
        return Path(".").resolve()

    def list_brain_paths(self, project: Project, project_root: Path) -> list[Path]:
        candidates = [project_root / "README.md", project_root / "docs", project_root / "specs"]
        paths: list[Path] = []
        for candidate in candidates:
            if candidate.is_file():
                paths.append(candidate)
            elif candidate.is_dir():
                paths.extend(sorted(path for path in candidate.rglob("*.md") if path.is_file()))
        return paths[:12]

    def build_context(
        self,
        project: Project,
        project_root: Path,
        project_summary: dict[str, Any],
        context_packet: dict[str, Any],
        source_documents: list[BrainSource],
    ) -> dict[str, Any]:
        return {
            "project": {
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "adapter_name": project.adapter_name,
                "project_root": str(project_root),
            },
            "project_summary": project_summary,
            "context_packet": context_packet,
            "brain_sources": [asdict(source) for source in source_documents],
        }

    def build_prompt(self, project: Project, context: dict[str, Any]) -> str:
        return "\n\n".join(
            [
                "You are performing a project heartbeat review.",
                "Use the provided project documents, task state, and telemetry to decide the next most important work.",
                "Issues/tasks must be atomic. Reject broad multi-objective work and split it into smaller tasks instead.",
                "Return strict JSON with keys: summary, priority_focus, issue_creation_needed, proposed_tasks.",
                "Each proposed_tasks item must include title, objective, priority, rationale.",
                "Optional proposed_tasks keys: split_of_task_id, strategy, validation_profile, allowed_paths, forbidden_paths.",
                json.dumps(context, indent=2, sort_keys=True),
            ]
        )

    def parse_response(self, response_text: str) -> dict[str, Any]:
        text = response_text.strip()
        if not text:
            return {
                "summary": "No heartbeat response returned.",
                "priority_focus": "",
                "issue_creation_needed": False,
                "proposed_tasks": [],
                "raw_response": response_text,
            }
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return {
                    "summary": str(payload.get("summary") or ""),
                    "priority_focus": str(payload.get("priority_focus") or ""),
                    "issue_creation_needed": bool(payload.get("issue_creation_needed", False)),
                    "proposed_tasks": list(payload.get("proposed_tasks") or []),
                    "raw_response": response_text,
                }
        except json.JSONDecodeError:
            pass
        issue_creation_needed = "create" in text.lower() and "issue" in text.lower()
        return {
            "summary": text.splitlines()[0].strip(),
            "priority_focus": "",
            "issue_creation_needed": issue_creation_needed,
            "proposed_tasks": [],
            "raw_response": response_text,
        }
