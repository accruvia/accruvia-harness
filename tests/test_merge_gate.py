"""Tests for the merge gate policy + execution."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from accruvia_harness.domain import DecisionAction
from accruvia_harness.merge_gate import (
    DEFAULT_DENIED_PATHS,
    MergePolicy,
    _matches_any,
    auto_merge_run,
    evaluate_run,
    execute_merge,
)


@dataclass
class _FakeRun:
    id: str
    task_id: str


@dataclass
class _FakeArtifact:
    kind: str
    path: str


@dataclass
class _FakeDecision:
    action: str
    rationale: str = ""


@dataclass
class _FakeStore:
    runs: dict = field(default_factory=dict)
    artifacts_by_run: dict = field(default_factory=dict)
    decisions_by_run: dict = field(default_factory=dict)

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def list_artifacts(self, run_id):
        return list(self.artifacts_by_run.get(run_id, []))

    def list_decisions(self, run_id):
        return list(self.decisions_by_run.get(run_id, []))


def _make_store_with_run(
    run_id="run_abc123def456",
    task_id="task_xyz789012345",
    decision="promote",
    report=None,
    report_path=None,
):
    store = _FakeStore()
    store.runs[run_id] = _FakeRun(id=run_id, task_id=task_id)
    store.decisions_by_run[run_id] = [_FakeDecision(action=decision)]
    if report is not None:
        tmp = Path(tempfile.mkstemp(suffix=".json")[1])
        tmp.write_text(json.dumps(report), encoding="utf-8")
        report_path = str(tmp)
    if report_path:
        store.artifacts_by_run[run_id] = [_FakeArtifact(kind="report", path=report_path)]
    else:
        store.artifacts_by_run[run_id] = []
    return store, run_id, task_id


def _healthy_report(changed_files):
    return {
        "changed_files": changed_files,
        "compile_check": {"passed": True},
        "test_check": {"passed": True},
        "ship_ready": True,
        "overall_validation": "pass",
    }


class MatchesAnyTests(unittest.TestCase):
    def test_matches_glob(self):
        self.assertIsNotNone(_matches_any(".github/workflows/ci.yml", (".github/**",)))

    def test_matches_basename(self):
        self.assertIsNotNone(_matches_any("src/secrets.key", ("*.key",)))

    def test_no_match(self):
        self.assertIsNone(_matches_any("src/foo.py", DEFAULT_DENIED_PATHS))


class EvaluateRunTests(unittest.TestCase):
    def test_healthy_run_auto_merges(self):
        store, run_id, task_id = _make_store_with_run(
            report=_healthy_report(["src/a.py", "tests/test_a.py"]),
        )
        d = evaluate_run(store, run_id)
        self.assertTrue(d.auto_merge, f"concerns: {d.concerns}")
        self.assertEqual([], d.concerns)
        self.assertEqual(run_id, d.run_id)
        self.assertEqual(task_id, d.task_id)
        self.assertEqual(f"harness-{task_id[-6:]}-{run_id[-6:]}", d.branch_name)

    def test_missing_run_blocks(self):
        store = _FakeStore()
        d = evaluate_run(store, "run_missing")
        self.assertFalse(d.auto_merge)
        self.assertIn("run_not_found", d.concerns)

    def test_decision_not_promote_blocks(self):
        store, run_id, _ = _make_store_with_run(
            decision="fail", report=_healthy_report(["src/a.py"]),
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertTrue(any("decision_not_promote" in c for c in d.concerns))

    def test_missing_report_blocks(self):
        store, run_id, _ = _make_store_with_run(report=None)
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertIn("missing_report", d.concerns)

    def test_compile_fail_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report={
                **_healthy_report(["src/a.py"]),
                "compile_check": {"passed": False},
            },
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertIn("compile_check_not_passed", d.concerns)

    def test_test_fail_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report={
                **_healthy_report(["src/a.py"]),
                "test_check": {"passed": False},
            },
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertIn("test_check_not_passed", d.concerns)

    def test_ship_not_ready_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report={**_healthy_report(["src/a.py"]), "ship_ready": False},
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertIn("self_review_not_ship_ready", d.concerns)

    def test_denied_path_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report(["src/a.py", ".github/workflows/ci.yml"]),
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertTrue(any("denied_path" in c for c in d.concerns))

    def test_secrets_file_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report(["src/a.py", "secrets/prod.pem"]),
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertTrue(any("denied_path" in c for c in d.concerns))

    def test_env_file_blocks(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report(["src/a.py", "config/.env.prod"]),
        )
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertTrue(any("denied_path" in c for c in d.concerns))

    def test_file_count_cap_blocks(self):
        many = [f"src/file_{i}.py" for i in range(60)]
        store, run_id, _ = _make_store_with_run(report=_healthy_report(many))
        d = evaluate_run(store, run_id, policy=MergePolicy(max_changed_files=50))
        self.assertFalse(d.auto_merge)
        self.assertTrue(any("changed_files_over_cap" in c for c in d.concerns))

    def test_custom_policy_relaxes(self):
        store, run_id, _ = _make_store_with_run(
            report={**_healthy_report(["src/a.py"]), "ship_ready": False},
        )
        # Custom policy that doesn't require ship_ready
        d = evaluate_run(store, run_id, policy=MergePolicy(require_ship_ready=False))
        self.assertTrue(d.auto_merge, f"concerns: {d.concerns}")

    def test_no_decision_blocks(self):
        store = _FakeStore()
        run_id = "run_x"
        store.runs[run_id] = _FakeRun(id=run_id, task_id="task_y")
        store.artifacts_by_run[run_id] = []
        d = evaluate_run(store, run_id)
        self.assertFalse(d.auto_merge)
        self.assertIn("no_decision", d.concerns)


class ExecuteMergeTests(unittest.TestCase):
    def _init_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)
        (root / "a.txt").write_text("v1")
        subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)

    def test_merge_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "feature"], check=True)
            (root / "b.txt").write_text("new")
            subprocess.run(["git", "-C", str(root), "add", "b.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "feat"], check=True)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
            r = execute_merge(root, "feature", target_branch="main")
            self.assertTrue(r.merged, r.stderr)
            self.assertTrue((root / "b.txt").exists())
            self.assertTrue(len(r.commit_sha) >= 7)

    def test_merge_missing_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            r = execute_merge(root, "nope", target_branch="main")
            self.assertFalse(r.merged)
            self.assertIn("branch not found", r.stderr)

    def test_merge_dirty_tree_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "f"], check=True)
            (root / "b").write_text("x")
            subprocess.run(["git", "-C", str(root), "add", "b"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "f"], check=True)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
            (root / "a.txt").write_text("dirty")
            r = execute_merge(root, "f", target_branch="main")
            self.assertFalse(r.merged)
            self.assertIn("dirty", r.stderr)


class AutoMergeRunTests(unittest.TestCase):
    def test_dry_run_never_executes(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report(["src/a.py"]),
        )
        d, r = auto_merge_run(store, run_id, Path("."), dry_run=True)
        self.assertTrue(d.auto_merge)
        self.assertIsNone(r)

    def test_blocked_decision_skips_execution(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report([".github/workflows/ci.yml"]),
        )
        d, r = auto_merge_run(store, run_id, Path("."))
        self.assertFalse(d.auto_merge)
        self.assertIsNone(r)


class CLIAutoMergeTests(unittest.TestCase):
    def test_parse_auto_merge_run_positional(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["auto-merge-run", "run_abc"])
        self.assertEqual(args.command, "auto-merge-run")
        self.assertEqual(args.run_id, "run_abc")
        self.assertEqual(args.target_branch, "main")
        self.assertFalse(args.dry_run)

    def test_parse_auto_merge_run_dry_run_flag(self):
        from accruvia_harness.cli_parser import build_parser
        parser = build_parser()
        args = parser.parse_args(["auto-merge-run", "run_xyz", "--dry-run"])
        self.assertTrue(args.dry_run)


class PromotionServiceConvergenceTests(unittest.TestCase):
    def test_evaluate_run_returns_merge_decision(self):
        store, run_id, _ = _make_store_with_run(
            report=_healthy_report(["src/a.py"]),
        )
        decision = evaluate_run(store, run_id)
        self.assertTrue(decision.auto_merge)
        self.assertEqual([], decision.concerns)
        self.assertEqual(run_id, decision.run_id)


if __name__ == "__main__":
    unittest.main()
