from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accruvia_harness.domain import ContextRecord, Objective, Project, Task, new_id
from accruvia_harness.evidence.collector import LocalEvidenceCollector
from accruvia_harness.store import SQLiteHarnessStore


class LocalEvidenceCollectorTests(unittest.TestCase):
    def test_git_evidence_returns_success_with_git_log(self) -> None:
        collector = LocalEvidenceCollector()

        def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[:3] == ["git", "log", "--oneline"]:
                return subprocess.CompletedProcess(command, 0, stdout="abc123 test commit\n", stderr="")
            if command[:3] == ["git", "diff", "HEAD~1"]:
                return subprocess.CompletedProcess(command, 0, stdout=" src/file.py | 2 +-\n", stderr="")
            raise AssertionError(f"Unexpected command: {command}")

        with patch.object(LocalEvidenceCollector, "_run_command", side_effect=fake_run):
            result = collector.collect("objective-1", "devops_evidence")

        self.assertTrue(result.success)
        self.assertIn("git_log", result.content)
        self.assertIn("abc123", result.content["git_log"])

    def test_workflow_evidence_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "harness.db"
            store = SQLiteHarnessStore(db_path)
            store.initialize()

            project = Project(id=new_id("project"), name="demo", description="demo")
            store.create_project(project)
            objective = Objective(
                id=new_id("objective"),
                project_id=project.id,
                title="Objective",
                summary="Collect workflow evidence",
            )
            store.create_objective(objective)
            task = Task(
                id=new_id("task"),
                project_id=project.id,
                objective_id=objective.id,
                title="Implement collector",
                objective="Add local evidence",
            )
            store.create_task(task)
            record = ContextRecord(
                id=new_id("context"),
                record_type="operator_comment",
                project_id=project.id,
                objective_id=objective.id,
                content="Workflow evidence is local",
            )
            store.create_context_record(record)

            result = LocalEvidenceCollector(str(db_path)).collect(objective.id, "workflow_implementation_evidence")

        self.assertTrue(result.success)
        self.assertEqual(objective.id, result.content["objective_id"])
        self.assertEqual(1, len(result.content["context_records"]))
        self.assertEqual(1, len(result.content["tasks"]))

    def test_unknown_type_returns_failure(self) -> None:
        result = LocalEvidenceCollector().collect("objective-1", "unknown_artifact")

        self.assertFalse(result.success)
        self.assertIn("Unsupported artifact type", result.error or "")


if __name__ == "__main__":
    unittest.main()
