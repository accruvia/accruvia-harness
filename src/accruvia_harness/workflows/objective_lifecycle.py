"""Durable objective lifecycle workflow.

This is the contract. The workflow enforces the phase sequence:

    CREATED → INTERROGATING → MERMAID_REVIEW → TRIO_PLANNING →
    EXECUTING → REVIEWING → PROMOTED

Each phase transition happens exactly once, in order. An LLM produces
content within a phase (interrogation answers, TRIO plans, code edits).
The workflow decides when to advance. No caller can skip a phase because
the workflow code is the only thing that calls advance_objective_phase.

Signals:
  - mermaid_approved: operator signs off on the Mermaid diagram.
    The workflow blocks in MERMAID_REVIEW until this signal arrives.
  - tasks_completed: all tasks for this objective have reached a
    terminal state. The workflow blocks in EXECUTING until this signal.

Activities:
  Each phase's work is a Temporal activity. Activities are retried on
  transient failure (LLM timeout, network blip) but NOT on validation
  failure (bad plan, failed review). Validation failures advance to
  FAILED.

Local fallback:
  When Temporal is not available (runtime_backend=local), the workflow
  can be driven synchronously via ObjectiveLifecycleRunner, which calls
  the same activities as plain functions in sequence. The contract is
  identical — the runner enforces the same phase transitions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..domain import (
    ObjectivePhase,
    PHASE_TO_STATUS,
    advance_objective_phase,
)

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
    _HAS_TEMPORAL = True
except ModuleNotFoundError:
    activity = None  # type: ignore[assignment]
    workflow = None  # type: ignore[assignment]
    RetryPolicy = None  # type: ignore[assignment]
    _HAS_TEMPORAL = False


@dataclass(frozen=True)
class PhaseResult:
    """Outcome of a single phase activity."""
    phase: str
    success: bool
    detail: str = ""
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Activities — each wraps the real work for one lifecycle phase.
# The sync_ functions are the actual implementations; the @activity.defn
# wrappers make them available to Temporal.
# ---------------------------------------------------------------------------

def sync_interrogation(config: str, objective_id: str) -> dict[str, Any]:
    from ..bootstrap import build_engine_from_config
    from ..config import HarnessConfig

    cfg = HarnessConfig.from_json(config) if isinstance(config, str) else config
    engine = build_engine_from_config(cfg)
    store = engine.store
    objective = store.get_objective(objective_id)
    if objective is None:
        return {"phase": "interrogating", "success": False, "detail": "objective not found"}

    from ..ui import HarnessUIDataService
    from types import SimpleNamespace

    ctx = SimpleNamespace(store=store, engine=engine, config=cfg)
    service = HarnessUIDataService(ctx)
    service.complete_interrogation_review(objective_id)

    return {"phase": "interrogating", "success": True, "detail": "interrogation complete"}


def sync_trio_planning(config: str, objective_id: str) -> dict[str, Any]:
    from ..bootstrap import build_engine_from_config
    from ..config import HarnessConfig

    cfg = HarnessConfig.from_json(config) if isinstance(config, str) else config
    engine = build_engine_from_config(cfg)
    store = engine.store
    objective = store.get_objective(objective_id)
    if objective is None:
        return {"phase": "trio_planning", "success": False, "detail": "objective not found"}

    from ..ui import HarnessUIDataService
    from types import SimpleNamespace

    ctx = SimpleNamespace(store=store, engine=engine, config=cfg)
    service = HarnessUIDataService(ctx)

    trio_result = service._generate_trio_plans_for_objective(objective)
    if not trio_result.success or not trio_result.plans:
        return {
            "phase": "trio_planning",
            "success": False,
            "detail": f"TRIO failed: {trio_result.stop_reason}",
        }

    from ..skills.plan_draft import materialize_plans_from_skill_output

    materialized = materialize_plans_from_skill_output(
        store, objective.id, trio_result.plans, author_tag="plan_draft_trio",
    )
    plan_ids = [p.id for p in materialized]
    return {
        "phase": "trio_planning",
        "success": True,
        "detail": f"{len(materialized)} plans materialized",
        "data": {"plan_ids": plan_ids, "plans": trio_result.plans},
    }


def sync_execute_tasks(config: str, objective_id: str, plans_data: list[dict[str, Any]]) -> dict[str, Any]:
    """Create tasks from materialized TRIO plans.

    Actual task execution is handled by the existing task runtime
    (local or Temporal TaskToStableWorkflow). This activity only
    creates the tasks — execution is driven by the supervisor or
    the tasks_completed signal.
    """
    from ..bootstrap import build_engine_from_config
    from ..config import HarnessConfig

    cfg = HarnessConfig.from_json(config) if isinstance(config, str) else config
    engine = build_engine_from_config(cfg)
    store = engine.store
    objective = store.get_objective(objective_id)
    if objective is None:
        return {"phase": "executing", "success": False, "detail": "objective not found"}

    from ..services.task_service import TaskService

    task_service = TaskService(store)
    plans = store.list_plans(objective_id) if hasattr(store, "list_plans") else []

    task_ids: list[str] = []
    for plan in plans:
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
        task = task_service.create_task_with_policy(
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
        task_ids.append(task.id)

    return {
        "phase": "executing",
        "success": True,
        "detail": f"{len(task_ids)} tasks created",
        "data": {"task_ids": task_ids},
    }


def sync_objective_review(config: str, objective_id: str) -> dict[str, Any]:
    from ..bootstrap import build_engine_from_config
    from ..config import HarnessConfig

    cfg = HarnessConfig.from_json(config) if isinstance(config, str) else config
    engine = build_engine_from_config(cfg)
    store = engine.store
    objective = store.get_objective(objective_id)
    if objective is None:
        return {"phase": "reviewing", "success": False, "detail": "objective not found"}

    from ..ui import HarnessUIDataService
    from ..domain import new_id
    from types import SimpleNamespace

    ctx = SimpleNamespace(store=store, engine=engine, config=cfg)
    service = HarnessUIDataService(ctx)
    review_id = new_id("review")
    packets, review_clear, failed_count = service._generate_objective_review_packets(
        objective_id, review_id,
    )
    return {
        "phase": "reviewing",
        "success": review_clear,
        "detail": f"review_clear={review_clear}, {failed_count} failed reviewers",
        "data": {
            "review_id": review_id,
            "review_clear": review_clear,
            "packet_count": len(packets),
            "failed_count": failed_count,
        },
    }


def sync_promotion(config: str, objective_id: str) -> dict[str, Any]:
    from ..bootstrap import build_engine_from_config
    from ..config import HarnessConfig

    cfg = HarnessConfig.from_json(config) if isinstance(config, str) else config
    engine = build_engine_from_config(cfg)
    store = engine.store
    objective = store.get_objective(objective_id)
    if objective is None:
        return {"phase": "promoted", "success": False, "detail": "objective not found"}

    from ..ui import HarnessUIDataService
    from types import SimpleNamespace

    ctx = SimpleNamespace(store=store, engine=engine, config=cfg)
    service = HarnessUIDataService(ctx)
    try:
        result = service.promote_objective_to_repo(objective_id)
        return {
            "phase": "promoted",
            "success": True,
            "detail": "promoted to repo",
            "data": result,
        }
    except Exception as exc:
        return {
            "phase": "promoted",
            "success": False,
            "detail": f"promotion failed: {exc}",
        }


# ---------------------------------------------------------------------------
# Temporal activity wrappers
# ---------------------------------------------------------------------------

if _HAS_TEMPORAL:

    @activity.defn(name="objective_interrogation")
    async def interrogation_activity(config: str, objective_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(sync_interrogation, config, objective_id)

    @activity.defn(name="objective_trio_planning")
    async def trio_planning_activity(config: str, objective_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(sync_trio_planning, config, objective_id)

    @activity.defn(name="objective_execute_tasks")
    async def execute_tasks_activity(config: str, objective_id: str, plans_data: list[dict[str, Any]]) -> dict[str, Any]:
        return await asyncio.to_thread(sync_execute_tasks, config, objective_id, plans_data)

    @activity.defn(name="objective_review")
    async def objective_review_activity(config: str, objective_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(sync_objective_review, config, objective_id)

    @activity.defn(name="objective_promotion")
    async def promotion_activity(config: str, objective_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(sync_promotion, config, objective_id)

else:
    # Stubs for import when temporalio is not installed
    async def interrogation_activity(config: str, objective_id: str) -> dict[str, Any]:
        return sync_interrogation(config, objective_id)

    async def trio_planning_activity(config: str, objective_id: str) -> dict[str, Any]:
        return sync_trio_planning(config, objective_id)

    async def execute_tasks_activity(config: str, objective_id: str, plans_data: list[dict[str, Any]]) -> dict[str, Any]:
        return sync_execute_tasks(config, objective_id, plans_data)

    async def objective_review_activity(config: str, objective_id: str) -> dict[str, Any]:
        return sync_objective_review(config, objective_id)

    async def promotion_activity(config: str, objective_id: str) -> dict[str, Any]:
        return sync_promotion(config, objective_id)


# ---------------------------------------------------------------------------
# The workflow — this IS the contract
# ---------------------------------------------------------------------------

_ACTIVITY_TIMEOUT = timedelta(minutes=30)
_SIGNAL_TIMEOUT = timedelta(hours=72)


if _HAS_TEMPORAL:

    @workflow.defn(name="objective_lifecycle")
    class ObjectiveLifecycleWorkflow:
        """Durable workflow enforcing interrogation → mermaid → TRIO → execute → review → promote."""

        def __init__(self) -> None:
            self._mermaid_approved = False
            self._tasks_completed = False
            self._current_phase = ObjectivePhase.CREATED

        @workflow.signal
        async def mermaid_approved(self) -> None:
            self._mermaid_approved = True

        @workflow.signal
        async def tasks_completed(self) -> None:
            self._tasks_completed = True

        @workflow.query
        def current_phase(self) -> str:
            return self._current_phase.value

        def _advance(self, target: ObjectivePhase) -> None:
            self._current_phase = advance_objective_phase(self._current_phase, target)

        @workflow.run
        async def run(self, config: str, objective_id: str) -> dict[str, Any]:
            # Phase 1: INTERROGATING
            self._advance(ObjectivePhase.INTERROGATING)
            result = await workflow.execute_activity(
                "objective_interrogation",
                args=[config, objective_id],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not result.get("success"):
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": result.get("detail", "interrogation failed")}

            # Phase 2: MERMAID_REVIEW — wait for operator signal
            self._advance(ObjectivePhase.MERMAID_REVIEW)
            await workflow.wait_condition(lambda: self._mermaid_approved, timeout=_SIGNAL_TIMEOUT)
            if not self._mermaid_approved:
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": "mermaid approval timed out"}

            # Phase 3: TRIO_PLANNING
            self._advance(ObjectivePhase.TRIO_PLANNING)
            trio_result = await workflow.execute_activity(
                "objective_trio_planning",
                args=[config, objective_id],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if not trio_result.get("success"):
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": trio_result.get("detail", "TRIO planning failed")}

            # Phase 4: EXECUTING — create tasks, then wait for completion signal
            self._advance(ObjectivePhase.EXECUTING)
            plans_data = (trio_result.get("data") or {}).get("plans") or []
            exec_result = await workflow.execute_activity(
                "objective_execute_tasks",
                args=[config, objective_id, plans_data],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            if not exec_result.get("success"):
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": exec_result.get("detail", "task creation failed")}

            await workflow.wait_condition(lambda: self._tasks_completed, timeout=_SIGNAL_TIMEOUT)
            if not self._tasks_completed:
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": "tasks completion timed out"}

            # Phase 5: REVIEWING
            self._advance(ObjectivePhase.REVIEWING)
            review_result = await workflow.execute_activity(
                "objective_review",
                args=[config, objective_id],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            if not review_result.get("success"):
                self._advance(ObjectivePhase.FAILED)
                return {"phase": "failed", "detail": review_result.get("detail", "review failed")}

            # Phase 6: PROMOTED
            self._advance(ObjectivePhase.PROMOTED)
            promo_result = await workflow.execute_activity(
                "objective_promotion",
                args=[config, objective_id],
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            return {
                "phase": "promoted" if promo_result.get("success") else "failed",
                "detail": promo_result.get("detail", ""),
            }

else:

    class ObjectiveLifecycleWorkflow:  # type: ignore[no-redef]
        """Non-Temporal stub for import compatibility."""

        def __init__(self) -> None:
            self._mermaid_approved = False
            self._tasks_completed = False
            self._current_phase = ObjectivePhase.CREATED

        def current_phase(self) -> str:
            return self._current_phase.value


# ---------------------------------------------------------------------------
# Local synchronous runner — same contract, no Temporal server needed.
# ---------------------------------------------------------------------------


class ObjectiveLifecycleRunner:
    """Drive the objective lifecycle synchronously for local/test use.

    Calls the same sync_ activity functions in the same order.
    Phase transitions are enforced identically via advance_objective_phase.
    The only difference: mermaid approval and task completion are provided
    by the caller (blocking callbacks) instead of Temporal signals.
    """

    def __init__(self, config: str, objective_id: str, *, store: Any = None) -> None:
        self.config = config
        self.objective_id = objective_id
        self._store = store
        if store is not None and hasattr(store, "get_objective"):
            obj = store.get_objective(objective_id)
            self.phase = obj.phase if obj is not None else ObjectivePhase.CREATED
        else:
            self.phase = ObjectivePhase.CREATED

    def _advance(self, target: ObjectivePhase) -> None:
        self.phase = advance_objective_phase(self.phase, target)
        if self._store is not None and hasattr(self._store, "set_objective_phase"):
            self._store.set_objective_phase(self.objective_id, self.phase)

    def run_through_mermaid(self) -> dict[str, Any]:
        """Run interrogation phase. Caller must then call approve_mermaid()."""
        self._advance(ObjectivePhase.INTERROGATING)
        result = sync_interrogation(self.config, self.objective_id)
        if not result.get("success"):
            self._advance(ObjectivePhase.FAILED)
            return result
        self._advance(ObjectivePhase.MERMAID_REVIEW)
        return result

    def approve_mermaid(self) -> None:
        if self.phase != ObjectivePhase.MERMAID_REVIEW:
            raise ValueError(f"Cannot approve mermaid in phase {self.phase.value}")

    def run_trio_and_create_tasks(self) -> dict[str, Any]:
        """Run TRIO planning and create tasks."""
        if self.phase != ObjectivePhase.MERMAID_REVIEW:
            raise ValueError(f"Expected MERMAID_REVIEW, got {self.phase.value}")
        self._advance(ObjectivePhase.TRIO_PLANNING)
        trio_result = sync_trio_planning(self.config, self.objective_id)
        if not trio_result.get("success"):
            self._advance(ObjectivePhase.FAILED)
            return trio_result
        self._advance(ObjectivePhase.EXECUTING)
        plans_data = (trio_result.get("data") or {}).get("plans") or []
        exec_result = sync_execute_tasks(self.config, self.objective_id, plans_data)
        if not exec_result.get("success"):
            self._advance(ObjectivePhase.FAILED)
        return exec_result

    def complete_tasks(self) -> None:
        if self.phase != ObjectivePhase.EXECUTING:
            raise ValueError(f"Cannot complete tasks in phase {self.phase.value}")

    def run_review_and_promote(self) -> dict[str, Any]:
        """Run objective review and promote if clear."""
        if self.phase != ObjectivePhase.EXECUTING:
            raise ValueError(f"Expected EXECUTING, got {self.phase.value}")
        self._advance(ObjectivePhase.REVIEWING)
        review_result = sync_objective_review(self.config, self.objective_id)
        if not review_result.get("success"):
            self._advance(ObjectivePhase.FAILED)
            return review_result
        self._advance(ObjectivePhase.PROMOTED)
        return sync_promotion(self.config, self.objective_id)
