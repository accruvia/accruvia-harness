from __future__ import annotations

import json
from pathlib import Path

from ..agent_worker import run_validation
from ..domain import Run, Task
from ..policy import WorkResult
from ..store import SQLiteHarnessStore


class ValidationService:
    def __init__(self, store: SQLiteHarnessStore, workspace_root: Path, telemetry=None) -> None:
        self.store = store
        self.workspace_root = workspace_root
        self.telemetry = telemetry

    def validate(self, task: Task, run: Run, work_result: WorkResult, workspace_path: Path) -> WorkResult:
        """Run compile+test on candidate and return an updated WorkResult."""
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        diagnostics = dict(work_result.diagnostics or {})
        project_workspace = Path(
            str(diagnostics.get("project_workspace") or workspace_path)
        )

        environ = {
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(project_workspace),
            "ACCRUVIA_TASK_ID": task.id,
            "ACCRUVIA_RUN_ID": run.id,
            "ACCRUVIA_TASK_VALIDATION_MODE": task.validation_mode,
        }

        if self.telemetry is not None:
            with self.telemetry.timed(
                "validation",
                task_id=task.id,
                run_id=run.id,
                validation_profile=task.validation_profile,
            ):
                exit_code = run_validation(environ)
        else:
            exit_code = run_validation(environ)

        report_path = run_dir / "report.json"
        report: dict[str, object] = {}
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = {}

        report["validation_exit_code"] = exit_code
        if report:
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        diagnostics.update(
            {
                "worker_outcome": str(report.get("worker_outcome") or work_result.outcome),
                "project_workspace": str(project_workspace),
                "changed_files": report.get("changed_files", diagnostics.get("changed_files", [])),
                "test_files": report.get("test_files", diagnostics.get("test_files", [])),
                "compile_check": report.get("compile_check"),
                "test_check": report.get("test_check"),
                "validation_elapsed_seconds": report.get("validation_elapsed_seconds"),
                "validation_exit_code": exit_code,
                "failure_category": report.get("failure_category") or diagnostics.get("failure_category"),
                "failure_message": report.get("failure_message") or diagnostics.get("failure_message"),
            }
        )
        outcome = str(report.get("worker_outcome") or work_result.outcome or "success")
        return WorkResult(
            summary=work_result.summary,
            artifacts=list(work_result.artifacts),
            outcome=outcome,
            diagnostics=diagnostics,
        )
