from __future__ import annotations

from ..domain import serialize_dataclass
from .common import CLIContext, emit


def handle_issue_command(args, ctx: CLIContext) -> bool:
    engine = ctx.engine
    if args.command == "import-issue":
        required_artifacts = args.required_artifacts or ["plan", "report"]
        task = engine.import_issue_task(
            args.project_id,
            args.issue_id,
            args.title,
            args.objective,
            args.priority,
            args.validation_profile,
            args.strategy,
            args.max_attempts,
            required_artifacts,
        )
        emit({"task": serialize_dataclass(task)})
        return True
    if args.command in {"import-github-issue", "import-gitlab-issue"}:
        required_artifacts = args.required_artifacts or ["plan", "report"]
        provider = ctx.github if args.command == "import-github-issue" else ctx.gitlab
        issue = provider.fetch_issue(args.repo, args.issue_id)
        task = (
            engine.import_github_issue(
                args.project_id,
                args.repo,
                issue,
                args.priority,
                args.validation_profile,
                args.strategy,
                args.max_attempts,
                required_artifacts,
            )
            if args.command == "import-github-issue"
            else engine.import_gitlab_issue(
                args.project_id,
                args.repo,
                issue,
                args.priority,
                args.validation_profile,
                args.strategy,
                args.max_attempts,
                required_artifacts,
            )
        )
        emit({"task": serialize_dataclass(task)})
        return True
    if args.command in {"sync-github-open", "sync-gitlab-open"}:
        required_artifacts = args.required_artifacts or ["plan", "report"]
        tasks = (
            engine.sync_github_open_issues(
                args.project_id,
                args.repo,
                ctx.github,
                args.limit,
                args.priority,
                args.validation_profile,
                args.strategy,
                args.max_attempts,
                required_artifacts,
            )
            if args.command == "sync-github-open"
            else engine.sync_gitlab_open_issues(
                args.project_id,
                args.repo,
                ctx.gitlab,
                args.limit,
                args.priority,
                args.validation_profile,
                args.strategy,
                args.max_attempts,
                required_artifacts,
            )
        )
        emit({"tasks": [serialize_dataclass(t) for t in tasks]})
        return True
    if args.command in {"report-github", "report-gitlab"}:
        task = engine.report_task_to_github(args.task_id, args.repo, ctx.github, args.comment, args.close) if args.command == "report-github" else engine.report_task_to_gitlab(args.task_id, args.repo, ctx.gitlab, args.comment, args.close)
        emit({"task": serialize_dataclass(task), "reported": True, "closed": args.close})
        return True
    if args.command in {"sync-github-state", "sync-gitlab-state"}:
        task = engine.sync_github_issue_state(args.task_id, args.repo, ctx.github) if args.command == "sync-github-state" else engine.sync_gitlab_issue_state(args.task_id, args.repo, ctx.gitlab)
        emit({"task": serialize_dataclass(task), "synced": True})
        return True
    if args.command in {"sync-github-metadata", "sync-gitlab-metadata"}:
        task = engine.sync_github_issue_metadata(args.task_id, args.repo, ctx.github) if args.command == "sync-github-metadata" else engine.sync_gitlab_issue_metadata(args.task_id, args.repo, ctx.gitlab)
        emit({"task": serialize_dataclass(task), "synced": True})
        return True
    return False
