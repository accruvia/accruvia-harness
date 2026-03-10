from __future__ import annotations

import json

from .common import decision_from_row, evaluation_from_row, promotion_from_row, run_from_row, parse_dt
from ..domain import Artifact, Decision, Evaluation, PromotionRecord, Run


class RunRecordsStoreMixin:
    def next_attempt(self, task_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["attempt"]) + 1

    def create_run(self, run: Run) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (id, task_id, status, attempt, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.task_id,
                    run.status.value,
                    run.attempt,
                    run.summary,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                ),
            )

    def update_run(self, run: Run) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ?, summary = ?, updated_at = ? WHERE id = ?",
                (run.status.value, run.summary, run.updated_at.isoformat(), run.id),
            )

    def list_runs(self, task_id: str | None = None) -> list[Run]:
        query = "SELECT id, task_id, status, attempt, summary, created_at, updated_at FROM runs"
        params: tuple[str, ...] = ()
        if task_id:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [run_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> Run | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, task_id, status, attempt, summary, created_at, updated_at FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return run_from_row(row) if row else None

    def create_artifact(self, artifact: Artifact) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO artifacts (id, run_id, kind, path, summary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.kind,
                    artifact.path,
                    artifact.summary,
                    artifact.created_at.isoformat(),
                ),
            )

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, run_id, kind, path, summary, created_at FROM artifacts WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [
            Artifact(
                id=row["id"],
                run_id=row["run_id"],
                kind=row["kind"],
                path=row["path"],
                summary=row["summary"],
                created_at=parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def create_evaluation(self, evaluation: Evaluation) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluations (id, run_id, verdict, confidence, summary, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.run_id,
                    evaluation.verdict,
                    evaluation.confidence,
                    evaluation.summary,
                    json.dumps(evaluation.details, sort_keys=True),
                    evaluation.created_at.isoformat(),
                ),
            )

    def list_evaluations(self, run_id: str) -> list[Evaluation]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, run_id, verdict, confidence, summary, details_json, created_at FROM evaluations WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [evaluation_from_row(row) for row in rows]

    def create_decision(self, decision: Decision) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO decisions (id, run_id, action, rationale, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    decision.id,
                    decision.run_id,
                    decision.action.value,
                    decision.rationale,
                    decision.created_at.isoformat(),
                ),
            )

    def list_decisions(self, run_id: str) -> list[Decision]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, run_id, action, rationale, created_at FROM decisions WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [decision_from_row(row) for row in rows]

    def create_promotion(self, promotion: PromotionRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO promotions (id, task_id, run_id, status, summary, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    promotion.id,
                    promotion.task_id,
                    promotion.run_id,
                    promotion.status.value,
                    promotion.summary,
                    json.dumps(promotion.details, sort_keys=True),
                    promotion.created_at.isoformat(),
                ),
            )

    def update_promotion(self, promotion: PromotionRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE promotions SET status = ?, summary = ?, details_json = ? WHERE id = ?",
                (
                    promotion.status.value,
                    promotion.summary,
                    json.dumps(promotion.details, sort_keys=True),
                    promotion.id,
                ),
            )

    def list_promotions(self, task_id: str | None = None) -> list[PromotionRecord]:
        query = "SELECT id, task_id, run_id, status, summary, details_json, created_at FROM promotions"
        params: tuple[str, ...] = ()
        if task_id:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [promotion_from_row(row) for row in rows]

    def latest_promotion(self, task_id: str) -> PromotionRecord | None:
        rows = self.list_promotions(task_id)
        return rows[-1] if rows else None
