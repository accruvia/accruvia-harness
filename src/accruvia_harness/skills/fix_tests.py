"""The /fix-tests skill — update test assertions after behavior changes.

When /implement changes behavior and /validate fails because existing tests
assert on old behavior, this skill reads the test failure output + the diff
and produces targeted test edits that align assertions with the new behavior.

This is NOT about fixing bugs in the implementation. It's about updating
test expectations when a deliberate behavior change makes old assertions
wrong. The caller (/diagnose) must determine this is the case before
invoking /fix-tests.

Output format matches /implement's edit-list so apply_changes can be
reused directly.
"""
from __future__ import annotations

from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class FixTestsSkill:
    """Reads test failure output + diff + test file and patches assertions."""

    name = "fix_tests"
    output_schema: dict[str, Any] = {
        "required": ["edits", "rationale"],
        "types": {
            "edits": "list",
            "rationale": "str",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        title = str(inputs.get("title") or "").strip()
        objective = str(inputs.get("objective") or "").strip()
        diff = str(inputs.get("diff") or "").strip()
        failure_output = str(inputs.get("failure_output") or "").strip()
        test_file_path = str(inputs.get("test_file_path") or "").strip()
        test_file_content = str(inputs.get("test_file_content") or "").strip()

        return "\n\n".join(
            filter(
                None,
                [
                    "You are fixing test assertions that fail because the implementation "
                    "deliberately changed behavior. The implementation is CORRECT — do NOT "
                    "revert it or change production code. Only fix the TEST file.",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    "The following diff was applied (this is the correct implementation):\n"
                    + (diff or "(no diff)"),
                    f"Test file: {test_file_path}",
                    "Test failure output (pytest):\n" + (failure_output or "(no output)"),
                    "Current test file content:\n" + (test_file_content or "(missing)"),
                    "Return strict JSON with keys:\n"
                    "  edits (list of {path, old_string, new_string} — path MUST be the test file)\n"
                    "  rationale (string explaining what assertions changed and why)",
                    "RULES:\n"
                    "  - ONLY edit the test file. Never edit production code.\n"
                    "  - old_string must be UNIQUE in the test file.\n"
                    "  - Update assertions to match the NEW correct behavior.\n"
                    "  - If a test method is no longer valid (tests removed behavior), "
                    "delete the entire method by replacing it with empty string.\n"
                    "  - Keep test coverage: don't delete tests that are still valid.\n"
                    "  - Produce minimal edits — only change what the failure output "
                    "indicates is wrong.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("edits", [])
        if isinstance(parsed.get("edits"), list):
            normalized: list[dict[str, str]] = []
            for entry in parsed["edits"]:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").strip()
                old = entry.get("old_string")
                new = entry.get("new_string")
                if not path or old is None or new is None:
                    continue
                normalized.append(
                    {"path": path, "old_string": str(old), "new_string": str(new)}
                )
            parsed["edits"] = normalized
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        edits = parsed.get("edits") or []
        if not edits:
            return False, ["edits must contain at least one fix"]
        for entry in edits:
            if not isinstance(entry, dict):
                return False, ["each edits entry must be an object"]
            if not all(k in entry for k in ("path", "old_string", "new_string")):
                return False, ["each edits entry must have path, old_string, new_string"]
        if not parsed.get("rationale", "").strip():
            return False, ["rationale must be non-empty"]
        return True, []

    def materialize(self, store: Any, result: SkillResult, inputs: dict[str, Any]) -> None:
        return None
