"""Calls harness CLI commands and parses JSON responses."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass(slots=True)
class QueryResult:
    command: str
    data: dict | list | None = None
    error: str | None = None
    ok: bool = True


class HarnessQueryClient:
    """Read-only query client that shells out to the harness CLI."""

    def __init__(
        self,
        cli_command: str = "accruvia-harness",
        db_path: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.cli_command = cli_command
        self.db_path = db_path
        self.timeout_seconds = timeout_seconds

    def _run(self, *args: str) -> QueryResult:
        cmd = [self.cli_command]
        if self.db_path:
            cmd.extend(["--db", self.db_path])
        cmd.extend(args)
        command_str = " ".join(args)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return QueryResult(command=command_str, error="Query timed out", ok=False)
        except FileNotFoundError:
            return QueryResult(command=command_str, error=f"CLI not found: {self.cli_command}", ok=False)
        if completed.returncode != 0:
            return QueryResult(command=command_str, error=completed.stderr.strip(), ok=False)
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return QueryResult(command=command_str, error="Invalid JSON response", ok=False)
        return QueryResult(command=command_str, data=data)

    def context_packet(self, project_id: str | None = None) -> QueryResult:
        args = ["context-packet"]
        if project_id:
            args.extend(["--project-id", project_id])
        return self._run(*args)

    def ops_report(self, project_id: str | None = None) -> QueryResult:
        args = ["ops-report"]
        if project_id:
            args.extend(["--project-id", project_id])
        return self._run(*args)

    def task_report(self, task_id: str) -> QueryResult:
        return self._run("task-report", task_id)

    def lineage_report(self, task_id: str) -> QueryResult:
        return self._run("lineage-report", task_id)

    def summary(self, project_id: str | None = None) -> QueryResult:
        args = ["summary"]
        if project_id:
            args.extend(["--project-id", project_id])
        return self._run(*args)

    def status(self) -> QueryResult:
        return self._run("status")

    def events(self, entity_type: str | None = None, entity_id: str | None = None) -> QueryResult:
        args = ["events"]
        if entity_type:
            args.extend(["--entity-type", entity_type])
        if entity_id:
            args.extend(["--entity-id", entity_id])
        return self._run(*args)

    def telemetry_report(self) -> QueryResult:
        return self._run("telemetry-report")
