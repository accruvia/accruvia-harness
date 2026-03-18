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

    def validate(self, task: Task, run: Run, work_result: WorkResult, workspace_path: Path) -> dict[str, object]:
        """Run compile+test on candidate. Returns updated report dict with validation results."""
        run_dir = self.workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)

        environ = {
            "ACCRUVIA_RUN_DIR": str(run_dir),
            "ACCRUVIA_PROJECT_WORKSPACE": str(workspace_path),
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
        return report
