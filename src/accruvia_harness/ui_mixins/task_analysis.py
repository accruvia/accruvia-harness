"""HarnessUIDataService task analysis methods."""
from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..domain import ContextRecord, Run, RunStatus, Task, TaskStatus, serialize_dataclass
from ._shared import _TASK_REPLY_STALE_SECONDS

from ._shared import RunOutputSection

class TaskAnalysisMixin:

    def task_failure_insight(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        project = self.store.get_project(task.project_id)
        objective = self.store.get_objective(task.objective_id) if task.objective_id else None
        runs = self.store.list_runs(task.id)
        run = runs[-1] if runs else None
        evaluation = None
        sections_raw: list[RunOutputSection] = []
        summarized_run: dict[str, object] = {}
        if run is not None:
            evaluations = self.store.list_evaluations(run.id)
            evaluation = evaluations[-1] if evaluations else None
            sections_raw = self._run_output_sections(run.id)
            summarized_run = self._summarize_run_output(run, sections_raw)
        diagnostics = evaluation.details.get("diagnostics") if evaluation is not None and isinstance(evaluation.details, dict) else {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        failure_message = str(
            diagnostics.get("failure_message")
            or diagnostics.get("error")
            or diagnostics.get("blocked_reason")
            or ""
        ).strip()
        root_cause_hint = str((evaluation.details if evaluation is not None else {}).get("root_cause_hint") or "").strip() if evaluation is not None else ""
        relevant_section_previews = self._task_failure_section_previews(sections_raw)
        normalized_failure = self._normalize_task_failure(
            failure_message=failure_message,
            root_cause_hint=root_cause_hint,
            section_previews=relevant_section_previews,
        )
        return {
            "project": serialize_dataclass(project) if project is not None else None,
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "task": serialize_dataclass(task),
            "run": serialize_dataclass(run) if run is not None else None,
            "analysis_summary": str(evaluation.summary or "") if evaluation is not None else "",
            "failure_message": failure_message,
            "root_cause_hint": root_cause_hint,
            "failure_category": str(diagnostics.get("failure_category") or "").strip(),
            "run_summary": summarized_run,
            "available_sections": [section.label for section in sections_raw],
            "relevant_section_previews": relevant_section_previews,
            "backend_failure_kind": normalized_failure["kind"],
            "backend_failure_explanation": normalized_failure["explanation"],
            "suggested_evidence": normalized_failure["suggested_evidence"],
        }


    def run_cli_output(self, run_id: str) -> dict[str, object]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        sections_raw = self._run_output_sections(run_id)
        sections = [
            {
                "label": section.label,
                "path": section.path,
                "content": section.content,
            }
            for section in sections_raw
        ]
        return {
            "run": serialize_dataclass(run),
            "summary": self._summarize_run_output(run, sections_raw),
            "sections": sections,
        }


    def run_cli_command(self, command: str) -> dict[str, object]:
        cleaned = command.strip()
        if not cleaned:
            raise ValueError("CLI command must not be empty")
        command_parts = shlex.split(cleaned)
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        src_path = str(repo_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_path
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "accruvia_harness",
                "--db",
                str(self.ctx.config.db_path),
                "--workspace",
                str(self.ctx.config.workspace_root),
                *command_parts,
            ],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout and stderr:
            output = f"{stdout}\n\n[stderr]\n{stderr}"
        else:
            output = stdout or stderr or "(no output)"
        return {
            "command": cleaned,
            "exit_code": completed.returncode,
            "output": output,
        }


    def task_conversation(self, task_id: str) -> dict[str, object]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise ValueError(f"Unknown project for task: {task_id}")
        objective = self.store.get_objective(task.objective_id) if task.objective_id else None
        task_records = self.store.list_context_records(project_id=project.id, objective_id=task.objective_id, task_id=task.id)
        comment_records = [record for record in task_records if record.record_type == "operator_comment"]
        reply_records = [
            record
            for record in task_records
            if record.record_type in {"harness_reply_pending", "harness_reply", "harness_reply_failed"}
        ]
        replies_by_comment: dict[str, list[ContextRecord]] = {}
        for record in reply_records:
            reply_to = str(record.metadata.get("reply_to") or "")
            if not reply_to:
                continue
            replies_by_comment.setdefault(reply_to, []).append(record)
        turns: list[dict[str, object]] = []
        rank = {"harness_reply_pending": 0, "harness_reply_failed": 1, "harness_reply": 2}
        now = _dt.datetime.now(_dt.timezone.utc)
        for comment in comment_records:
            turns.append(
                {
                    "id": comment.id,
                    "role": "operator",
                    "text": comment.content,
                    "created_at": comment.created_at.isoformat(),
                    "status": "completed",
                }
            )
            candidates = replies_by_comment.get(comment.id, [])
            if not candidates:
                continue
            selected = sorted(
                candidates,
                key=lambda record: (rank.get(record.record_type, -1), record.created_at.isoformat()),
            )[-1]
            queued_at_raw = selected.metadata.get("queued_at")
            started_at_raw = selected.metadata.get("started_at")
            completed_at_raw = selected.metadata.get("completed_at")
            status = str(selected.metadata.get("status") or "")
            stale = False
            stale_elapsed_ms: int | None = None
            if selected.record_type == "harness_reply_pending":
                anchor_raw = str(queued_at_raw or selected.created_at.isoformat() or "")
                try:
                    anchor_dt = _dt.datetime.fromisoformat(anchor_raw)
                    stale_elapsed_ms = max(0, int((now - anchor_dt).total_seconds() * 1000))
                    stale = stale_elapsed_ms >= (_TASK_REPLY_STALE_SECONDS * 1000)
                except ValueError:
                    anchor_dt = None
                if stale:
                    status = "failed"
            turns.append(
                {
                    "id": selected.id,
                    "role": "harness",
                    "text": (
                        f"{selected.content} Reply appears stalled and should be retried."
                        if stale
                        else selected.content
                    ),
                    "created_at": selected.created_at.isoformat(),
                    "status": status or ("pending" if selected.record_type == "harness_reply_pending" else "failed" if selected.record_type == "harness_reply_failed" else "completed"),
                    "pending": selected.record_type == "harness_reply_pending" and not stale,
                    "failed": selected.record_type == "harness_reply_failed" or stale,
                    "job_id": selected.metadata.get("job_id"),
                    "queued_at": queued_at_raw,
                    "started_at": started_at_raw,
                    "completed_at": completed_at_raw,
                    "elapsed_ms": selected.metadata.get("elapsed_ms") if not stale else stale_elapsed_ms,
                    "queue_wait_ms": selected.metadata.get("queue_wait_ms"),
                    "stale": stale,
                }
            )
        return {
            "task": serialize_dataclass(task),
            "objective": serialize_dataclass(objective) if objective is not None else None,
            "project": serialize_dataclass(project),
            "turns": turns[-20:],
        }


    def _run_output_sections(self, run_id: str) -> list[RunOutputSection]:
        run_dir = self.workspace_root / "runs" / run_id
        candidates: list[tuple[str, Path]] = []
        for artifact in self.store.list_artifacts(run_id):
            candidates.append((artifact.kind, Path(artifact.path)))
        for label, filename in [
            ("plan", "plan.txt"),
            ("report", "report.json"),
            ("compile_output", "compile_output.txt"),
            ("test_output", "test_output.txt"),
            ("worker_stdout", "worker.stdout.txt"),
            ("worker_stderr", "worker.stderr.txt"),
            ("llm_stdout", "llm.stdout.txt"),
            ("llm_stderr", "llm.stderr.txt"),
            ("codex_worker_stdout", "codex_worker.stdout.txt"),
            ("codex_worker_stderr", "codex_worker.stderr.txt"),
            ("atomicity_telemetry", "atomicity_telemetry.json"),
        ]:
            path = run_dir / filename
            if path.exists():
                candidates.append((label, path))
        seen: set[str] = set()
        sections: list[RunOutputSection] = []
        for label, path in candidates:
            resolved = str(path.resolve())
            if resolved in seen or not path.exists() or not path.is_file():
                continue
            seen.add(resolved)
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if not content:
                continue
            sections.append(
                RunOutputSection(
                    label=label.replace("_", " "),
                    path=resolved,
                    content=content,
                )
            )
        return sections


    def _summarize_run_output(self, run: Run, sections: list[RunOutputSection]) -> dict[str, object]:
        headline = f"Attempt {run.attempt} is {run.status.value}."
        highlights: list[str] = []
        section_map = {section.label: section.content for section in sections}

        if run.summary.strip():
            highlights.append(run.summary.strip())

        report_content = section_map.get("report")
        if report_content:
            try:
                report_payload = json.loads(report_content)
                worker_outcome = str(report_payload.get("worker_outcome") or "").strip()
                failure_category = str(report_payload.get("failure_category") or "").strip()
                if worker_outcome:
                    highlights.append(f"Worker outcome: {worker_outcome}.")
                if failure_category:
                    highlights.append(f"Failure category: {failure_category}.")
            except json.JSONDecodeError:
                highlights.append("A structured report exists, but it could not be parsed cleanly.")

        for label in ("test output", "compile output", "worker stderr", "codex worker stderr", "llm stderr"):
            content = section_map.get(label)
            if content:
                highlights.append(f"{label.title()}: {self._truncate_text(content, 160)}")

        status_value = run.status.value
        if status_value in {"failed", "blocked"}:
            interpretation = "The latest implementation attempt did not complete cleanly. Review the evidence before deciding whether to retry or investigate."
            recommended_next = "Ask the harness to summarize the failure or open investigation mode if the process feels wrong."
        elif status_value in {"analyzing", "working"}:
            interpretation = "The latest attempt is still in progress or has not reached a final decision yet."
            recommended_next = "Review the current evidence and decide whether to wait, redirect the harness, or investigate."
        elif status_value in {"completed"}:
            interpretation = "The latest implementation step completed. Review the result to decide whether to continue to the next slice."
            recommended_next = "Ask the harness what changed or continue execution if the result matches your intent."
        else:
            interpretation = "The latest run produced evidence, but the state still needs human review."
            recommended_next = "Review the summary first, then inspect raw evidence only if something looks off."

        return {
            "headline": headline,
            "interpretation": interpretation,
            "recommended_next": recommended_next,
            "highlights": highlights[:4],
        }


    def _task_failure_section_previews(self, sections: list[RunOutputSection]) -> dict[str, str]:
        previews: dict[str, str] = {}
        for label in ("worker stderr", "codex worker stderr", "llm stderr", "report", "plan", "workspace metadata"):
            matching = next((section for section in sections if section.label == label), None)
            if matching is not None:
                previews[label] = self._truncate_text(matching.content, 220)
        return previews


    def _normalize_task_failure(
        self,
        *,
        failure_message: str,
        root_cause_hint: str,
        section_previews: dict[str, str],
    ) -> dict[str, object]:
        combined = "\n".join(
            part for part in [
                failure_message.strip(),
                root_cause_hint.strip(),
                *(section_previews.values()),
            ] if part
        ).lower()
        suggested_evidence = [label for label in ("worker stderr", "codex worker stderr", "llm stderr", "report", "plan", "workspace metadata") if label in section_previews]
        if "hit your limit" in combined or "quota" in combined or "credits exhausted" in combined or "out of credits" in combined:
            return {
                "kind": "quota",
                "explanation": "The failure looks like provider quota or credit exhaustion rather than a code defect.",
                "suggested_evidence": suggested_evidence or ["llm stderr", "worker stderr", "report"],
            }
        if "unauthorized" in combined or "incorrect username or password" in combined or "authentication" in combined or "api key" in combined or "login" in combined:
            return {
                "kind": "auth",
                "explanation": "The failure looks like an authentication or credential problem in the backend toolchain.",
                "suggested_evidence": suggested_evidence or ["worker stderr", "llm stderr", "report"],
            }
        if "all worker backends failed" in combined or "executor/infrastructure" in combined or "executor failed" in combined or "backend unavailable" in combined:
            return {
                "kind": "backend_unavailable",
                "explanation": "The failure looks like backend or executor infrastructure trouble, not a completed product-level judgment.",
                "suggested_evidence": suggested_evidence or ["worker stderr", "llm stderr", "report", "plan"],
            }
        return {
            "kind": "",
            "explanation": "",
            "suggested_evidence": suggested_evidence,
        }


    def _latest_linked_task_and_run(self, *, project_id: str, objective_id: str | None):
        linked_tasks = [
            task
            for task in self.store.list_tasks(project_id)
            if objective_id and task.objective_id == objective_id
        ]
        if not linked_tasks:
            return None, None
        best_pair = None
        for candidate in linked_tasks:
            candidate_runs = self.store.list_runs(candidate.id)
            candidate_latest_run = candidate_runs[-1] if candidate_runs else None
            candidate_sort_key = (
                candidate_latest_run.created_at if candidate_latest_run is not None else candidate.updated_at,
                candidate.id,
            )
            if best_pair is None or candidate_sort_key > best_pair[0]:
                best_pair = (candidate_sort_key, candidate, candidate_latest_run)
        assert best_pair is not None
        return best_pair[1], best_pair[2]

