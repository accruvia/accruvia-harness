"""HarnessUIDataService atomic generation methods."""
from __future__ import annotations

import datetime as _dt
import time
from pathlib import Path
from typing import Any

from ..domain import (
    ContextRecord, MermaidStatus, Objective, ObjectivePhase,
    ObjectiveStatus, Run, Task, TaskStatus, new_id,
)
from ._shared import _ATOMIC_GENERATION, _BACKGROUND_SUPERVISOR

class AtomicGenerationMixin:

    def queue_atomic_generation(self, objective_id: str, *, async_mode: bool = True, runner: "ObjectiveLifecycleRunner | None" = None) -> dict[str, object]:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective: {objective_id}")
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            raise ValueError("Atomic generation requires a finished Mermaid.")
        current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and self._atomic_generation_is_stale(current, objective_id):
            self._mark_atomic_generation_interrupted(objective, current)
            current = self._atomic_generation_state(objective_id)
        if current["status"] == "running" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        if current["status"] == "completed" and int(current.get("diagram_version") or 0) == mermaid.version:
            return {"atomic_generation": current}
        generation_id = new_id("atomic_generation")
        start_record = ContextRecord(
            id=new_id("context"),
            record_type="atomic_generation_started",
            project_id=objective.project_id,
            objective_id=objective.id,
            visibility="operator_visible",
            author_type="system",
            content=f"Started generating atomic units from Mermaid v{mermaid.version}.",
            metadata={"generation_id": generation_id, "diagram_version": mermaid.version},
        )
        self.store.create_context_record(start_record)
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Generating atomic units from accepted flowchart v{mermaid.version}.",
                metadata={"kind": "atomic_generation", "status": "started", "generation_id": generation_id, "diagram_version": mermaid.version},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "started",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": f"Started atomic generation from Mermaid v{mermaid.version}.",
            }
        )

        _runner = runner or self.start_objective_lifecycle(objective.id)

        def worker() -> None:
            self._run_atomic_generation(objective.id, generation_id, mermaid.version, lifecycle_runner=_runner)

        if async_mode:
            _ATOMIC_GENERATION.start(objective.id, worker)
        else:
            worker()
        return {"atomic_generation": self._atomic_generation_state(objective.id)}


    def _atomic_generation_is_stale(self, generation: dict[str, object], objective_id: str = "") -> bool:
        if generation.get("status") != "running":
            return False
        # If the in-memory coordinator thread is still alive, it's not stale
        if objective_id and objective_id in _ATOMIC_GENERATION._running:
            return False
        last_activity_at = str(generation.get("last_activity_at") or "")
        if not last_activity_at:
            return False
        try:
            last_activity = _dt.datetime.fromisoformat(last_activity_at)
        except ValueError:
            return False
        age_seconds = (_dt.datetime.now(_dt.timezone.utc) - last_activity).total_seconds()
        # LLM calls can take several minutes; 5 minutes is a reasonable staleness threshold
        return age_seconds > 300


    def _mark_atomic_generation_interrupted(self, objective: Objective, generation: dict[str, object]) -> None:
        generation_id = str(generation.get("generation_id") or "")
        if not generation_id:
            return
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_failed",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Atomic generation was interrupted before publishing units. The harness can resume from the accepted flowchart.",
                metadata={
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                    "interrupted": True,
                },
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "interrupted",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": "Atomic generation was interrupted and is eligible for restart.",
            }
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content="Action receipt: Atomic generation was interrupted. Resuming from the accepted flowchart.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "interrupted",
                    "generation_id": generation_id,
                    "diagram_version": generation.get("diagram_version"),
                },
            )
        )


    def _maybe_resume_atomic_generation(self, objective_id: str) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        if mermaid is None or mermaid.status != MermaidStatus.FINISHED:
            return
        generation = self._atomic_generation_state(objective_id)
        linked_tasks = [task for task in self.store.list_tasks(objective.project_id) if task.objective_id == objective_id]
        if generation.get("status") == "running" and self._atomic_generation_is_stale(generation, objective_id):
            self._mark_atomic_generation_interrupted(objective, generation)
            generation = self._atomic_generation_state(objective_id)
        if generation.get("status") == "completed":
            return
        if generation.get("status") == "running" and not self._atomic_generation_is_stale(generation, objective_id):
            return
        has_runnable_linked_work = any(task.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} for task in linked_tasks)
        if linked_tasks and has_runnable_linked_work:
            return
        self.queue_atomic_generation(objective_id, async_mode=self._workflow_async_mode())


    def _run_atomic_generation(self, objective_id: str, generation_id: str, diagram_version: int, *, lifecycle_runner=None) -> None:
        objective = self.store.get_objective(objective_id)
        if objective is None:
            return
        _lr = lifecycle_runner
        try:
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="reading accepted flowchart",
                content=f"Reading accepted Mermaid v{diagram_version} before decomposition.",
            )
            if _lr is not None and _lr.phase == ObjectivePhase.MERMAID_REVIEW:
                _lr._advance(ObjectivePhase.TRIO_PLANNING)
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="running TRIO planning",
                content="Running TRIO plan decomposition with red-team review.",
            )
            trio_result = self._generate_trio_plans_for_objective(objective)
            if not trio_result.success or not trio_result.plans:
                raise RuntimeError(
                    f"TRIO planning failed after {trio_result.rounds_completed} round(s): "
                    f"{trio_result.stop_reason}"
                )
            plans_data = trio_result.plans
            from ..skills.plan_draft import materialize_plans_from_skill_output
            materialized = materialize_plans_from_skill_output(
                self.store, objective.id, plans_data, author_tag="plan_draft_trio",
            )
            self._record_atomic_generation_progress(
                objective,
                generation_id,
                diagram_version,
                phase="publishing units",
                content=f"Publishing {len(materialized)} TRIO plans as tasks.",
            )
            for index, plan in enumerate(materialized, start=1):
                sl = plan.slice or {}
                target_impl = str(sl.get("target_impl") or "").split("::", 1)[0].strip()
                target_test = str(sl.get("target_test") or "").split("::", 1)[0].strip()
                files_to_touch = [p for p in (target_impl, target_test) if p]
                scope = {
                    "files_to_touch": files_to_touch,
                    "files_not_to_touch": [],
                    "approach": str(sl.get("transformation") or sl.get("label") or ""),
                    "risks": list(sl.get("risks") or []),
                    "estimated_complexity": str(sl.get("estimated_complexity") or "medium"),
                }
                task = self.task_service.create_task_with_policy(
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    title=str(sl.get("label") or f"Plan {plan.id}"),
                    objective=str(sl.get("transformation") or sl.get("label") or ""),
                    priority=objective.priority,
                    parent_task_id=None,
                    source_run_id=None,
                    external_ref_type=None,
                    external_ref_id=None,
                    validation_profile="generic",
                    validation_mode="lightweight_operator",
                    scope=scope,
                    strategy="trio_plan",
                    max_attempts=3,
                    required_artifacts=["plan", "report"],
                    mermaid_node_id=plan.mermaid_node_id,
                    plan_id=plan.id,
                )
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="atomic_unit_generated",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        task_id=task.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Generated TRIO plan {index}: {task.title}",
                        metadata={
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                            "plan_id": plan.id,
                            "title": task.title,
                            "objective": task.objective,
                            "target_impl": sl.get("target_impl") or "",
                            "target_test": sl.get("target_test") or "",
                            "strategy": task.strategy,
                        },
                    )
                )
                self.store.create_context_record(
                    ContextRecord(
                        id=new_id("context"),
                        record_type="action_receipt",
                        project_id=objective.project_id,
                        objective_id=objective.id,
                        visibility="operator_visible",
                        author_type="system",
                        content=f"Action receipt: Published TRIO plan {index}: {task.title}",
                        metadata={
                            "kind": "atomic_generation",
                            "status": "publishing",
                            "generation_id": generation_id,
                            "diagram_version": diagram_version,
                            "order": index,
                            "task_id": task.id,
                            "plan_id": plan.id,
                        },
                    )
                )
                time.sleep(0.12)
            if _lr is not None and _lr.phase == ObjectivePhase.TRIO_PLANNING:
                _lr._advance(ObjectivePhase.EXECUTING)
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_completed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Generated {len(materialized)} TRIO plans from Mermaid v{diagram_version}.",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version, "unit_count": len(materialized)},
                )
            )
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Action receipt: TRIO generation complete. {len(materialized)} plans are ready for review.",
                    metadata={"kind": "atomic_generation", "status": "completed", "generation_id": generation_id, "unit_count": len(materialized)},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "atomic_generation",
                    "stage_status": "completed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "generation_id": generation_id,
                    "detail": f"Generated {len(materialized)} TRIO plan(s) from Mermaid v{diagram_version}.",
                }
            )
            self.store.update_objective_phase(objective.id)
            if self.auto_resume_atomic_generation:
                _BACKGROUND_SUPERVISOR.start(objective.project_id, self.ctx.engine, watch=True)
        except Exception as exc:
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="atomic_generation_failed",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content=f"Atomic generation failed: {exc}",
                    metadata={"generation_id": generation_id, "diagram_version": diagram_version},
                )
            )
            self.store.create_context_record(
                ContextRecord(
                    id=new_id("context"),
                    record_type="action_receipt",
                    project_id=objective.project_id,
                    objective_id=objective.id,
                    visibility="operator_visible",
                    author_type="system",
                    content="Action receipt: Atomic generation failed. Ask the harness to retry or revise the flowchart decomposition.",
                    metadata={"kind": "atomic_generation", "status": "failed", "generation_id": generation_id},
                )
            )
            self._emit_workflow_progress(
                {
                    "type": "workflow_stage_changed",
                    "stage_kind": "atomic_generation",
                    "stage_status": "failed",
                    "objective_id": objective.id,
                    "objective_title": objective.title,
                    "generation_id": generation_id,
                    "detail": f"Atomic generation failed: {exc}",
                }
            )
            self.store.update_objective_status(objective.id, ObjectiveStatus.PAUSED)


    def _record_atomic_generation_progress(
        self,
        objective: Objective,
        generation_id: str,
        diagram_version: int,
        *,
        phase: str,
        content: str,
    ) -> None:
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="atomic_generation_progress",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=content,
                metadata={"generation_id": generation_id, "diagram_version": diagram_version, "phase": phase},
            )
        )
        self._emit_workflow_progress(
            {
                "type": "workflow_stage_changed",
                "stage_kind": "atomic_generation",
                "stage_status": "progress",
                "objective_id": objective.id,
                "objective_title": objective.title,
                "generation_id": generation_id,
                "detail": f"Atomic generation phase: {phase}.",
            }
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="action_receipt",
                project_id=objective.project_id,
                objective_id=objective.id,
                visibility="operator_visible",
                author_type="system",
                content=f"Action receipt: Atomic generation phase changed to {phase}.",
                metadata={
                    "kind": "atomic_generation",
                    "status": "progress",
                    "generation_id": generation_id,
                    "diagram_version": diagram_version,
                    "phase": phase,
                },
            )
        )


    def _generate_trio_plans_for_objective(self, objective):
        """Run trio_plan_orchestrator.generate_trio_plans for an objective.

        Gathers intent model, interrogation context, and builds the
        SkillContext from the project's source root so TRIO plans are
        grounded against the real repo inventory.
        """
        from ..services.trio_plan_orchestrator import generate_trio_plans
        from ..skills.context import build_default_skill_context

        intent_model = self.store.latest_intent_model(objective.id)
        source_root = self._resolve_source_root(objective.project_id)
        skill_context = build_default_skill_context(source_root)

        interrogation_service = getattr(self.ctx, "interrogation_service", None)
        llm_router = getattr(interrogation_service, "llm_router", None)
        if llm_router is None or not getattr(llm_router, "executors", {}):
            raise RuntimeError("No LLM router available for TRIO planning")

        intent_inputs = {
            "objective_title": objective.title,
            "objective_summary": objective.summary,
            "intent_summary": intent_model.intent_summary if intent_model else "",
            "success_definition": intent_model.success_definition if intent_model else "",
            "non_negotiables": list(intent_model.non_negotiables) if intent_model else [],
            "frustration_signals": list(getattr(intent_model, "frustration_signals", []) or []),
        }
        return generate_trio_plans(
            intent_inputs=intent_inputs,
            project_id=objective.project_id,
            objective_id=objective.id,
            skill_context=skill_context,
            llm_router=llm_router,
            store=self.store,
            workspace_root=self.workspace_root,
            telemetry=getattr(self.ctx, "telemetry", None),
        )


    def _atomic_generation_state(self, objective_id: str) -> dict[str, object]:
        mermaid = self.store.latest_mermaid_artifact(objective_id, "workflow_control")
        diagram_version = mermaid.version if mermaid is not None else None
        starts = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_started")
            if diagram_version is None or int(record.metadata.get("diagram_version") or 0) == diagram_version
        ]
        if not starts:
            return {
                "status": "idle",
                "diagram_version": diagram_version,
                "generation_id": "",
                "started_at": "",
                "completed_at": "",
                "failed_at": "",
                "unit_count": 0,
            }
        start = starts[-1]
        generation_id = str(start.metadata.get("generation_id") or start.id)
        completed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_completed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        failed = next(
            (
                record
                for record in reversed(self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_failed"))
                if str(record.metadata.get("generation_id") or "") == generation_id
            ),
            None,
        )
        unit_count = len(
            [
                record
                for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
                if str(record.metadata.get("generation_id") or "") == generation_id
            ]
        )
        progress = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_generation_progress")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        status = "running"
        if failed is not None:
            status = "failed"
        elif completed is not None:
            status = "completed"
        phase = ""
        if status == "completed":
            phase = "complete"
        elif status == "failed":
            phase = "failed"
        elif progress:
            phase = str(progress[-1].metadata.get("phase") or "")
        related_times = [start.created_at]
        if progress:
            related_times.extend(record.created_at for record in progress)
        related_times.extend(
            record.created_at
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        )
        if completed is not None:
            related_times.append(completed.created_at)
        if failed is not None:
            related_times.append(failed.created_at)
        last_activity_at = max(related_times).isoformat() if related_times else ""
        # Extract refinement round and latest critique/coverage from telemetry
        telemetry = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_decomposition_telemetry")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        atomic_phases = self.workflow_timing.sequential_phase_rows(
            start.created_at,
            [(str(record.metadata.get("phase") or ""), record.created_at) for record in progress],
            completed_at=completed.created_at if completed is not None else None,
            failed_at=failed.created_at if failed is not None else None,
            last_activity_at=max(related_times) if related_times else None,
        )
        round_map: dict[int, dict[str, object]] = {}
        for record in telemetry:
            raw_round = record.metadata.get("round")
            if raw_round in (None, ""):
                continue
            try:
                round_number = int(raw_round)
            except (TypeError, ValueError):
                continue
            event_type = str(record.metadata.get("event_type") or "")
            current = round_map.setdefault(
                round_number,
                {
                    "round_number": round_number,
                    "started_at": record.created_at.isoformat(),
                    "ended_at": record.created_at.isoformat(),
                    "duration_ms": 0,
                    "events": [],
                    "critique_accepted": None,
                    "coverage_accepted": None,
                    "stalled": False,
                    "unit_count": 0,
                },
            )
            current["ended_at"] = max(str(current.get("ended_at") or ""), record.created_at.isoformat())
            current_events = list(current.get("events") or [])
            current_events.append(event_type)
            current["events"] = current_events
            if event_type == "round_complete":
                current["duration_ms"] = max(
                    int(current.get("duration_ms") or 0),
                    int(float(record.metadata.get("total_round_seconds") or 0.0) * 1000),
                )
                current["critique_accepted"] = record.metadata.get("critique_accepted")
                current["coverage_accepted"] = record.metadata.get("coverage_accepted")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type == "critique":
                current["critique_accepted"] = record.metadata.get("accepted")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type == "coverage":
                current["coverage_accepted"] = record.metadata.get("complete")
                current["unit_count"] = int(record.metadata.get("unit_count") or current.get("unit_count") or 0)
            elif event_type in {"generate", "refine"}:
                current["unit_count"] = int(record.metadata.get("unit_count") or record.metadata.get("unit_count_after") or current.get("unit_count") or 0)
            elif event_type in {"stall_detected", "stall_exit"}:
                current["stalled"] = True
        atomic_rounds = []
        for round_number in sorted(round_map):
            current = round_map[round_number]
            if not int(current.get("duration_ms") or 0):
                current["duration_ms"] = self.workflow_timing.duration_ms(
                    str(current.get("started_at") or ""),
                    last_activity_at=str(current.get("ended_at") or ""),
                )
            atomic_rounds.append(current)
        refinement_round = 0
        critique_accepted = None
        coverage_complete = None
        last_critique_problems = []
        last_coverage_gaps = []
        for record in telemetry:
            evt = record.metadata.get("event_type", "")
            rnd = record.metadata.get("round")
            if rnd is not None and int(rnd) > refinement_round:
                refinement_round = int(rnd)
            if evt == "critique":
                critique_accepted = record.metadata.get("accepted")
                last_critique_problems = list(record.metadata.get("problems") or [])
            if evt == "coverage":
                coverage_complete = record.metadata.get("complete")
                last_coverage_gaps = list(record.metadata.get("gaps") or [])
        return {
            "status": status,
            "diagram_version": diagram_version,
            "generation_id": generation_id,
            "started_at": start.created_at.isoformat(),
            "completed_at": completed.created_at.isoformat() if completed is not None else "",
            "failed_at": failed.created_at.isoformat() if failed is not None else "",
            "unit_count": unit_count,
            "phase": phase,
            "last_activity_at": last_activity_at,
            "duration_ms": self.workflow_timing.duration_ms(
                start.created_at,
                completed_at=completed.created_at if completed is not None else None,
                failed_at=failed.created_at if failed is not None else None,
                last_activity_at=max(related_times) if related_times else None,
            ),
            "atomic_phases": atomic_phases,
            "atomic_rounds": atomic_rounds,
            "error": failed.content if failed is not None else "",
            "refinement_round": refinement_round,
            "critique_accepted": critique_accepted,
            "coverage_complete": coverage_complete,
            "last_critique_problems": last_critique_problems,
            "last_coverage_gaps": last_coverage_gaps,
            "is_stale": self._atomic_generation_is_stale(
                {
                    "status": status,
                    "last_activity_at": last_activity_at,
                },
                objective_id,
            ),
        }


    def _atomic_units_for_objective(
        self,
        objective_id: str,
        linked_tasks: list[Task],
        generation_state: dict[str, object],
    ) -> list[dict[str, object]]:
        generation_id = str(generation_state.get("generation_id") or "")
        if not generation_id:
            return []
        tasks_by_id = {task.id: task for task in linked_tasks}
        task_runs = {task.id: self.store.list_runs(task.id) for task in linked_tasks}
        units: list[dict[str, object]] = []
        records = [
            record
            for record in self.store.list_context_records(objective_id=objective_id, record_type="atomic_unit_generated")
            if str(record.metadata.get("generation_id") or "") == generation_id
        ]
        published_task_ids: set[str] = set()

        for record in records:
            task_id = str(record.metadata.get("task_id") or "")
            if task_id:
                published_task_ids.add(task_id)
            task = tasks_by_id.get(task_id)
            runs = task_runs.get(task_id, [])
            latest_run = runs[-1] if runs else None

            status = task.status.value if task is not None else "pending"

            # Read validation results from the report artifact if available.
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass

            units.append(
                {
                    "id": task_id or record.id,
                    "title": str(record.metadata.get("title") or (task.title if task else record.content)),
                    "objective": str(record.metadata.get("objective") or (task.objective if task else "")),
                    "rationale": str(record.metadata.get("rationale") or ""),
                    "strategy": str(record.metadata.get("strategy") or (task.strategy if task else "")),
                    "status": status,
                    "order": int(record.metadata.get("order") or 0),
                    "published_unit": True,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
        next_order = len(units) + 1
        for task in linked_tasks:
            if task.id in published_task_ids:
                continue
            runs = task_runs.get(task.id, [])
            latest_run = runs[-1] if runs else None
            validation_info = None
            if latest_run is not None:
                report_artifacts = [
                    a for a in self.store.list_artifacts(latest_run.id)
                    if a.kind == "report" and a.path
                ]
                if report_artifacts:
                    try:
                        import json as _json
                        report_data = _json.loads(Path(report_artifacts[-1].path).read_text(encoding="utf-8"))
                        cc = report_data.get("compile_check")
                        tc = report_data.get("test_check")
                        if cc is not None or tc is not None:
                            validation_info = {
                                "compile_passed": bool(cc.get("passed")) if cc else None,
                                "test_passed": bool(tc.get("passed")) if tc else None,
                                "test_timed_out": bool(tc.get("timed_out")) if tc else False,
                            }
                    except Exception:
                        pass
            units.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "objective": task.objective,
                    "rationale": "",
                    "strategy": task.strategy,
                    "status": task.status.value,
                    "order": next_order,
                    "published_unit": False,
                    "latest_run": (
                        {
                            "attempt": latest_run.attempt,
                            "status": latest_run.status.value,
                            "started_at": latest_run.created_at.isoformat() if latest_run.created_at else None,
                            "finished_at": latest_run.updated_at.isoformat() if latest_run.status.value in ("completed", "failed", "blocked", "disposed") and latest_run.updated_at else None,
                            "validation": validation_info,
                        }
                        if latest_run is not None
                        else None
                    ),
                }
            )
            next_order += 1
        return sorted(units, key=lambda item: (int(item["order"]), str(item["title"])))

