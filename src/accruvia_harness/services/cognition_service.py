from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..cognition import BrainSource, CognitionAdapterRegistry, HeartbeatResult
from ..domain import Event, Project, Run, RunStatus, Task, TaskStatus, new_id, serialize_dataclass
from ..llm import LLMInvocation, LLMRouter


class CognitionService:
    _MAX_SOURCE_COUNT = 10
    _MAX_SOURCE_BYTES = 3000
    _MAX_TOTAL_SOURCE_BYTES = 12000

    def __init__(
        self,
        store,
        query_service,
        workspace_root: Path,
        cognition_registry: CognitionAdapterRegistry,
        task_service=None,
        llm_router: LLMRouter | None = None,
        telemetry=None,
    ) -> None:
        self.store = store
        self.query_service = query_service
        self.workspace_root = workspace_root
        self.cognition_registry = cognition_registry
        self.task_service = task_service
        self.llm_router = llm_router
        self.telemetry = telemetry

    def heartbeat(self, project_id: str) -> HeartbeatResult:
        project = self.store.get_project(project_id)
        if project is None:
            raise ValueError(f"Unknown project: {project_id}")
        adapter = self.cognition_registry.get(project.adapter_name)
        project_root = adapter.resolve_project_root(project)
        run_dir = self.workspace_root / "cognition" / project.id / new_id("heartbeat")
        run_dir.mkdir(parents=True, exist_ok=True)
        sources = self._load_sources(adapter.list_brain_paths(project, project_root))
        project_summary = self.query_service.project_summary(project.id)
        context_packet = self.query_service.context_packet(project.id)
        context = adapter.build_context(project, project_root, project_summary, context_packet, sources)
        context_path = run_dir / "heartbeat_context.json"
        context_path.write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
        sources_path = run_dir / "brain_sources.json"
        sources_path.write_text(
            json.dumps([asdict(source) for source in sources], indent=2, sort_keys=True),
            encoding="utf-8",
        )

        prompt_path: Path | None = None
        response_path: Path | None = None
        analysis_path: Path | None = None
        llm_backend: str | None = None
        analysis: dict[str, object]
        if self.llm_router is None:
            analysis = {
                "summary": "No LLM router configured for heartbeat analysis.",
                "priority_focus": "",
                "issue_creation_needed": False,
                "proposed_tasks": [],
            }
        else:
            prompt = adapter.build_prompt(project, context)
            task = Task(
                id=new_id("heartbeat_task"),
                project_id=project.id,
                title=f"Heartbeat for {project.name}",
                objective="Analyze project objectives, documents, and work backlog.",
                strategy="heartbeat",
                status=TaskStatus.COMPLETED,
            )
            run = Run(
                id=new_id("heartbeat_run"),
                task_id=task.id,
                status=RunStatus.COMPLETED,
                attempt=1,
                summary=f"Heartbeat analysis for {project.name}",
            )
            if self.telemetry is not None:
                with self.telemetry.timed("heartbeat_analysis", project_id=project.id, adapter_name=adapter.name):
                    result, llm_backend = self.llm_router.execute(
                        LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir),
                        telemetry=self.telemetry,
                    )
            else:
                result, llm_backend = self.llm_router.execute(
                    LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir),
                    telemetry=self.telemetry,
                )
            prompt_path = result.prompt_path
            response_path = result.response_path
            analysis = adapter.parse_response(result.response_text)
            analysis_path = run_dir / "heartbeat_analysis.json"
            analysis_path.write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")

        created_tasks, skipped_tasks = self._materialize_proposed_tasks(project, analysis)

        self.store.create_event(
            Event(
                id=new_id("event"),
                entity_type="project",
                entity_id=project.id,
                event_type="heartbeat_completed",
                payload={
                    "adapter_name": adapter.name,
                    "project_root": str(project_root),
                    "run_dir": str(run_dir),
                    "issue_creation_needed": bool(analysis.get("issue_creation_needed", False)),
                    "proposed_task_count": len(list(analysis.get("proposed_tasks") or [])),
                    "created_task_count": len(created_tasks),
                    "skipped_task_count": len(skipped_tasks),
                    "summary": str(analysis.get("summary") or ""),
                },
            )
        )
        return HeartbeatResult(
            project=serialize_dataclass(project),
            adapter_name=adapter.name,
            project_root=str(project_root),
            run_dir=str(run_dir),
            context_path=str(context_path),
            sources_path=str(sources_path),
            prompt_path=str(prompt_path) if prompt_path is not None else None,
            response_path=str(response_path) if response_path is not None else None,
            analysis_path=str(analysis_path) if analysis_path is not None else None,
            llm_backend=llm_backend,
            analysis=analysis,
            context=context,
            sources=[asdict(source) for source in sources],
            created_tasks=created_tasks,
            skipped_tasks=skipped_tasks,
        )

    def _materialize_proposed_tasks(self, project: Project, analysis: dict[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if self.task_service is None:
            return [], []
        proposals = analysis.get("proposed_tasks") or []
        if not isinstance(proposals, list):
            return [], []
        existing = self.store.list_tasks(project.id)
        created: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for item in proposals:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            objective = str(item.get("objective") or "").strip()
            if not title or not objective:
                skipped.append({"reason": "invalid_proposal", "proposal": item})
                continue
            parent_task_id = item.get("split_of_task_id")
            dedupe_match = next(
                (
                    task for task in existing
                    if task.title == title
                    and task.objective == objective
                    and task.parent_task_id == parent_task_id
                    and task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE, TaskStatus.COMPLETED}
                ),
                None,
            )
            if dedupe_match is not None:
                skipped.append(
                    {
                        "reason": "duplicate_task",
                        "task_id": dedupe_match.id,
                        "title": dedupe_match.title,
                    }
                )
                continue
            scope: dict[str, object] = {}
            if isinstance(item.get("allowed_paths"), list):
                scope["allowed_paths"] = [str(path) for path in item.get("allowed_paths") if path]
            if isinstance(item.get("forbidden_paths"), list):
                scope["forbidden_paths"] = [str(path) for path in item.get("forbidden_paths") if path]
            priority = self._parse_priority(item.get("priority", 100))
            created_task = self.task_service.create_task_with_policy(
                project_id=project.id,
                title=title,
                objective=objective,
                priority=priority,
                parent_task_id=str(parent_task_id) if parent_task_id else None,
                source_run_id=None,
                external_ref_type=None,
                external_ref_id=None,
                validation_profile=str(item.get("validation_profile") or "generic"),
                scope=scope,
                strategy=str(item.get("strategy") or "heartbeat"),
                max_attempts=int(item.get("max_attempts", 3)),
                max_branches=int(item.get("max_branches", 1)),
                required_artifacts=list(item.get("required_artifacts") or ["plan", "report"]),
            )
            existing.append(created_task)
            self.store.create_event(
                Event(
                    id=new_id("event"),
                    entity_type="task",
                    entity_id=created_task.id,
                    event_type="heartbeat_task_created",
                    payload={
                        "project_id": project.id,
                        "source": "heartbeat",
                        "split_of_task_id": parent_task_id,
                    },
                )
            )
            created.append(serialize_dataclass(created_task))
        return created, skipped

    @staticmethod
    def _parse_priority(value: object) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return 100
        if text.isdigit():
            return int(text)
        normalized = text.upper()
        if normalized in {"P0", "CRITICAL"}:
            return 1000
        if normalized in {"P1", "HIGH"}:
            return 700
        if normalized in {"P2", "MEDIUM"}:
            return 400
        if normalized in {"P3", "LOW"}:
            return 200
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            return int(digits)
        return 100

    def _load_sources(self, paths: list[Path]) -> list[BrainSource]:
        sources: list[BrainSource] = []
        remaining_budget = self._MAX_TOTAL_SOURCE_BYTES
        for path in paths[: self._MAX_SOURCE_COUNT]:
            if not path.exists() or not path.is_file():
                continue
            kind = path.suffix.lstrip(".") or "text"
            if remaining_budget <= 0:
                break
            per_file_budget = min(self._MAX_SOURCE_BYTES, remaining_budget)
            content = path.read_text(encoding="utf-8", errors="ignore")[:per_file_budget]
            remaining_budget -= len(content.encode("utf-8"))
            summary = content.splitlines()[0].strip() if content else path.name
            sources.append(BrainSource(path=str(path), kind=kind, summary=summary[:240], content=content))
        return sources
