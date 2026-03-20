from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.config import HarnessConfig
from accruvia_harness.domain import (
    ContextRecord,
    DecisionAction,
    IntentModel,
    MermaidArtifact,
    MermaidStatus,
    Objective,
    ObjectiveStatus,
    Project,
    PromotionMode,
    RepoProvider,
    Run,
    RunStatus,
    TaskStatus,
    WorkspacePolicy,
    new_id,
)
from accruvia_harness.engine import HarnessEngine
from accruvia_harness.llm import build_llm_router
from accruvia_harness.policy import DecideResult, WorkResult
from accruvia_harness.services.promotion_service import PromotionService
from accruvia_harness.services.repository_promotion_service import RepositoryPromotionService
from accruvia_harness.services.objective_promotion_service import ObjectivePromotionService
from accruvia_harness.project_adapters import ProjectAdapterRegistry
from accruvia_harness.workers import LocalArtifactWorker
from accruvia_harness.store import SQLiteHarnessStore


class MissingArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan only\n", encoding="utf-8")
        return WorkResult(
            summary="Recorded only a partial artifact set.",
            artifacts=[("plan", str(plan_path), "Plan artifact only")],
        )


class PromotionBlockedWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan\n", encoding="utf-8")
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "changed_files": ["src/accruvia_client/runner.py"],
                    "test_files": [],
                    "compile_check": {"passed": True},
                    "test_check": {"passed": False},
                    "promotion_blocked": True,
                    "promotion_block_reason": "Generated candidate lacks required test coverage.",
                    "follow_on_title": "Add missing test coverage",
                    "follow_on_objective": "Add the missing tests and regenerate the candidate.",
                }
            ),
            encoding="utf-8",
        )
        return WorkResult(
            summary="Recorded blocked promotion artifacts.",
            artifacts=[
                ("plan", str(plan_path), "Plan artifact"),
                ("report", str(report_path), "Blocked report artifact"),
            ],
        )


class BlockedDiagnosisWorker(PromotionBlockedWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "blocked"
        result.diagnostics = {"blocked_reason": "Generated candidate lacks required test coverage."}
        return result


class FailedDiagnosisWorker(PromotionBlockedWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "failed"
        result.diagnostics = {"timed_out": True, "blocked_reason": "Worker executor timed out."}
        return result


class ValidationTimeoutWorker(PromotionBlockedWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "failed"
        result.diagnostics = {
            "failure_category": "validation_timeout",
            "failure_message": "Focused validation exceeded the bounded ceiling.",
            "timeout_seconds": 300,
        }
        return result


class AtomicityBlockedWorker(PromotionBlockedWorker):
    def __init__(self, category: str) -> None:
        super().__init__()
        self.category = category

    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "blocked"
        result.diagnostics = {
            "failure_category": self.category,
            "failure_message": "Atomicity gate rejected the attempt before validation.",
        }
        return result


class InfrastructureBlockedWorker(PromotionBlockedWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "blocked"
        result.diagnostics = {
            "infrastructure_failure": True,
            "failure_category": "executor_process_failure",
            "failure_message": "Worker bootstrap crashed before producing artifacts.",
        }
        return result


class InfrastructureFailedWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        return WorkResult(
            summary="Executor crashed before durable artifacts were written.",
            artifacts=[],
            outcome="failed",
            diagnostics={
                "infrastructure_failure": True,
                "failure_category": "executor_process_failure",
                "failure_message": "Worker bootstrap crashed before producing artifacts.",
            },
        )


class ScopeSplitWorker(PromotionBlockedWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        result = super().work(task, run, workspace_root)
        result.outcome = "blocked"
        result.diagnostics = {
            "scope_violation": {
                "outside_allowed_paths": ["tests/test_boundary.py"],
                "forbidden_path_hits": ["tests/test_boundary.py"],
            }
        }
        return result


class ManifestProjectAdapter:
    name = "manifest"

    def prepare_workspace(self, project, task, run, run_dir: Path):
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        manifest = workspace / "custom-manifest.txt"
        manifest.write_text("custom workspace prepared\n", encoding="utf-8")
        from accruvia_harness.project_adapters import ProjectWorkspace

        return ProjectWorkspace(
            project_root=workspace,
            metadata_files=[manifest],
            environment={"ACCRUVIA_PROJECT_WORKSPACE": str(workspace)},
            diagnostics={"project_adapter": self.name},
        )


class CandidateArtifactWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        project_workspace = run_dir / "workspace"
        project_workspace.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("plan\n", encoding="utf-8")
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "worker_outcome": "candidate",
                    "changed_files": ["src/demo.py"],
                    "compile_check": {"passed": True},
                    "test_files": ["tests/test_demo.py"],
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        return WorkResult(
            summary="Recorded candidate artifacts.",
            artifacts=[
                ("plan", str(plan_path), "Plan artifact"),
                ("report", str(report_path), "Candidate report artifact"),
            ],
            diagnostics={"worker_outcome": "candidate"},
        )

    def build_worker(self, project, task, run, workspace, default_worker):
        return None


class ProjectOverrideWorker(LocalArtifactWorker):
    def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
        run_dir = workspace_root / "runs" / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.txt"
        plan_path.write_text("override plan\n", encoding="utf-8")
        report_path = run_dir / "report.json"
        report_path.write_text(
            json.dumps({"worker_backend": "project_override", "worker_outcome": "success"}),
            encoding="utf-8",
        )
        return WorkResult(
            summary="Used project-specific worker override.",
            artifacts=[
                ("plan", str(plan_path), "Override plan"),
                ("report", str(report_path), "Override report"),
            ],
        )


class OverrideProjectAdapter(ManifestProjectAdapter):
    name = "override"

    def build_worker(self, project, task, run, workspace, default_worker):
        return ProjectOverrideWorker()


class SharedRepoAdapter:
    name = "shared"

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def prepare_workspace(self, project, task, run, run_dir: Path):
        from accruvia_harness.project_adapters import ProjectWorkspace

        return ProjectWorkspace(
            project_root=self.repo_root,
            workspace_mode="shared_repo",
            source_repo_root=self.repo_root,
            environment={"ACCRUVIA_PROJECT_WORKSPACE": str(self.repo_root)},
            diagnostics={"project_adapter": self.name},
        )

    def build_worker(self, project, task, run, workspace, default_worker):
        return None


class GitBranchAdapter:
    name = "gitbranch"

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def prepare_workspace(self, project, task, run, run_dir: Path):
        from accruvia_harness.project_adapters import ProjectWorkspace

        workspace = run_dir / "workspace"
        branch = f"harness-{task.id}-{run.id}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(workspace), "HEAD"],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return ProjectWorkspace(
            project_root=workspace,
            workspace_mode="git_worktree",
            source_repo_root=self.repo_root,
            branch_name=branch,
            environment={"ACCRUVIA_PROJECT_WORKSPACE": str(workspace)},
            diagnostics={"project_adapter": self.name},
        )

    def build_worker(self, project, task, run, workspace, default_worker):
        return None


class BranchOnceDecider:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, analysis, run, task):
        self.calls += 1
        if self.calls == 1:
            return DecideResult(
                action=DecisionAction.BRANCH,
                rationale="Branch for speculative resolution.",
            )
        return DecideResult(
            action=DecisionAction.PROMOTE,
            rationale="Promote after branch winner selection.",
        )


class HarnessEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.store = SQLiteHarnessStore(base / "harness.db")
        self.store.initialize()
        self.engine = HarnessEngine(
            store=self.store,
            workspace_root=base / "workspace",
        )
        project = Project(id=new_id("project"), name="accruvia", description="Harness work")
        self.store.create_project(project)
        self.project_id = project.id

    def _init_git_repo(self, repo_root: Path, *, with_remote: bool = False) -> Path | None:
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "harness@test.local"], cwd=repo_root, check=True, capture_output=True, text=True)
        remote_root = None
        if with_remote:
            remote_root = repo_root.parent / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote_root)], cwd=repo_root.parent, check=True, capture_output=True, text=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_root)], cwd=repo_root, check=True, capture_output=True, text=True)
        return remote_root

    def test_run_once_completes_when_required_artifacts_exist(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Build first loop",
            objective="Produce required artifacts",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="456",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        run = self.engine.run_once(task.id)
        artifacts = self.store.list_artifacts(run.id)
        evaluations = self.store.list_evaluations(run.id)
        decisions = self.store.list_decisions(run.id)
        task_after = self.store.get_task(task.id)

        self.assertEqual("completed", run.status.value)
        self.assertEqual(["plan", "report", "workspace_metadata"], sorted(artifact.kind for artifact in artifacts))
        self.assertEqual("acceptable", evaluations[0].verdict)
        self.assertEqual("promote", decisions[0].action.value)
        assert task_after is not None
        self.assertEqual("gitlab_issue", task_after.external_ref_type)
        self.assertEqual("456", task_after.external_ref_id)
        self.assertEqual("completed", task_after.status.value)

    def test_run_once_prepares_project_workspace_before_work(self) -> None:
        registry = ProjectAdapterRegistry()
        registry.register(ManifestProjectAdapter())
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-manifest",
            project_adapter_registry=registry,
        )
        project = Project(
            id=new_id("project"),
            name="manifest-project",
            description="Uses custom project adapter",
            adapter_name="manifest",
        )
        self.store.create_project(project)
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Prepare workspace",
            objective="Ensure workspace adapter runs",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        artifact_paths = {artifact.kind: artifact.path for artifact in self.store.list_artifacts(run.id)}
        events = self.store.list_events(entity_type="run", entity_id=run.id)

        self.assertIn("workspace_metadata", artifact_paths)
        self.assertTrue(Path(artifact_paths["workspace_metadata"]).exists())
        self.assertIn("project_workspace_prepared", [event.event_type for event in events])

    def test_run_once_validates_against_prepared_project_workspace(self) -> None:
        registry = ProjectAdapterRegistry()
        registry.register(ManifestProjectAdapter())
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-validation-root",
            project_adapter_registry=registry,
            worker=CandidateArtifactWorker(),
        )
        project = Project(
            id=new_id("project"),
            name="manifest-validation-project",
            description="Uses custom project adapter for validation",
            adapter_name="manifest",
        )
        self.store.create_project(project)
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Validate from prepared workspace",
            objective="Ensure validation uses the project workspace root",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        captured: dict[str, Path] = {}

        class FakeValidationService:
            def validate(self, task, run, work_result, workspace_path):
                captured["workspace_path"] = Path(workspace_path)
                updated_diagnostics = dict(work_result.diagnostics or {})
                updated_diagnostics.update(
                    {
                        "worker_outcome": "success",
                        "compile_check": {"passed": True},
                        "test_check": {"passed": True},
                    }
                )
                report_path = engine.workspace_root / "runs" / run.id / "report.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "worker_outcome": "success",
                            "changed_files": ["src/demo.py"],
                            "test_files": ["tests/test_demo.py"],
                            "compile_check": {"passed": True},
                            "test_check": {"passed": True},
                        }
                    ),
                    encoding="utf-8",
                )
                return WorkResult(
                    summary=work_result.summary,
                    artifacts=list(work_result.artifacts),
                    outcome="success",
                    diagnostics=updated_diagnostics,
                )

        engine.validation = FakeValidationService()
        engine.runs.validation_service = engine.validation

        run = engine.run_once(task.id)

        expected_workspace = engine.workspace_root / "runs" / run.id / "workspace"
        self.assertEqual(expected_workspace.resolve(), captured["workspace_path"].resolve())

    def test_run_once_fails_candidate_when_validation_evidence_is_missing(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-missing-validation-proof",
            worker=CandidateArtifactWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Missing validation proof",
            objective="Mirror the live broken candidate report shape",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        class BrokenValidationService:
            def validate(self, task, run, work_result, workspace_path):
                report_path = engine.workspace_root / "runs" / run.id / "report.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "worker_outcome": "candidate",
                            "changed_files": ["src/accruvia_harness/ui.py", "tests/test_ui.py"],
                            "test_files": ["tests/test_ui.py"],
                            "summary": "Candidate emitted but validation proof was never persisted.",
                            "validation_profile": "python",
                            "validation_mode": "default_focused",
                            "effective_validation_mode": "default_focused",
                            "worker_backend": "agent",
                            "llm_backend": "codex",
                            "command": "codex exec",
                            "atomicity_gate": {"score": 0.1, "flags": [], "action": "allow", "rationale": "safe"},
                            "atomicity_telemetry_path": str(engine.workspace_root / "runs" / run.id / "atomicity_telemetry.json"),
                        }
                    ),
                    encoding="utf-8",
                )
                return WorkResult(
                    summary=work_result.summary,
                    artifacts=list(work_result.artifacts),
                    outcome="success",
                    diagnostics={"worker_outcome": "candidate"},
                )

        engine.validation = BrokenValidationService()
        engine.runs.validation_service = engine.validation

        run = engine.run_once(task.id)

        stored_run = self.store.get_run(run.id)
        self.assertIsNotNone(stored_run)
        self.assertEqual(RunStatus.FAILED, stored_run.status)
        task_after = self.store.get_task(task.id)
        self.assertIsNotNone(task_after)
        self.assertEqual(TaskStatus.FAILED, task_after.status)
        report_path = engine.workspace_root / "runs" / run.id / "report.json"
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("failed", payload["worker_outcome"])
        self.assertEqual("validation_evidence_missing", payload["failure_category"])

    def test_run_once_blocks_objective_linked_task_when_execution_gate_is_not_ready(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Clarify workflow",
            summary="Need process control first",
        )
        self.store.create_objective(objective)
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Blocked task",
            objective="Should not execute yet",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )

        with self.assertRaisesRegex(ValueError, "Intent model is required before execution"):
            self.engine.run_once(task.id)

        self.store.create_intent_model(
            IntentModel(
                id=new_id("intent"),
                objective_id=objective.id,
                version=1,
                intent_summary="Map the desired operator flow",
            )
        )
        self.store.create_context_record(
            ContextRecord(
                id=new_id("context"),
                record_type="interrogation_completed",
                project_id=self.project_id,
                objective_id=objective.id,
                content="Interrogation complete",
            )
        )
        with self.assertRaisesRegex(ValueError, "A required Mermaid artifact must exist before execution"):
            self.engine.run_once(task.id)

        self.store.create_mermaid_artifact(
            MermaidArtifact(
                id=new_id("diagram"),
                objective_id=objective.id,
                diagram_type="workflow_control",
                version=1,
                status=MermaidStatus.PAUSED,
                summary="Paused flow",
                content="flowchart TD\nA-->B",
                required_for_execution=True,
            )
        )
        with self.assertRaisesRegex(ValueError, "Execution is blocked until the current Mermaid is finished"):
            self.engine.run_once(task.id)

    def test_process_next_task_skips_objective_gate_blocked_task_and_runs_next_candidate(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Blocked objective",
            summary="Not ready for execution",
        )
        self.store.create_objective(objective)
        blocked = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Blocked by objective gate",
            objective="Should remain pending until the objective is ready",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )
        runnable = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Runnable task",
            objective="Can execute immediately",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
        )

        result = self.engine.process_next_task(worker_id="worker-a", lease_seconds=120)

        assert result is not None
        self.assertEqual(runnable.id, result["task"].id)
        blocked_after = self.store.get_task(blocked.id)
        runnable_after = self.store.get_task(runnable.id)
        assert blocked_after is not None
        assert runnable_after is not None
        self.assertEqual(TaskStatus.PENDING, blocked_after.status)
        self.assertEqual(TaskStatus.COMPLETED, runnable_after.status)
        self.assertEqual([], self.store.list_task_leases())

    def test_project_adapter_can_supply_real_worker_override(self) -> None:
        registry = ProjectAdapterRegistry()
        registry.register(OverrideProjectAdapter())
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-override",
            project_adapter_registry=registry,
        )
        project = Project(
            id=new_id("project"),
            name="override-project",
            description="Uses custom project worker",
            adapter_name="override",
        )
        self.store.create_project(project)
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Use override worker",
            objective="Ensure project adapter worker executes",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

    def test_isolated_required_project_rejects_shared_repo_workspace(self) -> None:
        repo_root = Path(self.temp_dir.name) / "shared-repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text("# shared\n", encoding="utf-8")
        self._init_git_repo(repo_root)
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        registry = ProjectAdapterRegistry()
        registry.register(SharedRepoAdapter(repo_root))
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-shared",
            project_adapter_registry=registry,
        )
        project = Project(
            id=new_id("project"),
            name="shared-project",
            description="Unsafe shared repo project",
            adapter_name="shared",
            workspace_policy=WorkspacePolicy.ISOLATED_REQUIRED,
        )
        self.store.create_project(project)
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Unsafe task",
            objective="Should be rejected before work starts",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        with self.assertRaisesRegex(RuntimeError, "isolated workspaces"):
            engine.run_once(task.id)

    def test_approved_promotion_pushes_branch_to_remote(self) -> None:
        repo_root = Path(self.temp_dir.name) / "promote-repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text("# demo\n", encoding="utf-8")
        remote_root = self._init_git_repo(repo_root, with_remote=True)
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_root, check=True, capture_output=True, text=True)

        registry = ProjectAdapterRegistry()
        registry.register(GitBranchAdapter(repo_root))
        project = Project(
            id=new_id("project"),
            name="promote-project",
            description="Promotion apply-back project",
            adapter_name="gitbranch",
            promotion_mode=PromotionMode.BRANCH_ONLY,
            repo_provider=RepoProvider.GITHUB,
            repo_name="accruvia/promote-project",
        )
        self.store.create_project(project)
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-promote",
            project_adapter_registry=registry,
        )
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Promote branch",
            objective="Update readme",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        run_dir = engine.workspace_root / "runs" / run.id
        workspace = run_dir / "workspace"
        (workspace / "README.md").write_text("# demo\n\nupdated\n", encoding="utf-8")

        review = engine.review_promotion(task.id, run_id=run.id)

        class _LLMResult:
            def __init__(self, prompt_path: Path, response_path: Path, response_text: str) -> None:
                self.prompt_path = prompt_path
                self.response_path = response_path
                self.response_text = response_text

        class _ApproveRouter:
            def execute(self, invocation, telemetry=None):
                Path(invocation.run_dir).mkdir(parents=True, exist_ok=True)
                response_path = Path(invocation.run_dir) / "response.txt"
                prompt_path = Path(invocation.run_dir) / "prompt.txt"
                prompt_path.write_text(invocation.prompt, encoding="utf-8")
                response_text = '{"approved": true, "rationale": "ready"}'
                response_path.write_text(response_text, encoding="utf-8")
                return _LLMResult(prompt_path, response_path, response_text), "command"

        engine.set_llm_router(_ApproveRouter())
        affirmation = engine.affirm_promotion(task.id, run_id=run.id, promotion_id=review.promotion.id)

        self.assertEqual("approved", affirmation.promotion.status.value)
        applyback = affirmation.promotion.details["applyback"]
        self.assertIn("branch_name", applyback)
        self.assertIn(applyback["branch_name"], subprocess.run(
            ["git", "ls-remote", "--heads", str(remote_root)],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

    def test_remediation_promotion_updates_existing_review_branch(self) -> None:
        repo_root = Path(self.temp_dir.name) / "remediate-repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text("# demo\n", encoding="utf-8")
        remote_root = self._init_git_repo(repo_root, with_remote=True)
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_root, check=True, capture_output=True, text=True)

        registry = ProjectAdapterRegistry()
        registry.register(GitBranchAdapter(repo_root))
        project = Project(
            id=new_id("project"),
            name="remediate-project",
            description="Promotion remediation project",
            adapter_name="gitbranch",
            promotion_mode=PromotionMode.BRANCH_AND_PR,
            repo_provider=RepoProvider.GITHUB,
            repo_name="accruvia/remediate-project",
        )
        self.store.create_project(project)
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-remediate",
            project_adapter_registry=registry,
        )
        parent = engine.create_task_with_policy(
            project_id=project.id,
            title="Parent task",
            objective="Parent objective",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        task = engine.create_task_with_policy(
            project_id=project.id,
            title="Rebase approved change onto current main",
            objective="Replay change on top of current main",
            priority=100,
            parent_task_id=parent.id,
            source_run_id="run_source_parent",
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_external_metadata(
            task.id,
            {"promotion_remediation": {"branch_name": "existing-review-branch", "review_url": "https://example/pr/1"}},
        )
        run = engine.run_once(task.id)
        run_dir = engine.workspace_root / "runs" / run.id
        workspace = run_dir / "workspace"
        (workspace / "README.md").write_text("# demo\n\nremediated\n", encoding="utf-8")
        review = engine.review_promotion(task.id, run_id=run.id)

        class _LLMResult:
            def __init__(self, prompt_path: Path, response_path: Path, response_text: str) -> None:
                self.prompt_path = prompt_path
                self.response_path = response_path
                self.response_text = response_text

        class _ApproveRouter:
            def execute(self, invocation, telemetry=None):
                Path(invocation.run_dir).mkdir(parents=True, exist_ok=True)
                response_path = Path(invocation.run_dir) / "response.txt"
                prompt_path = Path(invocation.run_dir) / "prompt.txt"
                prompt_path.write_text(invocation.prompt, encoding="utf-8")
                response_text = '{"approved": true, "rationale": "ready"}'
                response_path.write_text(response_text, encoding="utf-8")
                return _LLMResult(prompt_path, response_path, response_text), "command"

        engine.set_llm_router(_ApproveRouter())
        affirmation = engine.affirm_promotion(task.id, run_id=run.id, promotion_id=review.promotion.id)

        applyback = affirmation.promotion.details["applyback"]
        self.assertTrue(applyback["updated_existing_review"])
        self.assertEqual("existing-review-branch", applyback["branch_name"])
        self.assertIsNone(applyback["pr_url"])
        self.assertIn(
            "existing-review-branch",
            subprocess.run(
                ["git", "ls-remote", "--heads", str(remote_root)],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
        )

    def test_run_once_rejects_terminal_tasks(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Terminal",
            objective="Already done",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(task.id, TaskStatus.COMPLETED)

        with self.assertRaises(ValueError):
            self.engine.run_once(task.id)

    def test_run_until_stable_fails_after_retry_budget_is_exhausted(self) -> None:
        failing_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-retry",
            worker=MissingArtifactWorker(),
        )
        task = failing_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Retry until failed",
            objective="Exercise bounded retries",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="457",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        runs = failing_engine.run_until_stable(task.id)
        task_after = self.store.get_task(task.id)
        last_run = runs[-1]
        last_eval = self.store.list_evaluations(last_run.id)[0]
        last_decision = self.store.list_decisions(last_run.id)[0]

        self.assertEqual(2, len(runs))
        self.assertEqual("failed", task_after.status.value if task_after else None)
        self.assertEqual("failed", last_run.status.value)
        self.assertEqual("incomplete", last_eval.verdict)
        self.assertEqual("fail", last_decision.action.value)
        self.assertEqual(["report"], last_eval.details["missing_required_artifacts"])

    def test_run_until_stable_resolves_branching_without_infinite_loop(self) -> None:
        branching_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-branching",
            decider=BranchOnceDecider(),
        )
        task = branching_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Branch until stable",
            objective="Resolve branching automatically",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            max_branches=2,
            required_artifacts=["plan", "report"],
        )

        runs = branching_engine.run_until_stable(task.id)
        task_after = self.store.get_task(task.id)

        self.assertEqual(TaskStatus.COMPLETED, task_after.status if task_after else None)
        self.assertEqual(3, len(runs))
        self.assertEqual(2, len([run for run in runs if run.branch_id is not None]))

    def test_retry_strategy_is_recorded_from_previous_evaluation(self) -> None:
        failing_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-retry-focus",
            worker=MissingArtifactWorker(),
        )
        task = failing_engine.create_task_with_policy(
            project_id=self.project_id,
            title="Retry focus",
            objective="Exercise retry feedback",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        runs = failing_engine.run_until_stable(task.id)
        second_run_events = self.store.list_events("run", runs[1].id)
        retry_event = next(
            event for event in second_run_events if event.event_type == "retry_strategy_selected"
        )

        self.assertEqual("incomplete", retry_event.payload["previous_verdict"])
        self.assertIn("report", retry_event.payload["focus"])

    def test_run_once_emits_auditable_events(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Emit events",
            objective="Capture the control flow",
            priority=150,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="458",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )

        run = self.engine.run_once(task.id)
        task_events = self.store.list_events("task", task.id)
        run_events = self.store.list_events("run", run.id)

        self.assertEqual(
            ["task_created", "task_activated", "task_status_changed"],
            [event.event_type for event in task_events],
        )
        self.assertEqual("gitlab_issue", task_events[0].payload["external_ref_type"])
        self.assertEqual("458", task_events[0].payload["external_ref_id"])
        self.assertEqual(
            ["run_created", "project_workspace_prepared", "planned", "worker_completed"],
            [event.event_type for event in run_events],
        )

    def test_process_queue_uses_priority_order(self) -> None:
        low = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="460",
            title="Low priority",
            objective="Go second",
            priority=50,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        high = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="461",
            title="High priority",
            objective="Go first",
            priority=500,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        processed = self.engine.process_queue(limit=2)

        self.assertEqual(2, len(processed))
        self.assertEqual(high.id, processed[0]["task"].id)
        self.assertEqual(low.id, processed[1]["task"].id)

    def test_process_next_task_runs_single_attempt_for_retryable_task(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-single-attempt",
            worker=MissingArtifactWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Retry later",
            objective="Should remain pending after one failed attempt",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

        result = engine.process_next_task(worker_id="worker-a", lease_seconds=120)
        latest_task = self.store.get_task(task.id)
        runs = self.store.list_runs(task.id)

        self.assertIsNotNone(result)
        self.assertEqual(task.id, result["task"].id)
        self.assertEqual(1, len(result["runs"]))
        self.assertEqual(1, len(runs))
        self.assertEqual(TaskStatus.PENDING, latest_task.status)

    def test_process_queue_does_not_immediately_retry_same_task(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-no-immediate-retry",
            worker=MissingArtifactWorker(),
        )
        retrying = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Retry later",
            objective="Should not be retried in same sweep",
            priority=500,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )
        second = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Second task",
            objective="Should run after first attempt of retrying task",
            priority=400,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

        processed = engine.process_queue(limit=2)
        processed_ids = [item["task"].id for item in processed]

        self.assertEqual([retrying.id, second.id], processed_ids)
        self.assertEqual(1, len(self.store.list_runs(retrying.id)))
        self.assertEqual(1, len(self.store.list_runs(second.id)))

    def test_import_issue_task_creates_gitlab_linked_task(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="462",
            title="Imported issue",
            objective="Work imported from GitLab",
            priority=300,
            strategy="baseline",
            max_attempts=4,
            required_artifacts=["plan", "report"],
        )

        self.assertEqual("gitlab_issue", task.external_ref_type)
        self.assertEqual("462", task.external_ref_id)
        self.assertEqual(300, task.priority)

    def test_engine_accepts_injected_policy_components(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-injected",
            planner=self.engine.planner,
            worker=self.engine.worker,
            analyzer=self.engine.analyzer,
            decider=self.engine.decider,
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Injected policy",
            objective="Verify policy composition",
            priority=125,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        self.assertEqual("completed", run.status.value)

    def test_create_follow_on_task_preserves_lineage(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Parent task",
            objective="Generate follow-on work",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="463",
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)

        follow_on = self.engine.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=run.id,
            title="Follow-on task",
            objective="Handle discovered defect",
        )

        self.assertEqual(task.id, follow_on.parent_task_id)
        self.assertEqual(run.id, follow_on.source_run_id)
        self.assertEqual("463", follow_on.external_ref_id)

    def test_create_follow_on_task_accepts_traceability_metadata_for_objective_remediation(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Track remediation under the same objective",
        )
        self.store.create_objective(objective)
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Parent task",
            objective="Generate follow-on work",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="463",
            external_ref_metadata={"labels": ["bug"]},
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        run = Run(
            id=new_id("run"),
            task_id=task.id,
            status=RunStatus.COMPLETED,
            attempt=1,
            summary="Synthetic parent run for follow-on lineage testing.",
        )
        self.store.create_run(run)

        follow_on = self.engine.create_follow_on_task(
            parent_task_id=task.id,
            source_run_id=run.id,
            title="Review remediation task",
            objective="Attach review traceability metadata",
            external_ref_metadata_overrides={
                "promotion_remediation": {
                    "finding_ids": ["finding_123"],
                    "review_round_id": "review_round_001",
                    "dimension_name": "security",
                }
            },
        )

        self.assertEqual(task.objective_id, follow_on.objective_id)
        self.assertEqual(task.id, follow_on.parent_task_id)
        self.assertEqual(run.id, follow_on.source_run_id)
        self.assertEqual("463", follow_on.external_ref_id)
        self.assertEqual(["bug"], follow_on.external_ref_metadata["labels"])
        self.assertEqual(
            {
                "finding_ids": ["finding_123"],
                "review_round_id": "review_round_001",
                "dimension_name": "security",
            },
            follow_on.external_ref_metadata["promotion_remediation"],
        )

    def test_create_tasks_from_review_findings_creates_same_objective_follow_on_tasks(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Track remediation under the same objective",
        )
        self.store.create_objective(objective)
        parent = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Parent failed task",
            objective="Review failed-task splitability",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        run = Run(id=new_id("run"), task_id=parent.id, status=RunStatus.COMPLETED, attempt=1, summary="source")
        self.store.create_run(run)

        created = self.engine.tasks.create_tasks_from_review_findings(
            parent_task_id=parent.id,
            source_run_id=run.id,
            findings=[
                {
                    "title": "Split finding A",
                    "objective": "Address finding A",
                    "finding_ids": ["finding_a"],
                    "review_round_id": "round_1",
                    "dimension_name": "security",
                    "summary": "Need narrower work",
                    "remediation_hints": ["Keep it bounded"],
                },
                {
                    "title": "Split finding B",
                    "objective": "Address finding B",
                    "finding_ids": ["finding_b"],
                    "review_round_id": "round_1",
                    "dimension_name": "qa",
                },
            ],
        )

        self.assertEqual(2, len(created))
        self.assertTrue(all(task.objective_id == objective.id for task in created))
        self.assertEqual(["finding_a"], created[0].external_ref_metadata["promotion_remediation"]["finding_ids"])
        self.assertEqual("round_1", created[0].external_ref_metadata["promotion_remediation"]["review_round_id"])

    def test_apply_failed_task_disposition_handles_retry_split_manual_and_waive(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Track failed-task handling under the same objective",
        )
        self.store.create_objective(objective)

        retry_task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Retry task",
            objective="Retry path",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(retry_task.id, TaskStatus.FAILED)
        retry_result = self.engine.tasks.apply_failed_task_disposition(
            task_id=retry_task.id,
            disposition="retry_as_is",
            rationale="Retry after narrowing metadata.",
            attempt_metadata={"atomicity_narrowing": {"category": "policy_self_modification"}},
        )
        retry_after = self.store.get_task(retry_task.id)
        self.assertEqual({"status": "pending", "task_id": retry_task.id}, retry_result)
        self.assertEqual(TaskStatus.PENDING, retry_after.status if retry_after else None)
        self.assertIn("atomicity_narrowing", retry_after.attempt_metadata if retry_after else {})

        split_task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Split task",
            objective="Split path",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        split_run = Run(id=new_id("run"), task_id=split_task.id, status=RunStatus.BLOCKED, attempt=1, summary="blocked")
        self.store.create_run(split_run)
        self.store.update_task_status(split_task.id, TaskStatus.FAILED)
        split_result = self.engine.tasks.apply_failed_task_disposition(
            task_id=split_task.id,
            disposition="split_into_narrower_tasks",
            rationale="Task is splittable.",
            source_run_id=split_run.id,
            findings=[{"title": "Narrow split", "objective": "Do the narrow thing", "finding_ids": ["finding_1"]}],
        )
        self.assertEqual("split", split_result["status"])
        self.assertEqual(1, len(split_result["task_ids"]))

        manual_task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Manual task",
            objective="Manual path",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        manual_run = Run(id=new_id("run"), task_id=manual_task.id, status=RunStatus.BLOCKED, attempt=1, summary="blocked")
        self.store.create_run(manual_run)
        self.store.update_task_status(manual_task.id, TaskStatus.FAILED)
        manual_result = self.engine.tasks.apply_failed_task_disposition(
            task_id=manual_task.id,
            disposition="allow_manual_operator_implementation",
            rationale="Needs operator handling.",
            source_run_id=manual_run.id,
            operator_title="Manual operator follow-on",
            operator_objective="Complete the control-plane change manually.",
        )
        manual_follow_on = self.store.get_task(manual_result["task_id"])
        self.assertEqual("manual", manual_result["status"])
        self.assertEqual("operator_ergonomics", manual_follow_on.strategy if manual_follow_on else None)
        self.assertTrue(bool(manual_follow_on.external_ref_metadata.get("operator_owned")) if manual_follow_on else False)

        waive_task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Waive task",
            objective="Waive path",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(waive_task.id, TaskStatus.FAILED)
        waive_result = self.engine.tasks.apply_failed_task_disposition(
            task_id=waive_task.id,
            disposition="waive_obsolete",
            rationale="Superseded by manual implementation.",
        )
        waiver_records = self.store.list_context_records(objective_id=objective.id, record_type="failed_task_waived")
        self.assertEqual({"status": "waived", "task_id": waive_task.id}, waive_result)
        self.assertEqual(1, len(waiver_records))

    def test_waived_obsolete_failed_task_counts_as_resolved_for_objective_phase(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Waived obsolete failures should not block objective resolution",
        )
        self.store.create_objective(objective)
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Obsolete failed task",
            objective="Obsolete path",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(task.id, TaskStatus.FAILED)

        self.engine.tasks.apply_failed_task_disposition(
            task_id=task.id,
            disposition="waive_obsolete",
            rationale="Superseded by manual implementation.",
        )

        objective_after = self.store.get_objective(objective.id)
        self.assertEqual(ObjectiveStatus.RESOLVED, objective_after.status if objective_after else None)

    def test_promotion_service_decompose_review_findings_to_atomic_tasks_returns_created_ids(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Track remediation under the same objective",
        )
        self.store.create_objective(objective)
        parent = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Parent failed task",
            objective="Review failed-task splitability",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        run = Run(id=new_id("run"), task_id=parent.id, status=RunStatus.COMPLETED, attempt=1, summary="source")
        self.store.create_run(run)
        service = PromotionService(store=self.store, task_service=self.engine.tasks, workspace_root=self.engine.workspace_root)

        created_ids = service.decompose_review_findings_to_atomic_tasks(
            parent_task_id=parent.id,
            source_run_id=run.id,
            findings=[{"title": "Narrow split", "objective": "Do the narrow thing", "finding_ids": ["finding_1"]}],
        )

        self.assertEqual(1, len(created_ids))
        created = self.store.get_task(created_ids[0])
        self.assertEqual(objective.id, created.objective_id if created else None)

    def test_return_objective_to_execution_loop_reopens_objective_after_remediation_tasks(self) -> None:
        objective = Objective(
            id=new_id("objective"),
            project_id=self.project_id,
            title="Promotion review",
            summary="Return same objective to execution after remediation",
        )
        self.store.create_objective(objective)
        remediation = self.engine.create_task_with_policy(
            project_id=self.project_id,
            objective_id=objective.id,
            title="Remediation task",
            objective="Resume execution",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=2,
            required_artifacts=["plan", "report"],
        )
        self.store.update_task_status(remediation.id, TaskStatus.PENDING)
        service = ObjectivePromotionService(self.store)

        status = service._return_objective_to_execution_loop(
            objective.id,
            source_task_id=remediation.id,
            source_run_id="run_remediation",
        )

        objective_after = self.store.get_objective(objective.id)
        receipts = self.store.list_context_records(objective_id=objective.id, record_type="objective_execution_reentered")
        self.assertEqual(ObjectiveStatus.PLANNING, status)
        self.assertEqual(ObjectiveStatus.PLANNING, objective_after.status if objective_after else None)
        self.assertEqual(1, len(receipts))

    def test_process_next_task_uses_and_releases_lease(self) -> None:
        task = self.engine.import_issue_task(
            project_id=self.project_id,
            issue_id="464",
            title="Lease-aware queue item",
            objective="Verify process-next leases work",
            priority=100,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        result = self.engine.process_next_task(worker_id="worker-a", lease_seconds=120)

        self.assertEqual(task.id, result["task"].id if result else None)
        self.assertEqual([], self.store.list_task_leases())

    def test_review_promotion_creates_pending_candidate(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion pass",
            objective="Produce promotable candidate",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)

        result = self.engine.review_promotion(task.id, run.id)

        self.assertEqual("pending", result.promotion.status.value)
        self.assertIsNone(result.follow_on_task_id)
        self.assertEqual("pending", self.store.latest_promotion(task.id).status.value)

    def test_affirm_promotion_approves_pending_candidate(self) -> None:
        config = HarnessConfig(
            db_path=Path(self.temp_dir.name) / "affirm.db",
            workspace_root=Path(self.temp_dir.name) / "workspace-affirm",
            log_path=Path(self.temp_dir.name) / "affirm.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="command",
            llm_model=None,
            llm_command=f'bash "{Path(__file__).resolve().parent / "fixtures" / "fake_affirm_approve.sh"}"',
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-affirm",
            llm_router=build_llm_router(config),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion affirm",
            objective="Affirm a promotable candidate",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        engine.review_promotion(task.id, run.id)

        result = engine.affirm_promotion(task.id, run.id)

        self.assertEqual("approved", result.promotion.status.value)
        self.assertIn("affirmation", result.promotion.details)

    def test_repository_promotion_apply_objective_pushes_objective_snapshot_and_cleans_worktree(self) -> None:
        repo_root = Path(self.temp_dir.name) / "objective-promotion-repo"
        repo_root.mkdir()
        remote_root = self._init_git_repo(repo_root, with_remote=True)
        assert remote_root is not None
        tracked = repo_root / "src" / "feature.py"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("VALUE = 'base'\n", encoding="utf-8")
        unrelated = repo_root / "README.md"
        unrelated.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_root, check=True, capture_output=True, text=True)

        tracked.write_text("VALUE = 'objective'\n", encoding="utf-8")
        unrelated.write_text("local dirt\n", encoding="utf-8")
        project = Project(
            id=new_id("project"),
            name="repo-promotion",
            description="repo promotion",
            promotion_mode=PromotionMode.DIRECT_MAIN,
            repo_provider=RepoProvider.GITHUB,
            repo_name="accruvia/accruvia-harness",
            base_branch="main",
        )

        staging_root = Path(self.temp_dir.name) / "objective-promotion-staging"
        result = RepositoryPromotionService().apply_objective(
            project,
            objective_id=new_id("objective"),
            objective_title="Objective Promotion Review",
            source_repo_root=repo_root,
            source_working_root=repo_root,
            objective_paths=["src/feature.py"],
            staging_root=staging_root,
        )

        self.assertTrue(result.commit_sha)
        self.assertTrue(result.pushed_ref.endswith(":main"))
        self.assertTrue(result.cleanup_performed)
        self.assertEqual(result.commit_sha, result.verified_remote_sha)
        self.assertFalse(any(staging_root.iterdir()) if staging_root.exists() else False)
        remote_show = subprocess.run(
            ["git", "--git-dir", str(remote_root), "show", "main:src/feature.py"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("VALUE = 'objective'\n", remote_show.stdout)
        remote_readme = subprocess.run(
            ["git", "--git-dir", str(remote_root), "show", "main:README.md"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("base\n", remote_readme.stdout)

    def test_review_promotion_rejects_and_creates_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-promotion-blocked",
            worker=PromotionBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion blocked",
            objective="Exercise promotion rejection",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type="gitlab_issue",
            external_ref_id="465",
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        result = engine.review_promotion(task.id, run.id)
        follow_on = self.store.get_task(result.follow_on_task_id) if result.follow_on_task_id else None

        self.assertEqual("rejected", result.promotion.status.value)
        self.assertIsNotNone(follow_on)
        self.assertEqual(task.id, follow_on.parent_task_id if follow_on else None)
        self.assertEqual(run.id, follow_on.source_run_id if follow_on else None)
        self.assertEqual("failed", self.store.get_task(task.id).status.value)

    def test_blocked_worker_outcome_records_blocked_evaluation(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-blocked-outcome",
            worker=BlockedDiagnosisWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Blocked outcome",
            objective="Surface blocked diagnosis explicitly",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        evaluation = self.store.list_evaluations(run.id)[0]
        decision = self.store.list_decisions(run.id)[0]

        self.assertEqual("blocked", run.status.value)
        self.assertEqual("blocked", evaluation.verdict)

    def test_blocked_scope_violation_records_attempt_metadata(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-scope-split",
            worker=ScopeSplitWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Oversized scoped task",
            objective="Refactor server client and tests together",
            priority=200,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            scope={"allowed_paths": ["src/server_client.py"]},
            strategy="default",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        children = self.store.list_child_tasks(task.id)
        self.assertEqual("blocked", run.status.value)
        self.assertEqual(0, len(children))

    def test_failed_worker_outcome_records_failed_evaluation(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-failed-outcome",
            worker=FailedDiagnosisWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Failed outcome",
            objective="Surface failed worker outcome explicitly",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)
        evaluation = self.store.list_evaluations(run.id)[0]
        decision = self.store.list_decisions(run.id)[0]

        self.assertEqual("failed", run.status.value)
        self.assertEqual("failed", evaluation.verdict)
        self.assertEqual("fail", decision.action.value)

    def test_infrastructure_blocked_worker_creates_executor_repair_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-infra-blocked",
            worker=InfrastructureBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Executor failure",
            objective="Do not burn retry budget on infrastructure failures",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        decision = self.store.list_decisions(run.id)[0]
        self.assertEqual("blocked", run.status.value)
        self.assertEqual("fail", decision.action.value)
        self.assertEqual("failed", self.store.get_task(task.id).status.value)
        # Infrastructure failure info is now recorded in attempt_metadata
        updated_task = self.store.get_task(task.id)
        self.assertIn("infrastructure_failure", updated_task.attempt_metadata)

    def test_infrastructure_failed_worker_persists_report_and_creates_one_repair_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-infra-failed",
            worker=InfrastructureFailedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Executor exit failure",
            objective="Persist actionable failure evidence for executor exits",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        artifacts = self.store.list_artifacts(run.id)
        evaluations = self.store.list_evaluations(run.id)
        decisions = self.store.list_decisions(run.id)

        self.assertEqual("failed", run.status.value)
        self.assertIn("report", [artifact.kind for artifact in artifacts])
        report_artifact = next(artifact for artifact in artifacts if artifact.kind == "report")
        self.assertTrue(Path(report_artifact.path).exists())
        self.assertTrue(evaluations[0].details["infrastructure_failure"])
        self.assertEqual("fail", decisions[0].action.value)
        # Infrastructure failure info is now recorded in attempt_metadata instead of child tasks
        updated_task = self.store.get_task(task.id)
        self.assertIn("infrastructure_failure", updated_task.attempt_metadata)

    def test_executor_repair_task_does_not_spawn_recursive_repair_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-infra-repair",
            worker=InfrastructureBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Repair executor runtime",
            objective="Repair executor/runtime failure",
            priority=900,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="executor_repair",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        children = self.store.list_child_tasks(task.id)
        self.assertEqual("blocked", run.status.value)
        self.assertEqual([], children)

    def test_timeout_bound_repair_task_creates_narrower_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-timeout-decomposition",
            worker=ValidationTimeoutWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Repair executor runtime",
            objective="Repair executor/runtime failure",
            priority=900,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="executor_repair",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        self.assertEqual("failed", run.status.value)
        self.assertEqual("failed", self.store.get_task(task.id).status.value)
        # Timeout narrowing info is now recorded in attempt_metadata instead of child tasks
        updated_task = self.store.get_task(task.id)
        self.assertIn("timeout_narrowing", updated_task.attempt_metadata)
        self.assertEqual("validation_timeout", updated_task.attempt_metadata["timeout_narrowing"]["category"])

    def test_atomicity_blocked_worker_creates_atomicity_split_follow_on(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-atomicity-follow-on",
            worker=AtomicityBlockedWorker("atomicity_decomposition"),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Operator task",
            objective="Keep this slice atomic.",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            validation_profile="generic",
            validation_mode="lightweight_operator",
            strategy="operator_ergonomics",
            max_attempts=3,
            required_artifacts=["plan", "report"],
        )

        run = engine.run_once(task.id)

        self.assertEqual("blocked", run.status.value)
        # Atomicity narrowing info is now recorded in attempt_metadata instead of child tasks
        updated_task = self.store.get_task(task.id)
        self.assertIn("atomicity_narrowing", updated_task.attempt_metadata)
        self.assertEqual("atomicity_decomposition", updated_task.attempt_metadata["atomicity_narrowing"]["category"])

    def test_review_promotion_dedupes_follow_on_for_same_run(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-promotion-dedupe",
            worker=PromotionBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion blocked once",
            objective="Avoid duplicate follow-ons",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        first = engine.review_promotion(task.id, run.id)
        second = engine.review_promotion(task.id, run.id)

        self.assertEqual(first.follow_on_task_id, second.follow_on_task_id)

    def test_review_promotion_rejects_when_deterministic_test_evidence_is_missing(self) -> None:
        class NoTestEvidenceWorker(LocalArtifactWorker):
            def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                plan_path = run_dir / "plan.txt"
                plan_path.write_text("plan\n", encoding="utf-8")
                report_path = run_dir / "report.json"
                report_path.write_text(
                    json.dumps(
                        {
                            "changed_files": ["src/example.py"],
                            "compile_check": {"passed": True},
                            "test_files": [],
                            "test_check": {"passed": False},
                        }
                    ),
                    encoding="utf-8",
                )
                return WorkResult(
                    summary="Recorded candidate without test evidence.",
                    artifacts=[
                        ("plan", str(plan_path), "Plan artifact"),
                        ("report", str(report_path), "Candidate report artifact"),
                    ],
                )

        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-no-tests",
            worker=NoTestEvidenceWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Missing test evidence",
            objective="Exercise deterministic validator rejection",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)

        result = engine.review_promotion(task.id, run.id)

        self.assertEqual("rejected", result.promotion.status.value)
        validator_names = [entry["validator"] for entry in result.promotion.details["validators"]]
        self.assertIn("test_evidence", validator_names)

    def test_affirmation_prompt_includes_report_contents(self) -> None:
        task = self.engine.create_task_with_policy(
            project_id=self.project_id,
            title="Prompt contents",
            objective="Expose report contents to affirmation",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = self.engine.run_once(task.id)
        review = self.engine.review_promotion(task.id, run.id)
        service = PromotionService(self.store, self.engine.tasks, Path(self.temp_dir.name) / "workspace-prompt")
        prompt = service._build_affirmation_prompt(task, review.promotion, self.store.list_artifacts(run.id))

        self.assertIn('"changed_files"', prompt)
        self.assertIn("Artifact Contents:", prompt)

    def test_follow_on_objective_aggregates_multiple_issues(self) -> None:
        class MultiIssueWorker(LocalArtifactWorker):
            def work(self, task, run, workspace_root: Path) -> WorkResult:  # type: ignore[override]
                run_dir = workspace_root / "runs" / run.id
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "plan.txt").write_text("plan\n", encoding="utf-8")
                (run_dir / "report.json").write_text(json.dumps({}), encoding="utf-8")
                return WorkResult(
                    summary="multiple issues",
                    artifacts=[
                        ("plan", str(run_dir / "plan.txt"), "Plan"),
                        ("report", str(run_dir / "report.json"), "Report"),
                    ],
                )

        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-multi-issue",
            worker=MultiIssueWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Multi issue",
            objective="Exercise multiple validation issues",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        result = engine.review_promotion(task.id, run.id)
        follow_on = self.store.get_task(result.follow_on_task_id) if result.follow_on_task_id else None

        self.assertIsNotNone(follow_on)
        assert follow_on is not None
        self.assertIn("- ", follow_on.objective)
        self.assertIn("changed source and test files", follow_on.objective.lower())
        self.assertIn("compile", follow_on.objective.lower())

    def test_rereview_promotion_uses_remediation_run(self) -> None:
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-rereview",
            worker=PromotionBlockedWorker(),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Needs remediation",
            objective="Fail first review",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        failed_run = engine.run_once(task.id)
        failed_review = engine.review_promotion(task.id, failed_run.id)
        remediation_task = self.store.get_task(failed_review.follow_on_task_id)
        assert remediation_task is not None

        remediation_engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-rereview-remediation",
        )
        remediation_run = remediation_engine.run_once(remediation_task.id)
        rereview = remediation_engine.rereview_promotion(
            task.id,
            remediation_task_id=remediation_task.id,
            remediation_run_id=remediation_run.id,
            base_promotion_id=failed_review.promotion.id,
        )

        self.assertEqual("pending", rereview.promotion.status.value)
        self.assertEqual("rereview", rereview.promotion.details["review_mode"])
        self.assertEqual(remediation_task.id, rereview.promotion.details["remediation_task_id"])

    def test_affirm_promotion_rejects_pending_candidate(self) -> None:
        config = HarnessConfig(
            db_path=Path(self.temp_dir.name) / "reject.db",
            workspace_root=Path(self.temp_dir.name) / "workspace-reject-affirm",
            log_path=Path(self.temp_dir.name) / "reject.log",
            default_project_name="demo",
            default_repo="accruvia/accruvia",
            runtime_backend="local",
            temporal_target="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="accruvia-harness",
            worker_backend="local",
            worker_command=None,
            llm_backend="command",
            llm_model=None,
            llm_command=f'bash "{Path(__file__).resolve().parent / "fixtures" / "fake_affirm_reject.sh"}"',
            llm_codex_command=None,
            llm_claude_command=None,
            llm_accruvia_client_command=None,
        )
        engine = HarnessEngine(
            store=self.store,
            workspace_root=Path(self.temp_dir.name) / "workspace-reject-affirm",
            llm_router=build_llm_router(config),
        )
        task = engine.create_task_with_policy(
            project_id=self.project_id,
            title="Promotion reject",
            objective="Reject a pending promotion",
            priority=100,
            parent_task_id=None,
            source_run_id=None,
            external_ref_type=None,
            external_ref_id=None,
            strategy="baseline",
            max_attempts=1,
            required_artifacts=["plan", "report"],
        )
        run = engine.run_once(task.id)
        engine.review_promotion(task.id, run.id)

        result = engine.affirm_promotion(task.id, run.id)

        self.assertEqual("rejected", result.promotion.status.value)
        self.assertIsNotNone(result.follow_on_task_id)
