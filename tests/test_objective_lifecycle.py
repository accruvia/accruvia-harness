"""Tests for ObjectivePhase state machine and ObjectiveLifecycleRunner contract."""
from __future__ import annotations

import unittest

from accruvia_harness.domain import (
    ObjectivePhase,
    VALID_PHASE_TRANSITIONS,
    PHASE_TO_STATUS,
    advance_objective_phase,
)


class ObjectivePhaseTransitionTests(unittest.TestCase):
    def test_happy_path_sequence(self) -> None:
        phase = ObjectivePhase.CREATED
        for target in [
            ObjectivePhase.INTERROGATING,
            ObjectivePhase.MERMAID_REVIEW,
            ObjectivePhase.TRIO_PLANNING,
            ObjectivePhase.EXECUTING,
            ObjectivePhase.REVIEWING,
            ObjectivePhase.PROMOTED,
        ]:
            phase = advance_objective_phase(phase, target)
            self.assertEqual(target, phase)

    def test_idempotent_same_phase(self) -> None:
        phase = advance_objective_phase(ObjectivePhase.EXECUTING, ObjectivePhase.EXECUTING)
        self.assertEqual(ObjectivePhase.EXECUTING, phase)

    def test_skip_phase_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            advance_objective_phase(ObjectivePhase.CREATED, ObjectivePhase.TRIO_PLANNING)
        self.assertIn("Illegal phase transition", str(ctx.exception))
        self.assertIn("created", str(ctx.exception))
        self.assertIn("trio_planning", str(ctx.exception))

    def test_backward_transition_rejected(self) -> None:
        with self.assertRaises(ValueError):
            advance_objective_phase(ObjectivePhase.EXECUTING, ObjectivePhase.INTERROGATING)

    def test_any_phase_can_fail(self) -> None:
        for phase in ObjectivePhase:
            if phase in (ObjectivePhase.PROMOTED, ObjectivePhase.FAILED):
                continue
            result = advance_objective_phase(phase, ObjectivePhase.FAILED)
            self.assertEqual(ObjectivePhase.FAILED, result)

    def test_promoted_is_terminal(self) -> None:
        self.assertEqual(frozenset(), VALID_PHASE_TRANSITIONS[ObjectivePhase.PROMOTED])
        with self.assertRaises(ValueError):
            advance_objective_phase(ObjectivePhase.PROMOTED, ObjectivePhase.FAILED)

    def test_failed_is_terminal(self) -> None:
        self.assertEqual(frozenset(), VALID_PHASE_TRANSITIONS[ObjectivePhase.FAILED])
        with self.assertRaises(ValueError):
            advance_objective_phase(ObjectivePhase.FAILED, ObjectivePhase.CREATED)

    def test_every_phase_has_status_mapping(self) -> None:
        for phase in ObjectivePhase:
            self.assertIn(phase, PHASE_TO_STATUS, f"Missing PHASE_TO_STATUS for {phase}")

    def test_each_non_terminal_phase_has_exactly_two_transitions(self) -> None:
        for phase in ObjectivePhase:
            if phase in (ObjectivePhase.PROMOTED, ObjectivePhase.FAILED):
                continue
            allowed = VALID_PHASE_TRANSITIONS[phase]
            self.assertEqual(
                2, len(allowed),
                f"Phase {phase.value} should allow exactly next + FAILED, got {sorted(p.value for p in allowed)}",
            )


class ObjectiveLifecycleRunnerTests(unittest.TestCase):
    def test_runner_enforces_phase_order(self) -> None:
        from accruvia_harness.workflows.objective_lifecycle import ObjectiveLifecycleRunner

        runner = ObjectiveLifecycleRunner(config="{}", objective_id="obj-1")
        self.assertEqual(ObjectivePhase.CREATED, runner.phase)

        with self.assertRaises(ValueError):
            runner.approve_mermaid()

        with self.assertRaises(ValueError):
            runner.run_trio_and_create_tasks()

        with self.assertRaises(ValueError):
            runner.complete_tasks()

        with self.assertRaises(ValueError):
            runner.run_review_and_promote()

    def test_runner_phase_after_interrogation(self) -> None:
        from accruvia_harness.workflows.objective_lifecycle import ObjectiveLifecycleRunner
        from unittest.mock import patch

        runner = ObjectiveLifecycleRunner(config="{}", objective_id="obj-1")
        with patch(
            "accruvia_harness.workflows.objective_lifecycle.sync_interrogation",
            return_value={"success": False, "detail": "no LLM"},
        ):
            result = runner.run_through_mermaid()
        self.assertFalse(result["success"])
        self.assertEqual(ObjectivePhase.FAILED, runner.phase)

    def test_runner_phase_after_successful_interrogation(self) -> None:
        from accruvia_harness.workflows.objective_lifecycle import ObjectiveLifecycleRunner
        from unittest.mock import patch

        runner = ObjectiveLifecycleRunner(config="{}", objective_id="obj-1")
        with patch(
            "accruvia_harness.workflows.objective_lifecycle.sync_interrogation",
            return_value={"success": True, "detail": "ok"},
        ):
            result = runner.run_through_mermaid()
        self.assertTrue(result["success"])
        self.assertEqual(ObjectivePhase.MERMAID_REVIEW, runner.phase)
        runner.approve_mermaid()


if __name__ == "__main__":
    unittest.main()
