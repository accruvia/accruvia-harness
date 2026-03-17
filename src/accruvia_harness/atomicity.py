from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


CONTROL_PLANE_FILES = {
    "src/accruvia_harness/agent_worker.py",
    "src/accruvia_harness/workers.py",
    "src/accruvia_harness/policy.py",
    "src/accruvia_harness/config.py",
}
VALIDATION_POLICY_FILES = {
    "src/accruvia_harness/agent_worker.py",
    "src/accruvia_harness/services/task_service.py",
    "src/accruvia_harness/services/cognition_service.py",
    "src/accruvia_harness/workers.py",
    "src/accruvia_harness/config.py",
}


@dataclass(frozen=True, slots=True)
class AtomicityGateResult:
    telemetry: dict[str, object]
    score: int
    flags: list[str]
    action: str
    rationale: str
    effective_validation_mode: str


def _git_stdout(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=False,
        capture_output=True,
        text=True,
    ).stdout


def changed_files(workspace: Path) -> list[str]:
    changed = [line.strip() for line in _git_stdout(workspace, "diff", "--name-only").splitlines() if line.strip()]
    untracked = [
        line.strip()
        for line in _git_stdout(workspace, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip()
    ]
    return sorted(dict.fromkeys(changed + untracked))


def _diff_size_features(workspace: Path) -> dict[str, int]:
    lines_added = 0
    lines_deleted = 0
    changed_hunk_count = 0
    for line in _git_stdout(workspace, "diff", "--numstat").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added, deleted = parts[0], parts[1]
            if added.isdigit():
                lines_added += int(added)
            if deleted.isdigit():
                lines_deleted += int(deleted)
    for line in _git_stdout(workspace, "diff", "--unified=0").splitlines():
        if line.startswith("@@"):
            changed_hunk_count += 1
    return {
        "lines_added": lines_added,
        "lines_deleted": lines_deleted,
        "lines_changed_total": lines_added + lines_deleted,
        "changed_hunk_count": changed_hunk_count,
    }


def _surface_classes(paths: list[str]) -> set[str]:
    classes: set[str] = set()
    for path in paths:
        if path.startswith("src/accruvia_harness/commands/"):
            classes.add("cli_surface")
            classes.add("control_plane")
        elif path.startswith("src/accruvia_harness/services/"):
            classes.add("control_plane")
        elif path.startswith("src/accruvia_harness/persistence/"):
            classes.add("persistence_layer")
            # Only flag as control_plane if touching task/run orchestration,
            # not data-mapping utilities like common.py.
            if not path.endswith("/common.py"):
                classes.add("control_plane")
        elif path.startswith("src/accruvia_harness/cognition/"):
            classes.add("control_plane")
            classes.add("cognition")
        elif path.startswith("src/accruvia_harness/observer/"):
            classes.add("observer_surface")
        elif path.startswith("tests/"):
            classes.add("test_only")
        elif path.startswith("specs/") or path.endswith(".md"):
            classes.add("docs")
        if path in CONTROL_PLANE_FILES:
            classes.add("control_plane")
        if path in VALIDATION_POLICY_FILES:
            classes.add("validation_policy")
        if path.startswith(".github/"):
            classes.add("ci")
    return classes


def _subsystem_count(paths: list[str]) -> int:
    subsystems: set[str] = set()
    for path in paths:
        if path.startswith("src/accruvia_harness/"):
            parts = path.split("/")
            subsystems.add(parts[2] if len(parts) > 2 else parts[-1])
        else:
            subsystems.add(path.split("/")[0])
    return len(subsystems)


def _selected_validation_targets(validation_mode: str) -> list[str]:
    if validation_mode == "lightweight_repair":
        return ["tests/test_workers.py"]
    if validation_mode == "lightweight_operator":
        return ["tests/test_phase1.py"]
    return ["tests/test_engine.py", "tests/test_store.py", "tests/test_validation.py", "tests/test_phase1.py"]


def _objective_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", text.lower()) if len(token) >= 3}


def atomicity_gate(
    *,
    workspace: Path,
    title: str,
    objective: str,
    strategy: str,
    validation_mode: str,
    attempt: int,
    prior_timeout_count: int = 0,
) -> AtomicityGateResult:
    paths = changed_files(workspace)
    diff_features = _diff_size_features(workspace)
    surface_classes = _surface_classes(paths)
    selected_targets = _selected_validation_targets(validation_mode)
    changed_test_files = [path for path in paths if path.startswith("tests/")]
    touched_files_without_validation_target_count = sum(1 for path in changed_test_files if path not in selected_targets)
    objective_tokens = _objective_tokens(f"{title}\n{objective}")
    objective_keyword_path_overlap_count = sum(
        1 for token in objective_tokens for path in paths if token in path.lower()
    )
    project_is_self_hosting = (workspace / "src" / "accruvia_harness").exists()
    touches_control_plane = bool("control_plane" in surface_classes)
    touches_validation_policy = bool("validation_policy" in surface_classes)
    self_referential_change_detected = validation_mode in {
        "lightweight_operator",
        "lightweight_repair",
        "default_focused",
    } and any(path in VALIDATION_POLICY_FILES for path in paths)
    operator_task = validation_mode == "lightweight_operator" or strategy.startswith("operator_")
    operator_task_touches_non_operator_surface = operator_task and any(
        path.startswith("src/accruvia_harness/services/") or path.startswith("src/accruvia_harness/persistence/")
        for path in paths
    )
    intent_surface_mismatch_detected = operator_task and not any(
        path.startswith("src/accruvia_harness/commands/")
        or path.startswith("src/accruvia_harness/observer/")
        or path.startswith("tests/test_phase1.py")
        or path.startswith("tests/test_engine.py")
        for path in paths
    )
    subsystem_count = _subsystem_count(paths)
    telemetry: dict[str, object] = {
        "schema_version": 1,
        "changed_files": paths,
        "changed_file_count": len(paths),
        "subsystem_count": subsystem_count,
        "selected_validation_targets": selected_targets,
        "selected_validation_target_count": len(selected_targets),
        "touched_test_file_count": len(changed_test_files),
        "touched_files_without_validation_target_count": touched_files_without_validation_target_count,
        "project_is_self_hosting": project_is_self_hosting,
        "touches_control_plane": touches_control_plane,
        "touches_validation_policy": touches_validation_policy,
        "self_referential_change_detected": self_referential_change_detected,
        "operator_task_touches_non_operator_surface": operator_task_touches_non_operator_surface,
        "intent_surface_mismatch_detected": intent_surface_mismatch_detected,
        "retry_attempt_number": attempt,
        "prior_timeout_count": prior_timeout_count,
        "objective_keyword_path_overlap_count": objective_keyword_path_overlap_count,
        "surface_classes": sorted(surface_classes),
        **diff_features,
    }
    flags: list[str] = []
    score = 0
    if len(paths) >= 4:
        score += 1
        flags.append("large_diff")
    if subsystem_count >= 3:
        score += 1
        flags.append("wide_surface")
    if project_is_self_hosting and touches_control_plane:
        score += 2
        flags.append("control_plane_touch")
    if touches_validation_policy:
        score += 2
        flags.append("validation_policy_touch")
    if self_referential_change_detected:
        score += 3
        flags.append("self_referential_change")
    if attempt >= 2:
        score += 1
        flags.append("retry_pressure")
    if prior_timeout_count >= 1:
        score += 1
        flags.append("timeout_history")
    if intent_surface_mismatch_detected:
        score += 1
        flags.append("intent_surface_mismatch")
    if touched_files_without_validation_target_count >= 2:
        score += 1
        flags.append("validation_scope_mismatch")
    if operator_task_touches_non_operator_surface:
        score += 1
        flags.append("operator_surface_drift")

    effective_validation_mode = validation_mode
    action = "validate_normal"
    rationale = "Diff shape is compatible with normal validation."
    # Tasks generated from Mermaid decomposition or prior atomicity splits are
    # already atomic by design — bypass further decomposition to prevent loops.
    if strategy in {"atomicity_split", "atomic_from_mermaid"}:
        action = "validate_narrow"
        rationale = "Atomicity-split task bypasses further decomposition to prevent infinite loops."
        telemetry["atomicity_split_bypass"] = True
        return AtomicityGateResult(
            telemetry=telemetry,
            score=score,
            flags=flags,
            action=action,
            rationale=rationale,
            effective_validation_mode=effective_validation_mode,
        )
    if self_referential_change_detected:
        action = "block_self_referential"
        rationale = "Attempt modifies validation/task-selection machinery that evaluates tasks of its own class."
    elif score >= 6:
        action = "block_self_referential"
        rationale = "Atomicity risk is extremely high for this self-hosting control-plane attempt."
    elif score >= 4:
        action = "decompose_first"
        rationale = "Attempt spans too much surface area for efficient bounded validation."
    elif score >= 2:
        action = "validate_narrow"
        rationale = "Attempt risk is elevated; narrow validation should match the touched surface better."
        if validation_mode == "default_focused" and (
            operator_task or "cli_surface" in surface_classes or "observer_surface" in surface_classes
        ):
            effective_validation_mode = "lightweight_operator"
        elif validation_mode == "default_focused" and (
            touches_validation_policy or "control_plane" in surface_classes
        ):
            effective_validation_mode = "lightweight_repair"
    return AtomicityGateResult(
        telemetry=telemetry,
        score=score,
        flags=flags,
        action=action,
        rationale=rationale,
        effective_validation_mode=effective_validation_mode,
    )


def write_atomicity_telemetry(path: Path, result: AtomicityGateResult) -> None:
    payload = {
        **result.telemetry,
        "atomicity_risk_score": result.score,
        "atomicity_flags": result.flags,
        "gate_action": result.action,
        "gate_rationale": result.rationale,
        "effective_validation_mode": result.effective_validation_mode,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
