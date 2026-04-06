"""Accruvia Harness MCP Server.

Exposes the harness as tools that Claude Code can call natively, replacing
the 12-layer mediation in the old UI. The user talks directly to Claude.
Claude calls these tools when it needs harness data or wants to take action.

Tools:

  Read-only (inspection):
    harness_status       — project, task, run overview
    get_task_detail      — full detail for a specific task
    get_run_detail       — artifacts, evaluations, decisions for a run
    explain_run          — human-readable summary of what a run did
    list_skills          — all registered skills with descriptions
    cost_summary         — LLM spend per task

  Actions (mutation):
    request_feature      — translate intent + create + run task
    create_task          — create a task with explicit objective + scope
    run_task             — execute a pending task via skills pipeline
    auto_merge           — evaluate + merge a promoted run
    create_project       — set up a new project

The server uses stdio transport (Claude Code's default for local servers).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fastmcp import FastMCP

# Ensure the source tree is importable
_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root / "src") not in sys.path:
    sys.path.insert(0, str(_repo_root / "src"))

from accruvia_harness.config import HarnessConfig
from accruvia_harness.store import SQLiteHarnessStore
from accruvia_harness.domain import (
    TaskStatus,
    new_id,
    serialize_dataclass,
)

mcp = FastMCP("accruvia-harness")

# ---------------------------------------------------------------------------
# Lazy-loaded singletons (initialized on first tool call)
# ---------------------------------------------------------------------------
_store: SQLiteHarnessStore | None = None
_config: HarnessConfig | None = None


def _get_store() -> SQLiteHarnessStore:
    global _store, _config
    if _store is None:
        _config = HarnessConfig.from_env()
        _store = SQLiteHarnessStore(_config.db_path)
        _store.initialize()
    return _store


def _get_config() -> HarnessConfig:
    global _config
    if _config is None:
        _config = HarnessConfig.from_env()
    return _config


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

@mcp.tool()
def harness_status(project_id: str | None = None) -> dict:
    """Get an overview of the harness: projects, tasks by status, recent runs.
    If project_id is given, scope to that project."""
    store = _get_store()
    projects = store.list_projects()
    if project_id:
        projects = [p for p in projects if p.id == project_id]

    result: dict = {"projects": []}
    for project in projects:
        tasks = store.list_tasks(project.id)
        by_status: dict[str, int] = {}
        for t in tasks:
            by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
        recent_tasks = sorted(tasks, key=lambda t: t.updated_at, reverse=True)[:5]
        result["projects"].append({
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "task_counts": by_status,
            "recent_tasks": [
                {"id": t.id, "title": t.title, "status": t.status.value}
                for t in recent_tasks
            ],
        })
    from accruvia_harness.skills import build_default_registry
    result["skills_registered"] = len(build_default_registry())
    return result


@mcp.tool()
def get_task_detail(task_id: str) -> dict:
    """Get full detail for a specific task: objective, scope, status, runs."""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        return {"error": f"Task not found: {task_id}"}
    runs = store.list_runs(task_id)
    return {
        "task": serialize_dataclass(task),
        "runs": [
            {
                "id": r.id,
                "attempt": r.attempt,
                "status": r.status.value,
                "summary": r.summary,
            }
            for r in runs
        ],
    }


@mcp.tool()
def get_run_detail(run_id: str) -> dict:
    """Get artifacts, evaluations, and decisions for a specific run."""
    store = _get_store()
    run = store.get_run(run_id)
    if run is None:
        return {"error": f"Run not found: {run_id}"}
    artifacts = store.list_artifacts(run_id)
    evaluations = store.list_evaluations(run_id)
    decisions = store.list_decisions(run_id)
    # Load report artifact content if available
    report_content = None
    for a in artifacts:
        if a.kind == "report":
            try:
                report_content = json.loads(Path(a.path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    return {
        "run": serialize_dataclass(run),
        "artifacts": [{"kind": a.kind, "path": a.path} for a in artifacts],
        "evaluations": [serialize_dataclass(e) for e in evaluations],
        "decisions": [serialize_dataclass(d) for d in decisions],
        "report": report_content,
    }


@mcp.tool()
def get_intent_diagram(task_id: str) -> dict:
    """Get the Mermaid diagram showing the system's understanding of a task's
    intent. This is the visual representation of 'what was asked → what will
    be built → how it will be verified.' Rendered in the UI so non-developers
    can see intent vs execution at a glance.

    If the task was created via /translate-intent, the diagram is stored in the
    run artifacts. Otherwise, a basic diagram is generated from task metadata.
    """
    store = _get_store()
    config = _get_config()
    task = store.get_task(task_id)
    if task is None:
        return {"error": f"Task not found: {task_id}"}

    # Try to find the translate-intent output for this task
    for run in store.list_runs(task_id):
        run_dir = config.workspace_root / "runs" / run.id
        for artifact_dir in [run_dir, config.workspace_root / "requests"]:
            for json_path in artifact_dir.rglob("translate_intent_analysis.json"):
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    output = data.get("output") or {}
                    if output.get("mermaid_diagram"):
                        return {
                            "task_id": task_id,
                            "mermaid": output["mermaid_diagram"],
                            "why_chain": output.get("why_chain", []),
                            "acceptance_criteria": output.get("acceptance_criteria", []),
                            "source": "translate_intent",
                        }
                except (OSError, json.JSONDecodeError):
                    continue

    # Fallback: generate a basic diagram from task metadata
    criteria = []
    scope = task.scope or {}
    files = scope.get("allowed_paths") or []
    diagram = (
        "stateDiagram-v2\n"
        f'    [*] --> Intent: "{_escape_mermaid(task.title)}"\n'
        f'    Intent --> Objective: "{_escape_mermaid(task.objective[:80])}"\n'
    )
    if files:
        diagram += f'    Objective --> Scope: "Files: {", ".join(f[:30] for f in files[:3])}"\n'
        diagram += f'    Scope --> Pipeline: "strategy: {task.strategy}"\n'
    else:
        diagram += f'    Objective --> Pipeline: "strategy: {task.strategy}"\n'
    diagram += '    Pipeline --> [*]: "status: ' + task.status.value + '"\n'
    return {
        "task_id": task_id,
        "mermaid": diagram,
        "why_chain": [],
        "acceptance_criteria": criteria,
        "source": "generated_from_metadata",
    }


def _escape_mermaid(text: str) -> str:
    """Escape text for Mermaid diagram labels."""
    return text.replace('"', "'").replace("\n", " ")[:100]


@mcp.tool()
def explain_run(run_id: str) -> dict:
    """Get a human-readable summary of what a run accomplished or why it failed."""
    store = _get_store()
    run = store.get_run(run_id)
    if run is None:
        return {"error": f"Run not found: {run_id}"}
    artifacts = store.list_artifacts(run_id)
    # Try to load and use /summarize-run skill output
    for a in artifacts:
        if a.kind == "report":
            try:
                report = json.loads(Path(a.path).read_text(encoding="utf-8"))
                return {
                    "run_id": run_id,
                    "status": run.status.value,
                    "summary": run.summary,
                    "changed_files": report.get("changed_files", []),
                    "ship_ready": report.get("ship_ready"),
                    "quality_concerns": report.get("quality_concerns", []),
                    "implementation_rationale": report.get("implementation_rationale", ""),
                    "scope_approach": (report.get("scope") or {}).get("approach", ""),
                }
            except (OSError, json.JSONDecodeError):
                pass
    return {
        "run_id": run_id,
        "status": run.status.value,
        "summary": run.summary,
    }


@mcp.tool()
def list_skills() -> dict:
    """List all registered skills with their names."""
    from accruvia_harness.skills import build_default_registry
    registry = build_default_registry()
    skills = []
    for skill in registry:
        skills.append({
            "name": skill.name,
            "has_output_schema": bool(getattr(skill, "output_schema", None)),
        })
    return {"count": len(skills), "skills": skills}


@mcp.tool()
def cost_summary(project_id: str | None = None) -> dict:
    """Show LLM spending per task. Reads cost data from run metadata files."""
    store = _get_store()
    config = _get_config()
    projects = store.list_projects()
    if project_id:
        projects = [p for p in projects if p.id == project_id]
    rows: list[dict] = []
    for project in projects:
        for task in store.list_tasks(project.id):
            task_cost = 0.0
            task_tokens = 0
            for run in store.list_runs(task.id):
                run_dir = config.workspace_root / "runs" / run.id
                for meta_path in run_dir.rglob("llm_metadata.json"):
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        task_cost += float(meta.get("cost_usd") or 0)
                        task_tokens += int(meta.get("total_tokens") or 0)
                    except (OSError, json.JSONDecodeError, ValueError):
                        continue
            rows.append({
                "project": project.name,
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "cost_usd": round(task_cost, 4),
                "total_tokens": task_tokens,
            })
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    total_cost = sum(r["cost_usd"] for r in rows)
    total_tokens = sum(r["total_tokens"] for r in rows)
    return {
        "tasks": rows,
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
    }


# ---------------------------------------------------------------------------
# Action tools
# ---------------------------------------------------------------------------

@mcp.tool()
def request_feature(intent: str, project_id: str | None = None, dry_run: bool = False) -> dict:
    """Translate a plain-language feature request into a technical task and
    optionally execute it. This is the non-developer entry point.

    Args:
        intent: What you want, in your own words.
        project_id: Project to create the task in (uses first project if omitted).
        dry_run: If True, show the translation without executing.
    """
    store = _get_store()
    config = _get_config()
    projects = store.list_projects()
    if project_id:
        project = store.get_project(project_id)
    else:
        project = projects[0] if projects else None
    if project is None:
        return {"error": "No project found. Create one first."}

    from accruvia_harness.skills import TranslateIntentSkill, SkillInvocation, invoke_skill
    from accruvia_harness.services.work_orchestrator import _collect_repo_context, _search_codebase
    from accruvia_harness.llm import build_llm_router
    from accruvia_harness.domain import Task, Run, RunStatus

    workspace = Path(".")
    repo_context = _collect_repo_context(workspace)

    import re
    queries = re.findall(r'"([^"]+)"|([A-Z][a-z]+(?:[A-Z][a-z]+)+)', intent)
    search_queries = [q[0] or q[1] for q in queries if q[0] or q[1]]
    search_results = _search_codebase(workspace, search_queries) if search_queries else {}

    skill = TranslateIntentSkill()
    task = Task(id=new_id("request"), project_id=project.id, title="Intent translation",
                objective=intent, strategy="request", status=TaskStatus.COMPLETED)
    run = Run(id=new_id("request_run"), task_id=task.id, status=RunStatus.COMPLETED,
              attempt=1, summary="translate intent")
    run_dir = config.workspace_root / "requests" / run.id

    llm_router = build_llm_router(config)
    result = invoke_skill(
        skill,
        SkillInvocation(skill_name="translate_intent", inputs={
            "intent": intent,
            "repo_context": repo_context,
            "project_description": project.description,
            "codebase_search_results": search_results,
        }, task=task, run=run, run_dir=run_dir),
        llm_router,
    )
    if not result.success:
        return {"error": "translate_intent failed", "errors": result.errors}

    output = result.output
    translation = {
        "summary_for_requester": output.get("summary_for_requester", ""),
        "acceptance_criteria": output.get("acceptance_criteria", []),
        "estimated_complexity": output.get("estimated_complexity", ""),
        "risks": output.get("risks_plain_language", []),
        "technical_objective": output.get("technical_objective", ""),
        "suggested_files": output.get("suggested_files", []),
    }
    if dry_run:
        return {"dry_run": True, "translation": translation}

    # Create and run the task
    from accruvia_harness.bootstrap import build_engine_from_config
    engine = build_engine_from_config(config, store=store)
    scope: dict = {}
    if output.get("suggested_files"):
        scope["allowed_paths"] = output["suggested_files"]
    if output.get("suggested_forbidden_files"):
        scope["forbidden_paths"] = output["suggested_forbidden_files"]
    created = store.create_task(Task(
        id=new_id("task"), project_id=project.id,
        title=f"Request: {intent[:60]}",
        objective=output.get("technical_objective", intent),
        strategy="implementation", status=TaskStatus.PENDING,
        priority=700, scope=scope,
        validation_profile=output.get("validation_profile", "generic"),
        required_artifacts=["report"],
    ))
    runs = engine.run_until_stable(created.id)
    final_task = store.get_task(created.id)
    return {
        "translation": translation,
        "task": serialize_dataclass(final_task),
        "runs": [{"id": r.id, "status": r.status.value, "summary": r.summary} for r in runs],
        "completed": final_task.status.value == "completed" if final_task else False,
    }


@mcp.tool()
def create_task(
    project_id: str,
    title: str,
    objective: str,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    validation_profile: str = "python",
    priority: int = 700,
    max_attempts: int = 2,
) -> dict:
    """Create a task with explicit objective and scope (developer path)."""
    store = _get_store()
    from accruvia_harness.domain import Task
    scope: dict = {}
    if allowed_paths:
        scope["allowed_paths"] = allowed_paths
    if forbidden_paths:
        scope["forbidden_paths"] = forbidden_paths
    task = store.create_task(Task(
        id=new_id("task"),
        project_id=project_id,
        title=title,
        objective=objective,
        strategy="implementation",
        status=TaskStatus.PENDING,
        priority=priority,
        scope=scope,
        validation_profile=validation_profile,
        required_artifacts=["report"],
        max_attempts=max_attempts,
    ))
    return serialize_dataclass(task)


@mcp.tool()
def run_task(task_id: str) -> dict:
    """Execute a pending task through the skills pipeline."""
    store = _get_store()
    config = _get_config()
    os.environ.setdefault("ACCRUVIA_WORKER_BACKEND", "skills")
    from accruvia_harness.bootstrap import build_engine_from_config
    engine = build_engine_from_config(config, store=store)
    runs = engine.run_until_stable(task_id)
    task = store.get_task(task_id)
    return {
        "task": serialize_dataclass(task),
        "runs": [{"id": r.id, "status": r.status.value, "summary": r.summary} for r in runs],
    }


@mcp.tool()
def auto_merge(run_id: str, dry_run: bool = False) -> dict:
    """Evaluate merge policy and merge a promoted run to main.

    Args:
        run_id: The run to evaluate and merge.
        dry_run: If True, evaluate without executing.
    """
    from accruvia_harness.merge_gate import auto_merge_run, MergePolicy
    store = _get_store()
    decision, result = auto_merge_run(store, run_id, Path("."), dry_run=dry_run)
    output = {
        "auto_merge": decision.auto_merge,
        "reason": decision.reason,
        "concerns": decision.concerns,
        "branch": decision.branch_name,
        "changed_files": decision.changed_files,
    }
    if result is not None:
        output["merged"] = result.merged
        output["commit_sha"] = result.commit_sha
        output["stderr"] = result.stderr
    return output


@mcp.tool()
def create_project(
    name: str,
    description: str,
    adapter_name: str = "current_repo_git_worktree",
    workspace_policy: str = "isolated_required",
    promotion_mode: str = "branch_only",
    base_branch: str = "main",
) -> dict:
    """Create a new harness project."""
    store = _get_store()
    from accruvia_harness.domain import Project, WorkspacePolicy, PromotionMode
    project = store.create_project(Project(
        id=new_id("project"),
        name=name,
        description=description,
        adapter_name=adapter_name,
        workspace_policy=WorkspacePolicy(workspace_policy),
        promotion_mode=PromotionMode(promotion_mode),
        base_branch=base_branch,
    ))
    return serialize_dataclass(project)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
