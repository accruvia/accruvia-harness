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

    def test_scope_enforcement_in_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "changed_files": [
                        {"path": "src/a.py", "content": "ok"},
                        {"path": "src/forbidden.py", "content": "bad"},
                    ],
                    "deleted_files": [],
                },
            )
            summary = apply_changes(
                result,
                workspace_root=root,
                allowed_files=["src/a.py"],
            )
            self.assertIn("src/a.py", summary["written"])
            self.assertEqual(1, len(summary["rejected"]))
            self.assertTrue((root / "src" / "a.py").exists())
            self.assertFalse((root / "src" / "forbidden.py").exists())

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SkillResult(
                skill_name="implement", success=True,
                output={
                    "changed_files": [{"path": "../etc/passwd", "content": "bad"}],
                    "deleted_files": [],
                },
            )
            summary = apply_changes(result, workspace_root=root, allowed_files=[])
            self.assertEqual([], summary["written"])
            self.assertTrue(len(summary["rejected"]) >= 1)


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


class SkillRegistryTests(unittest.TestCase):
    def test_default_registry_has_all_nine(self) -> None:
        registry = build_default_registry()
        self.assertEqual(9, len(registry))
        expected = {
            "scope", "implement", "self_review", "validate", "diagnose",
            "promotion_review", "promotion_apply", "post_merge_check", "follow_on",
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
