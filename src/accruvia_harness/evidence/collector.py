from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover - fallback for minimal sandbox environments
    import fcntl

    class FileLock:  # type: ignore[override]
        def __init__(self, lock_file: str) -> None:
            self._lock_file = lock_file
            self._handle: Any | None = None

        def __enter__(self) -> "FileLock":
            path = Path(self._lock_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("w", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            if self._handle is None:
                return
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

from ..store import SQLiteHarnessStore


@dataclass
class EvidenceResult:
    artifact_type: str
    content: dict[str, Any]
    source: str
    collected_at: str
    success: bool
    error: str | None = None


class LocalEvidenceCollector:
    def __init__(self, db_path: str | None = None) -> None:
        configured = db_path or os.environ.get("HARNESS_DB") or ".accruvia-harness/harness.db"
        self.db_path = Path(configured)

    def collect(self, objective_id: str, artifact_type: str) -> EvidenceResult:
        collected_at = datetime.now(UTC).isoformat()
        try:
            if artifact_type in {"test_execution_report", "integration_test_report"}:
                return self._collect_test_execution_report(artifact_type, collected_at)
            if artifact_type == "unit_test_coverage":
                return self._collect_unit_test_coverage(artifact_type, collected_at)
            if artifact_type == "workflow_implementation_evidence":
                return self._collect_workflow_implementation_evidence(objective_id, artifact_type, collected_at)
            if artifact_type in {"devops_evidence", "deployment_evidence"}:
                return self._collect_git_evidence(artifact_type, collected_at)
            return EvidenceResult(
                artifact_type=artifact_type,
                content={},
                source="local",
                collected_at=collected_at,
                success=False,
                error=f"Unsupported artifact type: {artifact_type}",
            )
        except Exception as exc:
            return EvidenceResult(
                artifact_type=artifact_type,
                content={},
                source="local",
                collected_at=collected_at,
                success=False,
                error=str(exc),
            )

    def _collect_test_execution_report(self, artifact_type: str, collected_at: str) -> EvidenceResult:
        with FileLock("/tmp/harness-test.lock"):
            completed = self._run_command([sys.executable, "-m", "pytest", "-q", "--tb=short", "tests/"])
        return EvidenceResult(
            artifact_type=artifact_type,
            content={
                "command": [sys.executable, "-m", "pytest", "-q", "--tb=short", "tests/"],
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
            },
            source="local_pytest",
            collected_at=collected_at,
            success=completed.returncode == 0,
            error=None if completed.returncode == 0 else f"pytest exited with code {completed.returncode}",
        )

    def _collect_unit_test_coverage(self, artifact_type: str, collected_at: str) -> EvidenceResult:
        with FileLock("/tmp/harness-test.lock"):
            completed = self._run_command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--cov=accruvia_harness",
                    "--cov-report=json",
                    "tests/",
                ]
            )
        coverage_path = Path("coverage.json")
        coverage_content = json.loads(coverage_path.read_text(encoding="utf-8"))
        return EvidenceResult(
            artifact_type=artifact_type,
            content={
                "command": [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--cov=accruvia_harness",
                    "--cov-report=json",
                    "tests/",
                ],
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
                "coverage": coverage_content,
            },
            source="local_pytest_cov",
            collected_at=collected_at,
            success=completed.returncode == 0,
            error=None if completed.returncode == 0 else f"pytest exited with code {completed.returncode}",
        )

    def _collect_workflow_implementation_evidence(
        self,
        objective_id: str,
        artifact_type: str,
        collected_at: str,
    ) -> EvidenceResult:
        store = SQLiteHarnessStore(self.db_path)
        records = store.list_context_records(objective_id=objective_id)
        objective = store.get_objective(objective_id)
        tasks = store.list_tasks(objective.project_id) if objective is not None else []
        linked_tasks = [task for task in tasks if task.objective_id == objective_id]
        return EvidenceResult(
            artifact_type=artifact_type,
            content={
                "objective_id": objective_id,
                "context_records": [self._serialize_context_record(record) for record in records],
                "tasks": [self._serialize_task(task) for task in linked_tasks],
            },
            source=str(self.db_path),
            collected_at=collected_at,
            success=True,
            error=None,
        )

    def _collect_git_evidence(self, artifact_type: str, collected_at: str) -> EvidenceResult:
        git_log = self._run_command(["git", "log", "--oneline", "-10"])
        git_diff = self._run_command(["git", "diff", "HEAD~1", "--stat"])
        success = git_log.returncode == 0 and git_diff.returncode == 0
        errors = []
        if git_log.returncode != 0:
            errors.append(f"git log exited with code {git_log.returncode}")
        if git_diff.returncode != 0:
            errors.append(f"git diff exited with code {git_diff.returncode}")
        return EvidenceResult(
            artifact_type=artifact_type,
            content={
                "git_log": git_log.stdout,
                "git_log_stderr": git_log.stderr,
                "git_log_exit_code": git_log.returncode,
                "git_diff_stat": git_diff.stdout,
                "git_diff_stderr": git_diff.stderr,
                "git_diff_exit_code": git_diff.returncode,
            },
            source="git",
            collected_at=collected_at,
            success=success,
            error=None if success else "; ".join(errors),
        )

    @staticmethod
    def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @staticmethod
    def _serialize_context_record(record: Any) -> dict[str, Any]:
        data = asdict(record)
        data["created_at"] = record.created_at.isoformat()
        return data

    @staticmethod
    def _serialize_task(task: Any) -> dict[str, Any]:
        data = asdict(task)
        data["status"] = task.status.value
        data["created_at"] = task.created_at.isoformat()
        data["updated_at"] = task.updated_at.isoformat()
        return data
