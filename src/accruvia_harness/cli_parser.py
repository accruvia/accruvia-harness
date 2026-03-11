from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accruvia-harness")
    parser.add_argument("--db", default=None, help="Path to the SQLite database.")
    parser.add_argument("--workspace", default=None, help="Path for durable run artifacts.")
    parser.add_argument("--log-path", default=None, help="Path for structured JSONL logs. Defaults under ACCRUVIA_HARNESS_HOME.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the harness database.")
    subparsers.add_parser("config", help="Show resolved harness configuration.")
    subparsers.add_parser("runtime-info", help="Show the configured workflow runtime backend.")
    subparsers.add_parser("run-temporal-worker", help="Start a Temporal worker for the configured task queue.")

    create_project = subparsers.add_parser("create-project", help="Create a project.")
    create_project.add_argument("name")
    create_project.add_argument("description")
    create_project.add_argument("--adapter-name", default="generic")

    create_task = subparsers.add_parser("create-task", help="Create a task.")
    create_task.add_argument("project_id")
    create_task.add_argument("title")
    create_task.add_argument("objective")
    create_task.add_argument("--priority", type=int, default=100)
    create_task.add_argument("--external-ref-type")
    create_task.add_argument("--external-ref-id")
    create_task.add_argument("--validation-profile", default="generic")
    create_task.add_argument("--strategy", default="default")
    create_task.add_argument("--max-attempts", type=int, default=3)
    create_task.add_argument("--max-branches", type=int, default=1)
    create_task.add_argument("--required-artifact", action="append", dest="required_artifacts", default=None)

    run_once = subparsers.add_parser("run-once", help="Run one full harness cycle for a task.")
    run_once.add_argument("task_id")

    run_until_stable = subparsers.add_parser("run-until-stable", help="Run repeated cycles until a task is completed or failed.")
    run_until_stable.add_argument("task_id")

    process_next = subparsers.add_parser("process-next", help="Select the highest-priority pending task and process it until stable.")
    process_next.add_argument("--project-id")
    process_next.add_argument("--worker-id", default="local-worker")
    process_next.add_argument("--lease-seconds", type=int, default=300)

    process_queue = subparsers.add_parser("process-queue", help="Process up to N pending tasks in priority order.")
    process_queue.add_argument("limit", type=int)
    process_queue.add_argument("--project-id")
    process_queue.add_argument("--worker-id", default="local-worker")
    process_queue.add_argument("--lease-seconds", type=int, default=300)

    runtime_run = subparsers.add_parser("run-runtime", help="Run a task through the configured workflow runtime backend.")
    runtime_run.add_argument("task_id")

    runtime_process_next = subparsers.add_parser("process-next-runtime", help="Process the next pending task through the configured workflow runtime backend.")
    runtime_process_next.add_argument("--project-id")
    runtime_process_next.add_argument("--worker-id", default="local-worker")
    runtime_process_next.add_argument("--lease-seconds", type=int, default=300)

    import_issue = subparsers.add_parser("import-issue", help="Create a harness task from an external issue reference.")
    import_issue.add_argument("project_id")
    import_issue.add_argument("issue_id")
    import_issue.add_argument("title")
    import_issue.add_argument("objective")
    import_issue.add_argument("--priority", type=int, default=100)
    import_issue.add_argument("--validation-profile", default="generic")
    import_issue.add_argument("--strategy", default="default")
    import_issue.add_argument("--max-attempts", type=int, default=3)
    import_issue.add_argument("--required-artifact", action="append", dest="required_artifacts", default=None)

    for name, help_text in [
        ("import-github-issue", "Fetch a GitHub issue and create or reuse a linked harness task."),
        ("import-gitlab-issue", "Fetch a GitLab issue and create or reuse a linked harness task."),
    ]:
        cmd = subparsers.add_parser(name, help=help_text)
        cmd.add_argument("project_id")
        cmd.add_argument("repo")
        cmd.add_argument("issue_id")
        cmd.add_argument("--priority", type=int, default=100)
        cmd.add_argument("--validation-profile", default="generic")
        cmd.add_argument("--strategy", default="default")
        cmd.add_argument("--max-attempts", type=int, default=3)
        cmd.add_argument("--required-artifact", action="append", dest="required_artifacts", default=None)

    for name, help_text in [
        ("sync-github-open", "Import or reuse open GitHub issues as linked harness tasks."),
        ("sync-gitlab-open", "Import or reuse open GitLab issues as linked harness tasks."),
    ]:
        cmd = subparsers.add_parser(name, help=help_text)
        cmd.add_argument("project_id")
        cmd.add_argument("repo")
        cmd.add_argument("--limit", type=int, default=20)
        cmd.add_argument("--priority", type=int, default=100)
        cmd.add_argument("--validation-profile", default="generic")
        cmd.add_argument("--strategy", default="default")
        cmd.add_argument("--max-attempts", type=int, default=3)
        cmd.add_argument("--required-artifact", action="append", dest="required_artifacts", default=None)

    for name, help_text in [
        ("report-github", "Post a comment back to a GitHub-linked task and optionally close the issue."),
        ("report-gitlab", "Post a comment back to a GitLab-linked task and optionally close the issue."),
    ]:
        cmd = subparsers.add_parser(name, help=help_text)
        cmd.add_argument("task_id")
        cmd.add_argument("repo")
        cmd.add_argument("--comment")
        cmd.add_argument("--close", action="store_const", const=True, default=None)

    sync_github_state = subparsers.add_parser("sync-github-state", help="Synchronize a GitHub-linked issue state from the current harness task status.")
    sync_github_state.add_argument("task_id")
    sync_github_state.add_argument("repo")
    sync_gitlab_state = subparsers.add_parser("sync-gitlab-state", help="Synchronize a GitLab-linked issue state from the current harness task status.")
    sync_gitlab_state.add_argument("task_id")
    sync_gitlab_state.add_argument("repo")
    sync_github_metadata = subparsers.add_parser("sync-github-metadata", help="Synchronize labels, milestone, and assignees from a GitHub issue into task metadata.")
    sync_github_metadata.add_argument("task_id")
    sync_github_metadata.add_argument("repo")
    sync_gitlab_metadata = subparsers.add_parser("sync-gitlab-metadata", help="Synchronize labels, milestone, and assignees from a GitLab issue into task metadata.")
    sync_gitlab_metadata.add_argument("task_id")
    sync_gitlab_metadata.add_argument("repo")

    review = subparsers.add_parser("review-promotion", help="Run promotion validation for a completed task run.")
    review.add_argument("task_id")
    review.add_argument("--run-id")
    review.add_argument("--no-follow-on", action="store_true")

    affirm = subparsers.add_parser("affirm-promotion", help="Ask the configured LLM executor to affirm or reject a pending promotion.")
    affirm.add_argument("task_id")
    affirm.add_argument("--run-id")
    affirm.add_argument("--promotion-id")
    affirm.add_argument("--no-follow-on", action="store_true")

    rereview = subparsers.add_parser("rereview-promotion", help="Re-review a previously failed promotion using a remediation task run.")
    rereview.add_argument("task_id")
    rereview.add_argument("remediation_task_id")
    rereview.add_argument("--remediation-run-id")
    rereview.add_argument("--base-promotion-id")
    rereview.add_argument("--no-follow-on", action="store_true")

    smoke = subparsers.add_parser("smoke-test", help="Run a local end-to-end smoke test from project creation through task completion.")
    smoke.add_argument("--project-name", default="smoke-project")
    smoke.add_argument("--task-title", default="Smoke task")
    smoke.add_argument("--objective", default="Verify the local durable loop")

    subparsers.add_parser("status", help="Show projects, tasks, runs, and promotions.")
    summary = subparsers.add_parser("summary", help="Show high-level harness summary.")
    summary.add_argument("--project-id")
    context_packet = subparsers.add_parser("context-packet", help="Export a compact read-only summary for LLM interrogation.")
    context_packet.add_argument("--project-id")
    ops_report = subparsers.add_parser("ops-report", help="Show operational backlog and profile-aware promotion metrics.")
    ops_report.add_argument("--project-id")
    subparsers.add_parser("telemetry-report", help="Show aggregated telemetry counters and span timings.")
    dashboard_report = subparsers.add_parser("dashboard-report", help="Show a small operational dashboard export.")
    dashboard_report.add_argument("--project-id")
    explain_system = subparsers.add_parser("explain-system", help="Use the configured LLM executor to explain the current system state from read-only evidence.")
    explain_system.add_argument("--project-id")
    lineage_report = subparsers.add_parser("lineage-report", help="Show ancestors and spawned child tasks for a task.")
    lineage_report.add_argument("task_id")
    task_report = subparsers.add_parser("task-report", help="Show task lineage and evidence.")
    task_report.add_argument("task_id")
    explain_task = subparsers.add_parser("explain-task", help="Use the configured LLM executor to explain a task from read-only evidence.")
    explain_task.add_argument("task_id")
    events = subparsers.add_parser("events", help="Show recorded harness events.")
    events.add_argument("--entity-type")
    events.add_argument("--entity-id")
    return parser
