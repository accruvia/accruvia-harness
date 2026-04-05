"""Base Skill abstraction.

Skills invoke an LLM with a narrow prompt and enforce a structured output
schema. Each skill is responsible for:

1. Building its prompt from typed inputs (build_prompt).
2. Parsing the LLM response into structured JSON (parse_response).
3. Validating the parsed output against its schema (validate_output).
4. Materializing the output into durable state (materialize).

The caller (orchestrator) decides what to do with the materialized result.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..domain import Run, Task, new_id
from ..llm import LLMExecutionError, LLMInvocation, LLMRouter


class SkillError(RuntimeError):
    """Raised when a skill invocation fails in a non-recoverable way."""


@dataclass(slots=True)
class SkillInvocation:
    """Everything a skill needs for a single invocation.

    inputs: the typed dict the skill's build_prompt expects.
    task/run: Task and Run context for LLM invocation bookkeeping.
    run_dir: where prompt/response files are written for durability.
    """

    skill_name: str
    inputs: dict[str, Any]
    task: Task
    run: Run
    run_dir: Path
    model: str | None = None
    timeout_seconds_override: int | None = None


@dataclass(slots=True)
class SkillResult:
    """Outcome of invoking a skill."""

    skill_name: str
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    raw_response: str = ""
    errors: list[str] = field(default_factory=list)
    llm_backend: str | None = None
    prompt_path: str | None = None
    response_path: str | None = None
    analysis_path: str | None = None


class Skill(Protocol):
    """A narrow LLM role with schema-enforced output.

    Skills are stateless. State lives in the store (passed at materialization)
    and in the run_dir artifacts.
    """

    name: str
    # Schema spec: {"required": [...], "types": {"field": "str|bool|int|list|dict"}}
    output_schema: dict[str, Any]

    def build_prompt(self, inputs: dict[str, Any]) -> str:
        """Compose the full prompt string for the LLM."""
        ...

    def parse_response(self, response_text: str) -> dict[str, Any]:
        """Parse the LLM's raw response into structured output."""
        ...

    def validate_output(self, parsed: dict[str, Any]) -> tuple[bool, list[str]]:
        """Check that parsed output conforms to this skill's schema."""
        ...

    def materialize(
        self,
        store: Any,
        result: SkillResult,
        inputs: dict[str, Any],
    ) -> None:
        """Write skill output to durable state (DB, artifacts, events).

        Called only when result.success is True. No-op for read-only skills.
        """
        ...


def extract_json_payload(response_text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from an LLM response.

    Tries in order:
    1. Whole response as JSON
    2. First ```json fenced block
    3. First {...} balanced block
    """
    text = response_text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass

    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, ValueError):
                    return None
                break
    return None


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "bool": bool,
    "int": int,
    "float": (int, float),
    "list": list,
    "dict": dict,
    "any": object,
}


def validate_against_schema(
    parsed: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate parsed output against a lightweight schema spec.

    Schema shape:
        {
            "required": ["field_a", "field_b"],
            "types": {"field_a": "str", "field_b": "bool", "items": "list"},
            "allowed_values": {"priority": ["P0", "P1", "P2", "P3"]},
        }
    """
    errors: list[str] = []
    required = schema.get("required") or []
    types = schema.get("types") or {}
    allowed = schema.get("allowed_values") or {}

    for field_name in required:
        if field_name not in parsed:
            errors.append(f"missing required field: {field_name}")

    for field_name, type_name in types.items():
        if field_name not in parsed:
            continue
        expected = _TYPE_MAP.get(type_name)
        if expected is None:
            continue
        value = parsed[field_name]
        if isinstance(expected, tuple):
            if not isinstance(value, expected):
                errors.append(
                    f"field {field_name} expected {type_name}, got {type(value).__name__}"
                )
        else:
            # bool is a subclass of int — handle explicitly
            if type_name == "bool" and not isinstance(value, bool):
                errors.append(
                    f"field {field_name} expected bool, got {type(value).__name__}"
                )
                continue
            if type_name == "int" and isinstance(value, bool):
                errors.append(
                    f"field {field_name} expected int, got bool"
                )
                continue
            if not isinstance(value, expected):
                errors.append(
                    f"field {field_name} expected {type_name}, got {type(value).__name__}"
                )

    for field_name, choices in allowed.items():
        if field_name not in parsed:
            continue
        if parsed[field_name] not in choices:
            errors.append(
                f"field {field_name} must be one of {choices}, got {parsed[field_name]!r}"
            )

    return (len(errors) == 0, errors)


def invoke_skill(
    skill: Skill,
    invocation: SkillInvocation,
    llm_router: LLMRouter,
    telemetry: Any = None,
) -> SkillResult:
    """Invoke a skill against the LLM and return a validated SkillResult.

    This is the core contract that every skill-using service calls. On success
    the SkillResult carries validated structured output. On failure, errors
    are collected and success=False — the caller decides how to handle it.
    """
    invocation.run_dir.mkdir(parents=True, exist_ok=True)
    prompt = skill.build_prompt(invocation.inputs)

    try:
        llm_result, backend = llm_router.execute(
            LLMInvocation(
                task=invocation.task,
                run=invocation.run,
                prompt=prompt,
                run_dir=invocation.run_dir,
                model=invocation.model,
                timeout_seconds_override=invocation.timeout_seconds_override,
            ),
            telemetry=telemetry,
        )
    except LLMExecutionError as exc:
        return SkillResult(
            skill_name=skill.name,
            success=False,
            errors=[f"llm_execution_failed: {exc}"],
        )

    raw_response = llm_result.response_text
    try:
        parsed = skill.parse_response(raw_response)
    except Exception as exc:  # noqa: BLE001 - skill-level failure capture
        return SkillResult(
            skill_name=skill.name,
            success=False,
            raw_response=raw_response,
            errors=[f"parse_failed: {exc}"],
            llm_backend=backend,
            prompt_path=str(llm_result.prompt_path),
            response_path=str(llm_result.response_path),
        )

    ok, errors = skill.validate_output(parsed)
    analysis_path = invocation.run_dir / f"{skill.name}_analysis.json"
    try:
        analysis_path.write_text(
            json.dumps(
                {"output": parsed, "errors": errors, "success": ok},
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError:
        analysis_path = None  # type: ignore[assignment]

    return SkillResult(
        skill_name=skill.name,
        success=ok,
        output=parsed,
        raw_response=raw_response,
        errors=errors,
        llm_backend=backend,
        prompt_path=str(llm_result.prompt_path),
        response_path=str(llm_result.response_path),
        analysis_path=str(analysis_path) if analysis_path is not None else None,
    )


def make_skill_context(task: Task, skill_name: str, workspace_root: Path) -> tuple[Task, Run, Path]:
    """Build a throwaway Task/Run/run_dir for skill invocation.

    Used when a skill needs to run outside a normal Run lifecycle (e.g.
    diagnose on a control-plane event). For in-Run skills, pass the real
    Task and Run instead.
    """
    from ..domain import RunStatus, TaskStatus

    synthetic_task = Task(
        id=new_id(f"skill_{skill_name}_task"),
        project_id=task.project_id,
        title=f"Skill invocation: {skill_name}",
        objective=f"Execute {skill_name} skill",
        strategy="skill",
        status=TaskStatus.COMPLETED,
    )
    synthetic_run = Run(
        id=new_id(f"skill_{skill_name}_run"),
        task_id=synthetic_task.id,
        status=RunStatus.COMPLETED,
        attempt=1,
        summary=f"Skill invocation for {skill_name}",
    )
    run_dir = workspace_root / "skills" / skill_name / synthetic_run.id
    return synthetic_task, synthetic_run, run_dir
