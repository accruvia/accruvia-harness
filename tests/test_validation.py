from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from accruvia_harness.domain import Artifact, Task, new_id
from accruvia_harness.validation import (
    ChangedFilesValidator,
    CompileCheckValidator,
    JavaScriptTestFileValidator,
    PythonTestFileValidator,
    RequiredArtifactsValidator,
    TerraformValidationValidator,
    TestEvidenceValidator,
    validators_for_profile,
)


class DeterministicValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.task = Task(
            id=new_id("task"),
            project_id=new_id("project"),
            title="Validation task",
            objective="Validate deterministic promotion evidence",
            required_artifacts=["plan", "report"],
        )
        self.plan_path = self.base / "plan.txt"
        self.plan_path.write_text("plan", encoding="utf-8")
        self.report_path = self.base / "report.json"
        self.report_path.write_text(
            json.dumps(
                {
                    "changed_files": ["src/example.py", "tests/test_example.py"],
                    "test_files": ["tests/test_example.py"],
                    "compile_check": {"passed": True},
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        self.artifacts = [
            Artifact(id=new_id("artifact"), run_id=new_id("run"), kind="plan", path=str(self.plan_path), summary="plan"),
            Artifact(id=new_id("artifact"), run_id=new_id("run"), kind="report", path=str(self.report_path), summary="report"),
        ]

    def test_required_artifacts_validator_passes_with_plan_and_report(self) -> None:
        result = RequiredArtifactsValidator().validate(self.task, self.artifacts)
        self.assertTrue(result.ok)

    def test_changed_files_validator_requires_non_empty_changed_files(self) -> None:
        result = ChangedFilesValidator().validate(self.task, self.artifacts)
        self.assertTrue(result.ok)

        self.report_path.write_text(json.dumps({}), encoding="utf-8")
        result = ChangedFilesValidator().validate(self.task, self.artifacts)
        self.assertFalse(result.ok)
        self.assertEqual("missing_changed_files", result.issues[0].code)

    def test_compile_check_validator_requires_passing_compile_check(self) -> None:
        result = CompileCheckValidator().validate(self.task, self.artifacts)
        self.assertTrue(result.ok)

        self.report_path.write_text(json.dumps({"compile_check": {"passed": False}}), encoding="utf-8")
        result = CompileCheckValidator().validate(self.task, self.artifacts)
        self.assertFalse(result.ok)
        self.assertEqual("compile_check_failed", result.issues[0].code)

    def test_test_evidence_validator_requires_test_files_and_passing_test_check(self) -> None:
        result = TestEvidenceValidator().validate(self.task, self.artifacts)
        self.assertTrue(result.ok)

        self.report_path.write_text(
            json.dumps({"test_files": ["tests/test_example.py"], "test_check": {"passed": False}}),
            encoding="utf-8",
        )
        result = TestEvidenceValidator().validate(self.task, self.artifacts)
        self.assertFalse(result.ok)
        self.assertEqual("missing_test_evidence", result.issues[0].code)

    def test_python_profile_validator_requires_python_tests(self) -> None:
        self.task.validation_profile = "python"
        self.report_path.write_text(
            json.dumps(
                {
                    "validation_profile": "python",
                    "test_files": ["tests/test_example.py"],
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(PythonTestFileValidator().validate(self.task, self.artifacts).ok)

    def test_javascript_profile_validator_requires_js_or_ts_tests(self) -> None:
        self.task.validation_profile = "javascript"
        self.report_path.write_text(
            json.dumps(
                {
                    "validation_profile": "javascript",
                    "test_files": ["tests/example.test.ts"],
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(JavaScriptTestFileValidator().validate(self.task, self.artifacts).ok)

        self.report_path.write_text(
            json.dumps(
                {
                    "validation_profile": "javascript",
                    "test_files": ["tests/test_example.py"],
                    "test_check": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        result = JavaScriptTestFileValidator().validate(self.task, self.artifacts)
        self.assertFalse(result.ok)
        self.assertEqual("javascript_test_file_mismatch", result.issues[0].code)

    def test_terraform_profile_validator_requires_tf_evidence(self) -> None:
        self.task.validation_profile = "terraform"
        self.report_path.write_text(
            json.dumps(
                {
                    "validation_profile": "terraform",
                    "changed_files": ["infra/main.tf", "infra/vars.tfvars"],
                    "terraform_validate": {"passed": True},
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(TerraformValidationValidator().validate(self.task, self.artifacts).ok)

    def test_validators_for_profile_returns_profile_specific_bundles(self) -> None:
        python_validators = [validator.__class__.__name__ for validator in validators_for_profile("python")]
        javascript_validators = [validator.__class__.__name__ for validator in validators_for_profile("javascript")]
        terraform_validators = [validator.__class__.__name__ for validator in validators_for_profile("terraform")]

        self.assertIn("PythonTestFileValidator", python_validators)
        self.assertIn("JavaScriptTestFileValidator", javascript_validators)
        self.assertIn("TerraformValidationValidator", terraform_validators)
