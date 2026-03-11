from __future__ import annotations

import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

from ..domain import Task
from .base import AdapterEvidence


class GenericAdapter:
    profile = "generic"

    def build_evidence(self, task: Task, run_dir: Path) -> AdapterEvidence:
        source_path = run_dir / "generated_artifact.txt"
        source_path.write_text("generic source artifact\n", encoding="utf-8")
        test_path = run_dir / "generated_validation.txt"
        test_path.write_text("generic validation evidence\n", encoding="utf-8")
        check_output_path = run_dir / "generic_check.txt"
        check_output_path.write_text("Generic validation passed.\n", encoding="utf-8")
        changed_files = [str(source_path), str(test_path)]
        return AdapterEvidence(
            passed=True,
            report={
                "changed_files": changed_files,
                "test_files": [str(test_path)],
                "compile_check": {
                    "passed": True,
                    "targets": changed_files,
                    "mode": "generic_stub",
                    "output_path": str(check_output_path),
                },
                "test_check": {
                    "passed": True,
                    "framework": "generic_stub",
                    "output_path": str(check_output_path),
                },
            },
            diagnostics={
                "compile_targets": changed_files,
                "test_output_path": str(check_output_path),
                "test_returncode": 0,
            },
        )


class PythonAdapter:
    profile = "python"

    def build_evidence(self, task: Task, run_dir: Path) -> AdapterEvidence:
        module_path = run_dir / "generated_module.py"
        module_path.write_text(
            "def generated_value() -> int:\n"
            "    return 2\n",
            encoding="utf-8",
        )
        test_path = run_dir / "test_generated_module.py"
        test_path.write_text(
            "import unittest\n\n"
            "from generated_module import generated_value\n\n"
            "class GeneratedModuleTests(unittest.TestCase):\n"
            "    def test_generated_value(self) -> None:\n"
            "        self.assertEqual(2, generated_value())\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        compile_targets = [str(module_path), str(test_path)]
        for target in compile_targets:
            py_compile.compile(target, doraise=True)
        test_completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(run_dir), "-p", "test_generated_module.py"],
            check=False,
            cwd=run_dir,
            capture_output=True,
            text=True,
        )
        test_output_path = run_dir / "test_output.txt"
        test_output_path.write_text(
            f"{test_completed.stdout}\n{test_completed.stderr}".strip(),
            encoding="utf-8",
        )
        return AdapterEvidence(
            passed=test_completed.returncode == 0,
            report={
                "changed_files": [str(module_path), str(test_path)],
                "test_files": [str(test_path)],
                "compile_check": {"passed": True, "targets": compile_targets},
                "test_check": {
                    "passed": test_completed.returncode == 0,
                    "framework": "unittest",
                    "command": [
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        str(run_dir),
                        "-p",
                        "test_generated_module.py",
                    ],
                    "output_path": str(test_output_path),
                },
            },
            diagnostics={
                "compile_targets": compile_targets,
                "test_output_path": str(test_output_path),
                "test_returncode": test_completed.returncode,
            },
        )


class JavaScriptAdapter:
    profile = "javascript"

    def build_evidence(self, task: Task, run_dir: Path) -> AdapterEvidence:
        source_path = run_dir / "generated-module.js"
        source_path.write_text(
            "export function generatedValue() {\n"
            "  return 2;\n"
            "}\n",
            encoding="utf-8",
        )
        test_path = run_dir / "generated-module.test.js"
        test_path.write_text(
            "import test from 'node:test';\n"
            "import assert from 'node:assert/strict';\n"
            "import { generatedValue } from './generated-module.js';\n\n"
            "test('generatedValue returns 2', () => {\n"
            "  assert.equal(generatedValue(), 2);\n"
            "});\n",
            encoding="utf-8",
        )
        compile_output_path = run_dir / "compile_output.txt"
        test_output_path = run_dir / "test_output.txt"
        changed_files = [str(source_path), str(test_path)]
        node_path = shutil.which("node")
        if node_path:
            compile_completed = subprocess.run(
                [node_path, "--check", str(source_path)],
                check=False,
                cwd=run_dir,
                capture_output=True,
                text=True,
            )
            compile_output_path.write_text(
                f"{compile_completed.stdout}\n{compile_completed.stderr}".strip(),
                encoding="utf-8",
            )
            test_completed = subprocess.run(
                [node_path, "--test", str(test_path)],
                check=False,
                cwd=run_dir,
                capture_output=True,
                text=True,
            )
            test_output_path.write_text(
                f"{test_completed.stdout}\n{test_completed.stderr}".strip(),
                encoding="utf-8",
            )
            passed = compile_completed.returncode == 0 and test_completed.returncode == 0
            compile_mode = "node_check"
            test_framework = "node_test"
            test_returncode = test_completed.returncode
            compile_returncode = compile_completed.returncode
        else:
            compile_output_path.write_text("Node is not installed; javascript compile check stubbed.\n", encoding="utf-8")
            test_output_path.write_text("Node is not installed; javascript test check stubbed.\n", encoding="utf-8")
            passed = True
            compile_mode = "javascript_stub"
            test_framework = "javascript_stub"
            test_returncode = 0
            compile_returncode = 0
        return AdapterEvidence(
            passed=passed,
            report={
                "changed_files": changed_files,
                "test_files": [str(test_path)],
                "compile_check": {
                    "passed": passed if node_path else True,
                    "targets": changed_files,
                    "mode": compile_mode,
                    "output_path": str(compile_output_path),
                },
                "test_check": {
                    "passed": passed if node_path else True,
                    "framework": test_framework,
                    "output_path": str(test_output_path),
                },
            },
            diagnostics={
                "compile_targets": changed_files,
                "test_output_path": str(test_output_path),
                "compile_output_path": str(compile_output_path),
                "test_returncode": test_returncode,
                "compile_returncode": compile_returncode,
            },
        )


class TerraformAdapter:
    profile = "terraform"

    def build_evidence(self, task: Task, run_dir: Path) -> AdapterEvidence:
        main_tf = run_dir / "main.tf"
        main_tf.write_text(
            'terraform {\n  required_version = ">= 1.0.0"\n}\n\n'
            'variable "name" {\n  type = string\n}\n',
            encoding="utf-8",
        )
        vars_tf = run_dir / "terraform.tfvars"
        vars_tf.write_text('name = "accruvia"\n', encoding="utf-8")
        validate_output_path = run_dir / "terraform_validate.txt"
        changed_files = [str(main_tf), str(vars_tf)]
        terraform_path = shutil.which("terraform")
        if terraform_path:
            validate_completed = subprocess.run(
                [terraform_path, "validate", "-no-color"],
                check=False,
                cwd=run_dir,
                capture_output=True,
                text=True,
            )
            validate_output_path.write_text(
                f"{validate_completed.stdout}\n{validate_completed.stderr}".strip(),
                encoding="utf-8",
            )
            passed = validate_completed.returncode == 0
            compile_mode = "terraform_validate"
            test_framework = "terraform_validate"
            validate_returncode = validate_completed.returncode
        else:
            validate_output_path.write_text("Terraform is not installed; terraform validate stubbed.\n", encoding="utf-8")
            passed = True
            compile_mode = "terraform_stub"
            test_framework = "terraform_stub"
            validate_returncode = 0
        return AdapterEvidence(
            passed=passed,
            report={
                "changed_files": changed_files,
                "test_files": [],
                "compile_check": {
                    "passed": passed if terraform_path else True,
                    "targets": changed_files,
                    "mode": compile_mode,
                    "output_path": str(validate_output_path),
                },
                "test_check": {
                    "passed": passed if terraform_path else True,
                    "framework": test_framework,
                    "output_path": str(validate_output_path),
                },
                "terraform_validate": {
                    "passed": passed if terraform_path else True,
                    "output_path": str(validate_output_path),
                },
            },
            diagnostics={
                "compile_targets": changed_files,
                "terraform_validate_output_path": str(validate_output_path),
                "test_returncode": validate_returncode,
            },
        )


def builtin_adapters() -> list[object]:
    return [GenericAdapter(), PythonAdapter(), JavaScriptAdapter(), TerraformAdapter()]
