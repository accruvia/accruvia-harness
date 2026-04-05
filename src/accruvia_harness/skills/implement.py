"""The /implement skill — engineer perspective with edit-list output.

Takes a scope (from /scope) plus task and repo context and produces code
changes in one of two shapes:

    edits:      targeted str-replace operations on existing files
                [{path, old_string, new_string}, ...]
    new_files:  full content for files that don't yet exist
                [{path, content}, ...]

An edit-list model, modeled on Claude Code's own Edit tool, is safer than
full-file rewrites at medium/large file sizes: a mistake in one edit does
not corrupt the rest of the file. The LLM is asked to pick small, uniquely
identifiable `old_string` anchors so replacements are unambiguous.

`apply_changes()` applies each operation deterministically with scope
validation, uniqueness checks, and per-operation success reporting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import SkillResult, extract_json_payload, validate_against_schema


class ImplementSkill:
    """Generates targeted edits + new files for the scoped task."""

    name = "implement"
    output_schema: dict[str, Any] = {
        "required": ["rationale"],
        "types": {
            "edits": "list",
            "new_files": "list",
            "deleted_files": "list",
            "rationale": "str",
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

        # Partition files_to_touch into existing (edit via edits) and new (via new_files).
        existing_paths = {p for p in files_to_touch if p in file_contents}
        new_paths = [p for p in files_to_touch if p not in existing_paths]

        files_block = []
        if existing_paths:
            files_block.append(
                "Existing files — use `edits` with targeted old_string/new_string:\n"
                + "\n".join(f"  - {p}" for p in sorted(existing_paths))
            )
        if new_paths:
            files_block.append(
                "New files — use `new_files` with full content:\n"
                + "\n".join(f"  - {p}" for p in new_paths)
            )

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
            truncated = str(content)[:12000]
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
                    "Do NOT touch files outside files_to_touch.",
                    f"Task title: {title}",
                    f"Task objective: {objective}",
                    f"Approach (from scope): {approach}",
                    "\n\n".join(files_block),
                    no_touch_block,
                    risks_block,
                    retry_block,
                    "Existing file contents (verbatim — reference these when composing "
                    "old_string anchors):\n\n" + existing_block
                    if existing_block
                    else "",
                    "Return strict JSON with keys:\n"
                    "  edits (list of {path, old_string, new_string} objects for existing files)\n"
                    "  new_files (list of {path, content} objects for files that do not exist yet)\n"
                    "  deleted_files (list of paths to delete; [] if none)\n"
                    "  rationale (1-3 sentence summary of what changed and why)",
                    "EDIT RULES (critical):\n"
                    "  - `old_string` MUST appear EXACTLY ONCE in the target file. Pick "
                    "enough surrounding context that the match is unique. If a snippet "
                    "repeats (e.g. `return None`), extend it with the preceding signature.\n"
                    "  - `new_string` replaces `old_string` verbatim. Preserve indentation "
                    "and line endings.\n"
                    "  - Produce ONE edit per discrete change. Do not bundle unrelated "
                    "changes into a single edit. Smaller edits fail-safely.\n"
                    "  - Never use edits to create a file. Put new files in `new_files`.\n"
                    "  - path MUST be one of files_to_touch. Out-of-scope paths are rejected.\n"
                    "  - When adding a new import alongside existing imports, anchor on a "
                    "neighboring import line so the edit is unambiguous.",
                    "If you have no edits, return `\"edits\": []`. If you have no new "
                    "files, return `\"new_files\": []`. At least one of edits, new_files, "
                    "or deleted_files MUST be non-empty.",
                ],
            )
        ).strip()

    def parse_response(self, response_text: str) -> dict[str, Any]:
        parsed = extract_json_payload(response_text)
        if parsed is None:
            return {}
        parsed.setdefault("edits", [])
        parsed.setdefault("new_files", [])
        parsed.setdefault("deleted_files", [])
        if isinstance(parsed.get("edits"), list):
            normalized_edits: list[dict[str, str]] = []
            for entry in parsed["edits"]:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").strip()
                old = entry.get("old_string")
                new = entry.get("new_string")
                if not path or old is None or new is None:
                    continue
                normalized_edits.append(
                    {"path": path, "old_string": str(old), "new_string": str(new)}
                )
            parsed["edits"] = normalized_edits
        if isinstance(parsed.get("new_files"), list):
            normalized_new: list[dict[str, str]] = []
            for entry in parsed["new_files"]:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").strip()
                content = entry.get("content")
                if not path or content is None:
                    continue
                normalized_new.append({"path": path, "content": str(content)})
            parsed["new_files"] = normalized_new
        if isinstance(parsed.get("deleted_files"), list):
            parsed["deleted_files"] = [str(p) for p in parsed["deleted_files"] if p]
        return parsed

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, errors = validate_against_schema(parsed, self.output_schema)
        if not ok:
            return ok, errors
        edits = parsed.get("edits") or []
        new_files = parsed.get("new_files") or []
        deleted = parsed.get("deleted_files") or []
        if not edits and not new_files and not deleted:
            return False, ["must have at least one of: edits, new_files, deleted_files"]
        for entry in edits:
            if not isinstance(entry, dict):
                return False, ["each edits entry must be an object"]
            if not all(k in entry for k in ("path", "old_string", "new_string")):
                return False, ["each edits entry must have path, old_string, new_string"]
            if entry["old_string"] == entry["new_string"]:
                return False, ["edits entry old_string must differ from new_string"]
        for entry in new_files:
            if not isinstance(entry, dict):
                return False, ["each new_files entry must be an object"]
            if "path" not in entry or "content" not in entry:
                return False, ["each new_files entry must have path and content"]
        if not parsed.get("rationale", "").strip():
            return False, ["rationale must be non-empty"]
        return True, []

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        return None


def _normalize_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _resolve_in_workspace(workspace_root: Path, rel_path: str) -> Path | None:
    """Resolve rel_path under workspace_root, returning None on path escape."""
    target = (workspace_root / rel_path).resolve()
    try:
        target.relative_to(workspace_root)
    except ValueError:
        return None
    return target


def apply_changes(
    result: SkillResult,
    *,
    workspace_root: Path,
    allowed_files: list[str],
) -> dict[str, Any]:
    """Apply edit-list + new_files + deletions to the workspace.

    Enforces that every touched path is in allowed_files. For edits, verifies
    old_string is present and unique before replacing. Reports per-operation
    outcomes in a structured summary:

        {
            written: [paths],          # paths with successful writes (edits+new_files)
            deleted: [paths],
            rejected: [{path, reason}],# scope/uniqueness/path-escape failures
            edits_applied: int,
            new_files_created: int,
        }
    """
    written: list[str] = []
    deleted: list[str] = []
    rejected: list[dict[str, str]] = []
    edits_applied = 0
    new_files_created = 0
    allowed_set = {_normalize_rel(p) for p in allowed_files}

    workspace_root = Path(workspace_root).resolve()

    # Apply edits
    for entry in result.output.get("edits") or []:
        path_str = _normalize_rel(str(entry.get("path") or ""))
        if not path_str:
            continue
        if path_str not in allowed_set:
            rejected.append({"path": path_str, "reason": "edit_out_of_scope"})
            continue
        target = _resolve_in_workspace(workspace_root, path_str)
        if target is None:
            rejected.append({"path": path_str, "reason": "path_escape"})
            continue
        if not target.exists() or not target.is_file():
            rejected.append({"path": path_str, "reason": "edit_target_missing"})
            continue
        old_string = str(entry.get("old_string") or "")
        new_string = str(entry.get("new_string") or "")
        if not old_string:
            rejected.append({"path": path_str, "reason": "edit_empty_old_string"})
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            rejected.append({"path": path_str, "reason": f"read_failed:{exc}"})
            continue
        match_count = content.count(old_string)
        if match_count == 0:
            rejected.append({"path": path_str, "reason": "old_string_not_found"})
            continue
        if match_count > 1:
            rejected.append(
                {"path": path_str, "reason": f"old_string_not_unique:{match_count}"}
            )
            continue
        updated = content.replace(old_string, new_string, 1)
        target.write_text(updated, encoding="utf-8")
        if path_str not in written:
            written.append(path_str)
        edits_applied += 1

    # Create new files
    for entry in result.output.get("new_files") or []:
        path_str = _normalize_rel(str(entry.get("path") or ""))
        if not path_str:
            continue
        if path_str not in allowed_set:
            rejected.append({"path": path_str, "reason": "new_file_out_of_scope"})
            continue
        target = _resolve_in_workspace(workspace_root, path_str)
        if target is None:
            rejected.append({"path": path_str, "reason": "path_escape"})
            continue
        if target.exists():
            rejected.append({"path": path_str, "reason": "new_file_already_exists"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(entry.get("content") or ""), encoding="utf-8")
        if path_str not in written:
            written.append(path_str)
        new_files_created += 1

    # Deletions
    for raw in result.output.get("deleted_files") or []:
        path_str = _normalize_rel(str(raw))
        if not path_str:
            continue
        if path_str not in allowed_set:
            rejected.append({"path": path_str, "reason": "delete_out_of_scope"})
            continue
        target = _resolve_in_workspace(workspace_root, path_str)
        if target is None:
            rejected.append({"path": path_str, "reason": "path_escape"})
            continue
        if target.exists():
            target.unlink()
            deleted.append(path_str)

    return {
        "written": written,
        "deleted": deleted,
        "rejected": rejected,
        "edits_applied": edits_applied,
        "new_files_created": new_files_created,
    }
