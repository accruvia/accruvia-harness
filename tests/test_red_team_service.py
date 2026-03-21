from __future__ import annotations

import unittest

from accruvia_harness.services.red_team_service import RedTeamLoopService


class RedTeamLoopServiceTests(unittest.TestCase):
    def test_red_team_loop_retries_until_major_findings_clear(self) -> None:
        service = RedTeamLoopService()
        prompts: list[str] = []
        candidates = iter(
            [
                {"summary": "draft 1", "content": "flowchart TD\nA-->B{Ready?}"},
                {"summary": "draft 2", "content": "flowchart TD\nA-->B{Execution artifacts sufficient for execution?}"},
            ]
        )

        def generate(prompt: str, _attempt: int, _history):
            prompts.append(prompt)
            try:
                candidate = next(candidates)
            except StopIteration:
                return None
            return candidate, {"backend": "fake"}

        def review(candidate: dict[str, str], _attempt: int, _history):
            if "Ready?" in candidate["content"]:
                return {
                    "ready_for_human_review": False,
                    "deterministic_review": {
                        "findings": [
                            {
                                "severity": "major",
                                "summary": "Ambiguous gate label",
                            }
                        ]
                    },
                    "llm_review": {"findings": []},
                }
            return {
                "ready_for_human_review": True,
                "deterministic_review": {"findings": []},
                "llm_review": {"findings": []},
            }

        def retry_prompt(initial_prompt, candidate, major_findings, history, attempt, max_rounds):
            return f"{initial_prompt}\nAttempt {attempt + 1}/{max_rounds}\n{candidate['content']}\n{major_findings}\n{len(history)}"

        result = service.run(
            initial_prompt="base prompt",
            max_rounds=20,
            generate_candidate=generate,
            review_candidate=review,
            build_retry_prompt=retry_prompt,
        )

        assert result is not None
        self.assertEqual("draft 2", result.candidate["summary"])
        self.assertEqual(2, len(result.history))
        self.assertEqual(2, len(prompts))
        self.assertIn("Attempt 2/20", prompts[1])
