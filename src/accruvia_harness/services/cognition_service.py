from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..cognition import BrainSource, CognitionAdapterRegistry, HeartbeatResult
from ..domain import Event, Project, Run, RunStatus, Task, TaskStatus, new_id, serialize_dataclass
from ..llm import LLMInvocation, LLMRouter


class CognitionService:
    def __init__(
        self,
        store,
        query_service,
        workspace_root: Path,
        cognition_registry: CognitionAdapterRegistry,
        llm_router: LLMRouter | None = None,
        telemetry=None,
    ) -> None:
        self.store = store
        self.query_service = query_service
        self.workspace_root = workspace_root
        self.cognition_registry = cognition_registry
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
            executor, llm_backend = self.llm_router.resolve()
            if self.telemetry is not None:
                with self.telemetry.timed("heartbeat_analysis", project_id=project.id, adapter_name=adapter.name):
                    result = executor.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
            else:
                result = executor.execute(LLMInvocation(task=task, run=run, prompt=prompt, run_dir=run_dir))
            prompt_path = result.prompt_path
            response_path = result.response_path
            analysis = adapter.parse_response(result.response_text)
            analysis_path = run_dir / "heartbeat_analysis.json"
            analysis_path.write_text(json.dumps(analysis, indent=2, sort_keys=True), encoding="utf-8")

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
        )

    def _load_sources(self, paths: list[Path]) -> list[BrainSource]:
        sources: list[BrainSource] = []
        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            kind = path.suffix.lstrip(".") or "text"
            content = path.read_text(encoding="utf-8", errors="ignore")[:12000]
            summary = content.splitlines()[0].strip() if content else path.name
            sources.append(BrainSource(path=str(path), kind=kind, summary=summary[:240], content=content))
        return sources
