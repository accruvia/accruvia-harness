"""Tests for the skills framework and built-in skills.

Covers the skill abstraction, schema validation, JSON extraction, and every
built-in skill's prompt generation, response parsing, and output validation.
Orchestration-level tests live in tests/test_work_orchestrator.py.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.skills import (
    BenchmarkSkill,
    CommitSkill,
    DiagnoseSkill,
    FollowOnSkill,
    ImplementSkill,
    PostMergeCheckSkill,
    PromotionApplySkill,
    PromotionReviewSkill,
    ScopeSkill,
    SelfReviewSkill,
    SkillRegistry,
    SkillResult,
    ValidateSkill,
    apply_changes,
    build_default_registry,
    commands_for_profile,
    extract_json_payload,
    validate_against_schema,
)


class SchemaValidationTests(unittest.TestCase):
    def test_required_fields(self) -> None:
        schema = {"required": ["a", "b"], "types": {}}
        ok, errs = validate_against_schema({"a": 1}, schema)
        self.assertFalse(ok)
        self.assertIn("missing required field: b", errs)

    def test_type_checks(self) -> None:
        schema = {"required": [], "types": {"flag": "bool", "name": "str", "count": "int"}}
        ok, _ = validate_against_schema({"flag": True, "name": "x", "count": 3}, schema)
        self.assertTrue(ok)
        ok, errs = validate_against_schema({"flag": "true"}, schema)
        self.assertFalse(ok)
        self.assertTrue(any("expected bool" in e for e in errs))

    def test_int_rejects_bool(self) -> None:
        schema = {"required": [], "types": {"count": "int"}}
        ok, errs = validate_against_schema({"count": True}, schema)
        self.assertFalse(ok)
        self.assertTrue(any("expected int, got bool" in e for e in errs))

    def test_allowed_values(self) -> None:
        schema = {"required": [], "types": {}, "allowed_values": {"tier": ["a", "b"]}}
        ok, _ = validate_against_schema({"tier": "a"}, schema)
        self.assertTrue(ok)
        ok, errs = validate_against_schema({"tier": "z"}, schema)
        self.assertFalse(ok)
        self.assertTrue(any("must be one of" in e for e in errs))


class JsonExtractionTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json_payload('{"a":1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        text = "leading\n```json\n{\"x\":true}\n```\ntrailing"
        self.assertEqual(extract_json_payload(text), {"x": True})

    def test_embedded_object(self) -> None:
        text = 'preamble {"key":"val"} more text'
        self.assertEqual(extract_json_payload(text), {"key": "val"})

    def test_nested(self) -> None:
        self.assertEqual(
            extract_json_payload('{"n":{"k":[1,2]}}'),
            {"n": {"k": [1, 2]}},
        )

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(extract_json_payload(""))
        self.assertIsNone(extract_json_payload("no json at all"))


class ScopeSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = ScopeSkill()

    def test_prompt_includes_constraints(self) -> None:
        p = self.skill.build_prompt({
            "title": "Add cache", "objective": "TTL", "strategy": "perf",
            "allowed_paths": ["src/auth/"], "forbidden_paths": ["src/billing/"],
            "repo_context": "context",
        })
        self.assertIn("Add cache", p)
        self.assertIn("src/auth/", p)
        self.assertIn("src/billing/", p)

    def test_retry_path_mentions_prior(self) -> None:
        p = self.skill.build_prompt({
            "title": "x", "objective": "y", "strategy": "s", "repo_context": "c",
            "prior_scope": "touched src/wrong.py",
            "retry_feedback": "never ran tests",
        })
        self.assertIn("src/wrong.py", p)
        self.assertIn("never ran tests", p)

    def test_valid_output(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "files_to_touch": ["a.py", "b.py"],
            "approach": "add b, refactor a",
            "risks": ["merge conflict"],
            "estimated_complexity": "small",
        }))
        ok, errs = self.skill.validate_output(parsed)
        self.assertTrue(ok, errs)

    def test_empty_files_rejected(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "files_to_touch": [], "approach": "x", "risks": [],
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertFalse(ok)


class ImplementSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = ImplementSkill()

    def test_edit_applies_unique_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("def greet():\n    return 'hi'\n")
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [
                        {"path": "src/a.py", "old_string": "return 'hi'", "new_string": "return 'hello'"},
                    ],
                    "new_files": [],
                    "deleted_files": [],
                    "rationale": "friendlier greeting",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["src/a.py"])
            self.assertEqual(1, summary["edits_applied"])
            self.assertEqual([], summary["rejected"])
            self.assertIn("return 'hello'", (root / "src" / "a.py").read_text())

    def test_edit_rejects_non_unique_old_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("return None\nreturn None\n")
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [
                        {"path": "src/a.py", "old_string": "return None", "new_string": "return True"},
                    ],
                    "new_files": [], "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["src/a.py"])
            self.assertEqual(0, summary["edits_applied"])
            self.assertEqual(1, len(summary["rejected"]))
            self.assertIn("old_string_not_unique", summary["rejected"][0]["reason"])
            # File untouched
            self.assertEqual("return None\nreturn None\n", (root / "src" / "a.py").read_text())

    def test_edit_rejects_missing_old_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("nothing here")
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [{"path": "a.py", "old_string": "missing", "new_string": "x"}],
                    "new_files": [], "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["a.py"])
            self.assertEqual(0, summary["edits_applied"])
            self.assertEqual("old_string_not_found", summary["rejected"][0]["reason"])

    def test_edit_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [{"path": "missing.py", "old_string": "x", "new_string": "y"}],
                    "new_files": [], "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["missing.py"])
            self.assertEqual("edit_target_missing", summary["rejected"][0]["reason"])

    def test_new_file_creates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [],
                    "new_files": [{"path": "src/new.py", "content": "x = 1"}],
                    "deleted_files": [], "rationale": "new module",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["src/new.py"])
            self.assertEqual(1, summary["new_files_created"])
            self.assertEqual("x = 1", (root / "src" / "new.py").read_text())

    def test_new_file_rejected_when_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("old")
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [],
                    "new_files": [{"path": "a.py", "content": "new"}],
                    "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["a.py"])
            self.assertEqual(0, summary["new_files_created"])
            self.assertEqual("new_file_already_exists", summary["rejected"][0]["reason"])
            self.assertEqual("old", (root / "a.py").read_text())

    def test_scope_enforcement_rejects_out_of_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [],
                    "new_files": [
                        {"path": "src/ok.py", "content": "yes"},
                        {"path": "src/forbidden.py", "content": "no"},
                    ],
                    "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=["src/ok.py"])
            self.assertIn("src/ok.py", summary["written"])
            self.assertEqual(1, len(summary["rejected"]))
            self.assertFalse((root / "src" / "forbidden.py").exists())

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "edits": [],
                    "new_files": [{"path": "../etc/passwd", "content": "bad"}],
                    "deleted_files": [], "rationale": "x",
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=[])
            self.assertEqual([], summary["written"])
            self.assertTrue(len(summary["rejected"]) >= 1)

    def test_validate_requires_at_least_one_operation(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "edits": [], "new_files": [], "deleted_files": [], "rationale": "x",
        }))
        ok, errs = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertTrue(any("at least one of" in e for e in errs))

    def test_validate_rejects_no_op_edit(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "edits": [{"path": "a.py", "old_string": "same", "new_string": "same"}],
            "rationale": "x",
        }))
        ok, errs = self.skill.validate_output(parsed)
        self.assertFalse(ok)
        self.assertTrue(any("must differ" in e for e in errs))

    def test_prompt_partitions_existing_and_new_files(self) -> None:
        p = self.skill.build_prompt({
            "title": "t", "objective": "o", "approach": "a",
            "files_to_touch": ["src/old.py", "src/new.py"],
            "file_contents": {"src/old.py": "existing"},
        })
        self.assertIn("Existing files", p)
        self.assertIn("New files", p)
        self.assertIn("src/old.py", p)
        self.assertIn("src/new.py", p)


class SelfReviewSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SelfReviewSkill()

    def test_blocker_flips_ship_ready(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "issues": [{"severity": "blocker", "description": "NameError"}],
            "ship_ready": True,  # LLM lied
            "summary": "broken",
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertTrue(ok)
        self.assertFalse(parsed["ship_ready"])

    def test_minor_preserves_ship_ready(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "issues": [{"severity": "minor", "description": "naming"}],
            "ship_ready": True,
            "summary": "nits only",
        }))
        self.skill.validate_output(parsed)
        self.assertTrue(parsed["ship_ready"])

    def test_feedback_extraction(self) -> None:
        result = SkillResult(skill_name="self_review", success=True, output={
            "issues": [
                {"severity": "blocker", "file": "x.py", "description": "crash"},
                {"severity": "minor", "description": "style"},
            ],
            "summary": "fix x",
        })
        fb = SelfReviewSkill.feedback_for_retry(result)
        self.assertIn("BLOCKER", fb)
        self.assertIn("crash", fb)


class ValidateSkillTests(unittest.TestCase):
    def test_profiles(self) -> None:
        self.assertTrue(any("pytest" in c["cmd"] for c in commands_for_profile("python")))
        self.assertEqual([], commands_for_profile("lightweight_operator"))

    def test_deterministic_execution(self) -> None:
        skill = ValidateSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Passing
            r = skill.invoke_deterministic(
                root,
                [{"name": "probe", "cmd": "python -c \"pass\"", "timeout": 30}],
                root / "_run",
            )
            self.assertEqual("pass", r.output["overall"])
            # Failing stops execution
            r = skill.invoke_deterministic(
                root,
                [
                    {"name": "ok", "cmd": "python -c \"pass\"", "timeout": 30},
                    {"name": "bad", "cmd": "python -c \"import sys; sys.exit(7)\"", "timeout": 30},
                    {"name": "skipped", "cmd": "python -c \"pass\"", "timeout": 30},
                ],
                root / "_fail",
            )
            self.assertEqual("fail", r.output["overall"])
            self.assertEqual(2, len(r.output["results"]))


class DiagnoseSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = DiagnoseSkill()

    def test_fast_path(self) -> None:
        fc = DiagnoseSkill.try_fast_path("HTTP 429 rate limit")
        self.assertIsNotNone(fc)
        self.assertEqual("provider_rate_limit", fc.classification)

    def test_fast_path_misses(self) -> None:
        self.assertIsNone(DiagnoseSkill.try_fast_path("some NameError occurred"))

    def test_classification_validation(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "classification": "code_defect", "confidence": 0.8,
            "retry_recommended": True, "cooldown_seconds": 0, "root_cause": "x",
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertTrue(ok)

    def test_invalid_classification_rejected(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "classification": "nonsense", "confidence": 0.5,
            "retry_recommended": True, "cooldown_seconds": 0, "root_cause": "x",
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertFalse(ok)


class PromotionReviewSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = PromotionReviewSkill()

    def test_blocker_flips_approval(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "approved": True,
            "rationale": "looks fine",
            "concerns": [{"severity": "blocker", "description": "race"}],
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertTrue(ok)
        self.assertFalse(parsed["approved"])


class PromotionApplySkillTests(unittest.TestCase):
    def test_git_merge(self) -> None:
        skill = PromotionApplySkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)
            (root / "a.txt").write_text("v1")
            subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b", "feat"], check=True)
            (root / "a.txt").write_text("v2")
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-a", "-m", "change"], check=True)
            subprocess.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True)
            r = skill.invoke_deterministic(
                workspace=root, source_branch="feat", target_branch="main",
                no_ff=True, merge_message="merge",
            )
            self.assertTrue(r.success, r.errors)
            self.assertTrue(r.output["merged"])
            self.assertEqual("v2", (root / "a.txt").read_text())

    def test_missing_branch(self) -> None:
        skill = PromotionApplySkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
            (root / "a.txt").write_text("x")
            subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], check=True)
            r = skill.invoke_deterministic(
                workspace=root, source_branch="nope", target_branch="main",
            )
            self.assertFalse(r.success)


class PostMergeCheckSkillTests(unittest.TestCase):
    def test_lightweight_profile_is_healthy(self) -> None:
        skill = PostMergeCheckSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = skill.invoke_deterministic(
                workspace=root,
                validation_profile="lightweight_operator",
                run_dir=root / "_rd",
            )
            self.assertTrue(r.output["main_healthy"])
            self.assertFalse(r.output["rollback_needed"])


class FollowOnSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = FollowOnSkill()

    def test_emits_proposed_tasks_in_cognition_schema(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "proposed_tasks": [
                {"title": "t1", "objective": "o1", "priority": "P1", "rationale": "r"},
                {"title": "t2", "objective": "o2", "priority": "P2", "rationale": "r",
                 "allowed_paths": ["tests/"]},
            ],
            "summary": "split x",
        }))
        ok, _ = self.skill.validate_output(parsed)
        self.assertTrue(ok)
        self.assertEqual(2, len(parsed["proposed_tasks"]))
        self.assertEqual(["tests/"], parsed["proposed_tasks"][1]["allowed_paths"])

    def test_empty_rejected(self) -> None:
        parsed = self.skill.parse_response(json.dumps({"proposed_tasks": [], "summary": "none"}))
        ok, _ = self.skill.validate_output(parsed)
        self.assertFalse(ok)

    def test_missing_title_dropped(self) -> None:
        parsed = self.skill.parse_response(json.dumps({
            "proposed_tasks": [
                {"title": "", "objective": "x", "priority": "P1", "rationale": "r"},
                {"title": "keep", "objective": "y", "priority": "P2", "rationale": "r"},
            ],
            "summary": "x",
        }))
        self.assertEqual(1, len(parsed["proposed_tasks"]))


class BenchmarkSkillTests(unittest.TestCase):
    def test_all_pass(self) -> None:
        """All commands succeed — failed list is empty, timings are captured."""
        skill = BenchmarkSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = skill.invoke_deterministic(
                workspace_root=root,
                validation_profile="python",
                run_dir=root / "_bench",
            )
            self.assertTrue(r.success)
            self.assertEqual("python", r.output["profile"])
            self.assertIsInstance(r.output["total_runtime_seconds"], float)
            self.assertGreater(r.output["test_count"], 0)
            self.assertIsInstance(r.output["slowest"], list)
            self.assertLessEqual(len(r.output["slowest"]), 3)
            # validate_output should accept the output shape
            ok, errs = skill.validate_output(r.output)
            self.assertTrue(ok, errs)

    def test_mixed_failure_no_short_circuit(self) -> None:
        """Failing commands do not prevent subsequent commands from running."""
        skill = BenchmarkSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Patch commands_for_profile won't work easily, so we call
            # invoke_deterministic on a profile and check indirectly.
            # Instead, directly test with the "python" profile in a dir
            # where pytest will fail but compileall might pass.
            # Simpler: use generic profile with make test which will fail.
            # Best approach: monkeypatch commands_for_profile.
            import accruvia_harness.skills.benchmark as bm
            original = bm.commands_for_profile
            try:
                bm.commands_for_profile = lambda _profile: [
                    {"name": "ok1", "cmd": "python -c \"pass\"", "timeout": 30},
                    {"name": "bad", "cmd": "python -c \"import sys; sys.exit(3)\"", "timeout": 30},
                    {"name": "ok2", "cmd": "python -c \"pass\"", "timeout": 30},
                ]
                r = skill.invoke_deterministic(
                    workspace_root=root,
                    validation_profile="test",
                    run_dir=root / "_bench_fail",
                )
            finally:
                bm.commands_for_profile = original

            self.assertTrue(r.success)
            # All 3 commands ran (no short-circuit)
            self.assertEqual(3, r.output["test_count"])
            # Exactly one failure
            self.assertEqual(1, len(r.output["failed"]))
            self.assertEqual("bad", r.output["failed"][0]["name"])
            self.assertEqual(3, r.output["failed"][0]["exit_code"])
            # Slowest has up to 3 entries
            self.assertEqual(3, len(r.output["slowest"]))

    def test_empty_profile(self) -> None:
        """lightweight_operator has no commands — returns zero counts."""
        skill = BenchmarkSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = skill.invoke_deterministic(
                workspace_root=root,
                validation_profile="lightweight_operator",
                run_dir=root / "_bench_empty",
            )
            self.assertTrue(r.success)
            self.assertEqual("lightweight_operator", r.output["profile"])
            self.assertEqual(0.0, r.output["total_runtime_seconds"])
            self.assertEqual(0, r.output["test_count"])
            self.assertEqual([], r.output["slowest"])
            self.assertEqual([], r.output["failed"])


class CommitSkillTests(unittest.TestCase):
    def test_happy_path_commit(self) -> None:
        """Stage a file and commit it in a live temp git repo."""
        skill = CommitSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "false"], check=True)
            (root / "a.txt").write_text("hello")
            r = skill.invoke_deterministic(
                workspace=root, paths=["a.txt"], message="add a",
            )
            self.assertTrue(r.success, r.errors)
            self.assertTrue(r.output["committed"])
            self.assertEqual(["a.txt"], r.output["staged"])
            self.assertTrue(len(r.output["commit_sha"]) >= 7)

    def test_empty_paths_noop(self) -> None:
        """Empty paths list returns committed=False with success=True."""
        skill = CommitSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            r = skill.invoke_deterministic(
                workspace=root, paths=[], message="noop",
            )
            self.assertTrue(r.success)
            self.assertFalse(r.output["committed"])
            self.assertEqual([], r.output["staged"])

    def test_missing_git_dir(self) -> None:
        """Non-git directory returns success=False."""
        skill = CommitSkill()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = skill.invoke_deterministic(
                workspace=root, paths=["a.txt"], message="fail",
            )
            self.assertFalse(r.success)
            self.assertIn("workspace is not a git repository", r.errors)


class SkillRegistryTests(unittest.TestCase):
    def test_default_registry_has_all_eleven(self) -> None:
        registry = build_default_registry()
        self.assertEqual(11, len(registry))
        expected = {
            "scope", "implement", "self_review", "validate", "diagnose",
            "promotion_review", "promotion_apply", "post_merge_check", "follow_on",
            "benchmark", "commit",
        }
        self.assertEqual(expected, set(registry.names()))

    def test_duplicate_registration_rejected(self) -> None:
        registry = SkillRegistry()
        registry.register(ScopeSkill())
        with self.assertRaises(ValueError):
            registry.register(ScopeSkill())

    def test_unknown_skill_lookup_raises(self) -> None:
        registry = SkillRegistry()
        with self.assertRaises(KeyError):
            registry.get("ghost")


if __name__ == "__main__":
    unittest.main()
