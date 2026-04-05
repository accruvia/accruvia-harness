"""The /implement skill — engineer perspective.

Takes a scope (from /scope) plus task and repo context and produces the actual
code changes. Returns structured {changed_files: [{path, content}], rationale}
that a deterministic materializer writes to the workspace.

This skill replaces the external "worker CLI writes report.json and hopes"
contract. The harness owns the prompt, the LLM fills the role, and Python
writes the files with scope validation.

For the MVP, /implement returns full file contents. Larger files with complex
edits should be flagged as too_large by /scope so the task gets split.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class ImplementSkill:
    """Generates code edits for files listed in the scope."""

    name = "implement"
    output_schema: dict[str, Any] = {
        "required": ["changed_files", "rationale"],
        "types": {
            "changed_files": "list",
            "rationale": "str",
            "deleted_files": "list",
        },
    }

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        title = str(inputs.get("title") or "").strip()
        objective = str(inputs.get("objective") or "").strip()
        approach = str(inputs.get("approach") or "").strip()
        files_to_touch = list(inputs.get("files_to_touch") or [])
        files_not_to_touch = list(inputs.get("files_not_to_touch") or [])
        risks = list(inputs.get("risks") or [])
        file_contents = dict(inputs.get("file_contents") or {})
        retry_feedback = str(inputs.get("retry_feedback") or "").strip()

        files_block = "\n".join(f"  - {p}" for p in files_to_touch)
        no_touch_block = ""
        if files_not_to_touch:
            no_touch_block = (
                "DO NOT modify these files under any circumstances:\n"
                + "\n".join(f"  - {p}" for p in files_not_to_touch)
            )
        risks_block = ""
        if risks:
            risks_block = "Known risks for this scope:\n" + "\n".join(
                f"  - {r}" for r in risks
            )

        existing_block_parts = []
        for path, content in file_contents.items():
            truncated = str(content)[:8000]
            existing_block_parts.append(
                f"=== {path} ===\n{truncated}\n=== end {path} ==="
            )
        existing_block = "\n\n".join(existing_block_parts) if existing_block_parts else ""

        retry_block = ""
        if retry_feedback:
            retry_block = (
                "This is a retry. Prior attempt failed. Feedback follows — fix "
                "the specific issue, do not rewrite unrelated code.\n"
                f"Feedback: {retry_feedback}"
            )

        return "\n\n".join(
            filter(
                None,
                [
                    "You are the engineer implementing a single scoped task. You have "
                    "already received a scope from the tech lead. Do NOT widen the scope. "
                    "Do NOT touch files outside files_to_touch. Return the full new "
                    "content for each file you edit or create.",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    f"Approach (from scope): {approach}",
                    f"Files to touch:\n{files_block}",
                    no_touch_block,
                    risks_block,
                    retry_block,
                    "Existing file contents (for reference — use these verbatim as the "
                    "base, only change what the objective requires):\n\n" + existing_block
                    if existing_block
                    else "",
                    "Return strict JSON with keys:\n"
                    "  changed_files (list of objects with keys 'path' and 'content'; "
                    "content is the full new file content, not a diff)\n"
                    "  deleted_files (list of file paths to delete; empty if none)\n"
                    "  rationale (string, 1-3 sentences describing what changed and why)",
                    "CRITICAL: changed_files[*].path MUST be one of files_to_touch. "
                    "Any path outside that list will be rejected. The content field MUST "
                    "be the complete file content, not a patch or diff fragment.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("deleted_files", [])
        if isinstance(parsed.get("changed_files"), list):
            normalized: list[dict[str, str]] = []
            for entry in parsed["changed_files"]:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").strip()
                content = entry.get("content")
                if not path or content is None:
                    continue
                normalized.append({"path": path, "content": str(content)})
            parsed["changed_files"] = normalized
        if isinstance(parsed.get("deleted_files"), list):
            parsed["deleted_files"] = [str(p) for p in parsed["deleted_files"] if p]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        changed = parsed.get("changed_files") or []
        if not changed:
            return False, ["changed_files must contain at least one file"]
        for entry in changed:
            if not isinstance(entry, dict):
                return False, ["each changed_files entry must be an object"]
            if "path" not in entry or "content" not in entry:
                return False, ["each changed_files entry must have 'path' and 'content'"]
        if not parsed.get("rationale", "").strip():
            return False, ["rationale must be non-empty"]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        # File writes happen via apply_changes(), called by the orchestrator
        # after scope validation. Keeping the skill itself side-effect-free
        # lets the caller control where workspace writes land.
        return None


def apply_changes(
    result: SkillResult,
    *,
    workspace_root: Path,
    allowed_files: list[str],
) -> dict[str, Any]:
    """Apply changed_files from an /implement result to the workspace.

    Enforces that every written path was in the scope's allowed_files list.
    Returns a summary: {written: [...], deleted: [...], rejected: [...]}.
    """
    written: list[str] = []
    deleted: list[str] = []
    rejected: list[dict[str, str]] = []
    allowed_set = {p.replace("\\", "/").lstrip("./") for p in allowed_files}

    workspace_root = Path(workspace_root).resolve()

    for entry in result.output.get("changed_files") or []:
        path_str = str(entry.get("path") or "").replace("\\", "/").lstrip("./")
        if not path_str:
            continue
        if path_str not in allowed_set:
            rejected.append({"path": path_str, "reason": "out_of_scope"})
            continue
        target = (workspace_root / path_str).resolve()
        try:
            target.relative_to(workspace_root)
        except ValueError:
            rejected.append({"path": path_str, "reason": "path_escape"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(entry.get("content") or ""), encoding="utf-8")
        written.append(path_str)

    for path_str in result.output.get("deleted_files") or []:
        path_str = str(path_str).replace("\\", "/").lstrip("./")
        if path_str not in allowed_set:
            rejected.append({"path": path_str, "reason": "delete_out_of_scope"})
            continue
        target = (workspace_root / path_str).resolve()
        try:
            target.relative_to(workspace_root)
        except ValueError:
            rejected.append({"path": path_str, "reason": "path_escape"})
            continue
        if target.exists():
            target.unlink()
            deleted.append(path_str)

    return {"written": written, "deleted": deleted, "rejected": rejected}
