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
                "You are the global project brain for a software project managed by Accruvia Harness.",
                "Your job is to assess the entire project, decide whether meaningful work is needed, and create only tasks that directly further the goal of the software.",
                "Every proposed task must directly support the purpose of the software. Reject feature creep, novelty work, local churn, and product drift.",
                "Assess the whole project, not just the latest failure or the current dirty state.",
                "Review the project through these lenses: product direction, user value, customer onboarding, UX and operator ergonomics, developer experience, reliability, recovery and graceful degradation, security, privacy and data handling, observability and telemetry quality, QA depth, test suite runtime and efficiency, test determinism and flake resistance, appropriate code coverage, architecture coherence, refactoring opportunities, tech debt and entropy control, consistency of code patterns and abstractions, performance and latency, cost efficiency, resource usage, failure handling and retries, workflow correctness, state-model integrity, backlog quality, task granularity, idempotency, documentation accuracy, operational runbooks, release safety, CI health and signal quality, dependency health, upgrade risk, integration boundary safety, optional-feature isolation, data model migration safety, multi-provider resilience, prompt and policy drift, self-hosting safety, branch and worktree hygiene, promotion and review pipeline quality, auditability and traceability, and feature creep.",
                "Actively look for semantically equivalent behavior implemented through different code paths, wrappers, prompts, policies, or resource settings. Treat 'same intent, different execution path' as a reliability smell, because drift between those paths causes regressions.",
                "Do not treat failed, blocked, stale, or crash-looping tasks as adequate backlog coverage. If a dominant unresolved problem does not have a currently runnable path to progress, propose a fresh bounded task that restores forward motion.",
                "Use the provided project documents, task state, telemetry, loop history, and recent execution outcomes to decide whether work is actually justified.",
                "It is desirable to create zero tasks when there is genuinely no high- or medium-value work.",
                "There is no fixed cap on the number of tasks. Create as many tasks as are genuinely justified.",
                "However, task volume must not overwhelm the timeliness and usefulness of future strategy loops. Choose task quantity so the project can still be reviewed again at appropriate intervals.",
                "Use project velocity, backlog size, retry rate, loop cost, completion rate, queue behavior, and strategy overhead to judge the appropriate interval and the right amount of work to create.",
                "Prefer durable, broadly useful work over narrow local optimizations.",
                "Tasks must be atomic, concrete, and single-objective. Split broad multi-objective work into smaller tasks.",
                "Return strict JSON with keys: summary, priority_focus, issue_creation_needed, proposed_tasks, risks.",
                "Each proposed_tasks item must include title, objective, priority, rationale.",
                "Optional proposed_tasks keys: split_of_task_id, strategy, validation_profile, allowed_paths, forbidden_paths.",
                "Optional top-level key: next_heartbeat_seconds. Use it to recommend when the next strategy loop should run.",
                "Priority must be one of P0, P1, P2, P3, or a numeric priority.",
                "Set issue_creation_needed to false when no worthwhile work is justified. If false, proposed_tasks should be empty.",
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
                    "risks": list(payload.get("risks") or []),
                    "next_heartbeat_seconds": self._parse_next_heartbeat_seconds(payload.get("next_heartbeat_seconds")),
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
            "risks": [],
            "next_heartbeat_seconds": None,
            "raw_response": response_text,
        }

    @staticmethod
    def _parse_next_heartbeat_seconds(value: object) -> int | None:
        if value in (None, "", False):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
