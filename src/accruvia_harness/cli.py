from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import HarnessConfig
from .domain import serialize_dataclass
from .engine import HarnessEngine
from .gitlab import GitLabCLI
from .interrogation import HarnessQueryService
from .logging_utils import HarnessLogger, classify_error
from .runtime import build_runtime
from .store import SQLiteHarnessStore
from .temporal_backend import run_temporal_worker_sync
from .workers import build_worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accruvia-harness")
    parser.add_argument("--db", default=None, help="Path to the SQLite database.")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Path for durable run artifacts.",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        help="Path for structured JSONL logs. Defaults under ACCRUVIA_HARNESS_HOME.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the harness database.")
    subparsers.add_parser("config", help="Show resolved harness configuration.")
    subparsers.add_parser("runtime-info", help="Show the configured workflow runtime backend.")
    subparsers.add_parser(
        "run-temporal-worker",
        help="Start a Temporal worker for the configured task queue.",
    )

    create_project = subparsers.add_parser("create-project", help="Create a project.")
    create_project.add_argument("name")
    create_project.add_argument("description")

    create_task = subparsers.add_parser("create-task", help="Create a task.")
    create_task.add_argument("project_id")
    create_task.add_argument("title")
    create_task.add_argument("objective")
    create_task.add_argument("--priority", type=int, default=100)
    create_task.add_argument("--external-ref-type")
    create_task.add_argument("--external-ref-id")
    create_task.add_argument("--strategy", default="default")
    create_task.add_argument("--max-attempts", type=int, default=3)
    create_task.add_argument(
        "--required-artifact",
        action="append",
        dest="required_artifacts",
        default=None,
        help="Repeat to declare required artifact kinds.",
    )

    run_once = subparsers.add_parser("run-once", help="Run one full harness cycle for a task.")
    run_once.add_argument("task_id")

    run_until_stable = subparsers.add_parser(
        "run-until-stable",
        help="Run repeated cycles until a task is completed or failed.",
    )
    run_until_stable.add_argument("task_id")

    process_next = subparsers.add_parser(
        "process-next",
        help="Select the highest-priority pending task and process it until stable.",
    )
    process_next.add_argument("--project-id")
    process_next.add_argument("--worker-id", default="local-worker")
    process_next.add_argument("--lease-seconds", type=int, default=300)

    process_queue = subparsers.add_parser(
        "process-queue",
        help="Process up to N pending tasks in priority order.",
    )
    process_queue.add_argument("limit", type=int)
    process_queue.add_argument("--project-id")
    process_queue.add_argument("--worker-id", default="local-worker")
    process_queue.add_argument("--lease-seconds", type=int, default=300)

    runtime_run = subparsers.add_parser(
        "run-runtime",
        help="Run a task through the configured workflow runtime backend.",
    )
    runtime_run.add_argument("task_id")

    runtime_process_next = subparsers.add_parser(
        "process-next-runtime",
        help="Process the next pending task through the configured workflow runtime backend.",
    )
    runtime_process_next.add_argument("--project-id")
    runtime_process_next.add_argument("--worker-id", default="local-worker")
    runtime_process_next.add_argument("--lease-seconds", type=int, default=300)

    import_issue = subparsers.add_parser(
        "import-issue",
        help="Create a harness task from an external issue reference.",
    )
    import_issue.add_argument("project_id")
    import_issue.add_argument("issue_id")
    import_issue.add_argument("title")
    import_issue.add_argument("objective")
    import_issue.add_argument("--priority", type=int, default=100)
    import_issue.add_argument("--strategy", default="default")
    import_issue.add_argument("--max-attempts", type=int, default=3)
    import_issue.add_argument(
        "--required-artifact",
        action="append",
        dest="required_artifacts",
        default=None,
        help="Repeat to declare required artifact kinds.",
    )

    import_gitlab_issue = subparsers.add_parser(
        "import-gitlab-issue",
        help="Fetch a GitLab issue and create or reuse a linked harness task.",
    )
    import_gitlab_issue.add_argument("project_id")
    import_gitlab_issue.add_argument("repo")
    import_gitlab_issue.add_argument("issue_id")
    import_gitlab_issue.add_argument("--priority", type=int, default=100)
    import_gitlab_issue.add_argument("--strategy", default="default")
    import_gitlab_issue.add_argument("--max-attempts", type=int, default=3)
    import_gitlab_issue.add_argument(
        "--required-artifact",
        action="append",
        dest="required_artifacts",
        default=None,
        help="Repeat to declare required artifact kinds.",
    )

    sync_gitlab = subparsers.add_parser(
        "sync-gitlab-open",
        help="Import or reuse open GitLab issues as linked harness tasks.",
    )
    sync_gitlab.add_argument("project_id")
    sync_gitlab.add_argument("repo")
    sync_gitlab.add_argument("--limit", type=int, default=20)
    sync_gitlab.add_argument("--priority", type=int, default=100)
    sync_gitlab.add_argument("--strategy", default="default")
    sync_gitlab.add_argument("--max-attempts", type=int, default=3)
    sync_gitlab.add_argument(
        "--required-artifact",
        action="append",
        dest="required_artifacts",
        default=None,
        help="Repeat to declare required artifact kinds.",
    )

    report_gitlab = subparsers.add_parser(
        "report-gitlab",
        help="Post a comment back to a GitLab-linked task and optionally close the issue.",
    )
    report_gitlab.add_argument("task_id")
    report_gitlab.add_argument("repo")
    report_gitlab.add_argument("--comment", required=True)
    report_gitlab.add_argument("--close", action="store_true")

    review_promotion = subparsers.add_parser(
        "review-promotion",
        help="Run promotion validation for a completed task run.",
    )
    review_promotion.add_argument("task_id")
    review_promotion.add_argument("--run-id")
    review_promotion.add_argument("--no-follow-on", action="store_true")

    smoke = subparsers.add_parser(
        "smoke-test",
        help="Run a local end-to-end smoke test from project creation through task completion.",
    )
    smoke.add_argument("--project-name", default="smoke-project")
    smoke.add_argument("--task-title", default="Smoke task")
    smoke.add_argument("--objective", default="Verify the local durable loop")

    subparsers.add_parser("status", help="Show projects, tasks, and runs.")
    summary = subparsers.add_parser("summary", help="Show high-level harness summary.")
    summary.add_argument("--project-id")
    context_packet = subparsers.add_parser(
        "context-packet",
        help="Export a compact read-only summary for LLM interrogation.",
    )
    context_packet.add_argument("--project-id")
    task_report = subparsers.add_parser("task-report", help="Show task lineage and evidence.")
    task_report.add_argument("task_id")
    events = subparsers.add_parser("events", help="Show recorded harness events.")
    events.add_argument("--entity-type")
    events.add_argument("--entity-id")
    return parser


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = HarnessConfig.from_env(args.db, args.workspace, args.log_path)
    logger = HarnessLogger(config.log_path)
    logger.log("cli_invoked", command=args.command)

    store = SQLiteHarnessStore(config.db_path)
    store.initialize()
    engine = HarnessEngine(store=store, workspace_root=config.workspace_root)
    engine.set_worker(build_worker(config.worker_backend, config.worker_command))
    gitlab = GitLabCLI()
    runtime = build_runtime(
        backend=config.runtime_backend,
        engine=engine,
        temporal_target=config.temporal_target,
        temporal_namespace=config.temporal_namespace,
        temporal_task_queue=config.temporal_task_queue,
    )
    query_service = HarnessQueryService(store)

    try:
        if args.command == "init-db":
            emit(
                {
                    "db": str(config.db_path),
                    "initialized": True,
                    "schema_version": store.schema_version(),
                    "expected_schema_version": store.expected_schema_version(),
                }
            )
            return

        if args.command == "config":
            emit(
                {
                    "db_path": str(config.db_path),
                    "workspace_root": str(config.workspace_root),
                    "log_path": str(config.log_path),
                    "default_project_name": config.default_project_name,
                    "default_repo": config.default_repo,
                    "runtime_backend": config.runtime_backend,
                    "temporal_target": config.temporal_target,
                    "temporal_namespace": config.temporal_namespace,
                    "temporal_task_queue": config.temporal_task_queue,
                    "worker_backend": config.worker_backend,
                    "worker_command": config.worker_command,
                }
            )
            return

        if args.command == "runtime-info":
            info = runtime.info()
            emit(
                {
                    "backend": info.backend,
                    "available": info.available,
                    "details": info.details,
                }
            )
            return

        if args.command == "run-temporal-worker":
            run_temporal_worker_sync(
                target=config.temporal_target,
                namespace=config.temporal_namespace,
                task_queue=config.temporal_task_queue,
            )
            return

        if args.command == "create-project":
            project = engine.create_project(args.name, args.description)
            emit({"project": serialize_dataclass(project)})
            return

        if args.command == "create-task":
            required_artifacts = args.required_artifacts or ["plan", "report"]
            task = engine.create_task_with_policy(
                args.project_id,
                args.title,
                args.objective,
                args.priority,
                None,
                None,
                args.external_ref_type,
                args.external_ref_id,
                args.strategy,
                args.max_attempts,
                required_artifacts,
            )
            emit({"task": serialize_dataclass(task)})
            return

        if args.command == "run-once":
            run = engine.run_once(args.task_id)
            emit(
                {
                    "run": serialize_dataclass(run),
                    "artifacts": [serialize_dataclass(item) for item in store.list_artifacts(run.id)],
                    "evaluations": [serialize_dataclass(item) for item in store.list_evaluations(run.id)],
                    "decisions": [serialize_dataclass(item) for item in store.list_decisions(run.id)],
                }
            )
            return

        if args.command == "process-next":
            result = engine.process_next_task(
                args.project_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
            if result is None:
                emit({"processed": None})
                return
            emit(
                {
                    "task": serialize_dataclass(result["task"]),
                    "runs": [serialize_dataclass(run) for run in result["runs"]],
                }
            )
            return

        if args.command == "run-runtime":
            result = runtime.run_task_until_stable(args.task_id)
            emit(
                {
                    "task": serialize_dataclass(result["task"]),
                    "runs": [serialize_dataclass(run) for run in result["runs"]],
                }
            )
            return

        if args.command == "process-next-runtime":
            result = runtime.process_next_task(
                args.project_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
            if result is None:
                emit({"processed": None})
                return
            emit(
                {
                    "task": serialize_dataclass(result["task"]),
                    "runs": [serialize_dataclass(run) for run in result["runs"]],
                }
            )
            return

        if args.command == "process-queue":
            results = engine.process_queue(
                args.limit,
                args.project_id,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
            emit(
                {
                    "processed": [
                        {
                            "task": serialize_dataclass(item["task"]),
                            "runs": [serialize_dataclass(run) for run in item["runs"]],
                        }
                        for item in results
                    ]
                }
            )
            return

        if args.command == "import-issue":
            required_artifacts = args.required_artifacts or ["plan", "report"]
            task = engine.import_issue_task(
                project_id=args.project_id,
                issue_id=args.issue_id,
                title=args.title,
                objective=args.objective,
                priority=args.priority,
                strategy=args.strategy,
                max_attempts=args.max_attempts,
                required_artifacts=required_artifacts,
            )
            emit({"task": serialize_dataclass(task)})
            return

        if args.command == "import-gitlab-issue":
            required_artifacts = args.required_artifacts or ["plan", "report"]
            issue = gitlab.fetch_issue(args.repo, args.issue_id)
            task = engine.import_gitlab_issue(
                project_id=args.project_id,
                repo=args.repo,
                issue=issue,
                priority=args.priority,
                strategy=args.strategy,
                max_attempts=args.max_attempts,
                required_artifacts=required_artifacts,
            )
            emit({"task": serialize_dataclass(task)})
            return

        if args.command == "sync-gitlab-open":
            required_artifacts = args.required_artifacts or ["plan", "report"]
            tasks = engine.sync_gitlab_open_issues(
                project_id=args.project_id,
                repo=args.repo,
                gitlab=gitlab,
                limit=args.limit,
                priority=args.priority,
                strategy=args.strategy,
                max_attempts=args.max_attempts,
                required_artifacts=required_artifacts,
            )
            emit({"tasks": [serialize_dataclass(task) for task in tasks]})
            return

        if args.command == "report-gitlab":
            task = engine.report_task_to_gitlab(
                task_id=args.task_id,
                repo=args.repo,
                gitlab=gitlab,
                comment=args.comment,
                close=args.close,
            )
            emit({"task": serialize_dataclass(task), "reported": True, "closed": args.close})
            return

        if args.command == "review-promotion":
            result = engine.review_promotion(
                task_id=args.task_id,
                run_id=args.run_id,
                create_follow_on=not args.no_follow_on,
            )
            emit(
                {
                    "promotion": serialize_dataclass(result.promotion),
                    "follow_on_task_id": result.follow_on_task_id,
                }
            )
            return

        if args.command == "run-until-stable":
            runs = engine.run_until_stable(args.task_id)
            emit(
                {
                    "runs": [serialize_dataclass(run) for run in runs],
                    "task": serialize_dataclass(store.get_task(args.task_id)),
                }
            )
            return

        if args.command == "smoke-test":
            project = engine.create_project(args.project_name, "Local smoke-test project")
            task = engine.create_task_with_policy(
                project.id,
                args.task_title,
                args.objective,
                100,
                None,
                None,
                None,
                None,
                "smoke",
                2,
                ["plan", "report"],
            )
            runs = engine.run_until_stable(task.id)
            final_task = store.get_task(task.id)
            emit(
                {
                    "project": serialize_dataclass(project),
                    "task": serialize_dataclass(final_task),
                    "runs": [serialize_dataclass(run) for run in runs],
                    "events": [
                        serialize_dataclass(item) for item in store.list_events("task", task.id)
                    ],
                }
            )
            return

        if args.command == "status":
            emit(
                {
                    "projects": [serialize_dataclass(item) for item in store.list_projects()],
                    "tasks": [serialize_dataclass(item) for item in store.list_tasks()],
                    "runs": [serialize_dataclass(item) for item in store.list_runs()],
                    "promotions": [serialize_dataclass(item) for item in store.list_promotions()],
                    "leases": [serialize_dataclass(item) for item in store.list_task_leases()],
                    "schema_version": store.schema_version(),
                }
            )
            return

        if args.command == "summary":
            if args.project_id:
                emit(query_service.project_summary(args.project_id))
                return
            emit(query_service.portfolio_summary())
            return

        if args.command == "context-packet":
            emit(query_service.context_packet(args.project_id))
            return

        if args.command == "task-report":
            emit(query_service.task_report(args.task_id))
            return

        if args.command == "events":
            emit(
                {
                    "events": [
                        serialize_dataclass(item)
                        for item in store.list_events(args.entity_type, args.entity_id)
                    ]
                }
            )
            return

        raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        logger.log(
            "cli_error",
            command=args.command,
            error_class=exc.__class__.__name__,
            error_category=classify_error(exc),
            message=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
