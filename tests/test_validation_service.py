from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.domain import Run, RunStatus, Task, new_id
from accruvia_harness.policy import WorkResult
from accruvia_harness.services.validation_service import ValidationService


class ValidationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.workspace_root = self.base / "workspace-root"
        self.workspace_root.mkdir()
        self.project_workspace = self.base / "project-workspace"
        self.project_workspace.mkdir()
        self.run = Run(
            id=new_id("run"),
            task_id=new_id("task"),
            status=RunStatus.VALIDATING,
            attempt=1,
            summary="candidate",
        )
        self.task = Task(
            id=self.run.task_id,
            project_id=new_id("project"),
            title="Validation service task",
            objective="Persist deterministic validation evidence",
        )
        self.run_dir = self.workspace_root / "runs" / self.run.id
        self.run_dir.mkdir(parents=True)

    def test_validate_persists_validation_exit_code_and_propagates_report_evidence(self) -> None:
        report_path = self.run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "worker_outcome": "success",
                    "changed_files": ["src/example.py", "tests/test_example.py"],
                    "test_files": ["tests/test_example.py"],
                    "compile_check": {"passed": True},
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        service = ValidationService(store=None, workspace_root=self.workspace_root)  # type: ignore[arg-type]
        work_result = WorkResult(
            summary="candidate",
            artifacts=[("report", str(report_path), "Structured report")],
            outcome="success",
            diagnostics={"worker_outcome": "candidate", "project_workspace": str(self.project_workspace)},
        )

        with patch("accruvia_harness.services.validation_service.run_validation", return_value=0):
            result = service.validate(self.task, self.run, work_result, self.workspace_root)

        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(0, persisted["validation_exit_code"])
        self.assertEqual(["src/example.py", "tests/test_example.py"], result.diagnostics["changed_files"])
        self.assertEqual(["tests/test_example.py"], result.diagnostics["test_files"])
        self.assertEqual(0, result.diagnostics["validation_exit_code"])

    def test_validate_uses_worker_project_workspace_for_compile_and_tests(self) -> None:
        report_path = self.run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "worker_outcome": "candidate",
                    "changed_files": ["example_module.py", "tests/test_example_module.py"],
                    "test_files": ["tests/test_example_module.py"],
                }
            ),
            encoding="utf-8",
        )
        service = ValidationService(store=None, workspace_root=self.workspace_root)  # type: ignore[arg-type]
        work_result = WorkResult(
            summary="candidate",
            artifacts=[("report", str(report_path), "Structured report")],
            outcome="success",
            diagnostics={"worker_outcome": "candidate", "project_workspace": str(self.project_workspace)},
        )

        def _fake_run_validation(environ: dict[str, str]) -> int:
            self.assertEqual(str(self.project_workspace), environ["ACCRUVIA_PROJECT_WORKSPACE"])
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload.update(
                {
                    "worker_outcome": "success",
                    "compile_check": {"passed": True},
                    "test_check": {"passed": True},
                }
            )
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            return 0

        with patch("accruvia_harness.services.validation_service.run_validation", side_effect=_fake_run_validation):
            result = service.validate(self.task, self.run, work_result, self.workspace_root)

        self.assertEqual("success", result.outcome)
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(persisted["compile_check"]["passed"])
        self.assertTrue(persisted["test_check"]["passed"])
        self.assertEqual(str(self.project_workspace), result.diagnostics["project_workspace"])
