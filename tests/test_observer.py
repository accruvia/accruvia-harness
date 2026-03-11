from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

from accruvia_harness.observer.agent import ObserverAgent, SYSTEM_PROMPT
from accruvia_harness.observer.evidence_cache import EvidenceCache
from accruvia_harness.observer.query_client import HarnessQueryClient, QueryResult
from accruvia_harness.observer.webhook import WebhookReceiver
from accruvia_harness.observer_hook import notify_observer, NOTIFY_EVENT_TYPES


class EvidenceCacheTests(unittest.TestCase):
    def test_record_and_latest(self) -> None:
        cache = EvidenceCache()
        cache.record("context_packet", {"tasks": 5})
        latest = cache.latest("context_packet")
        self.assertIsNotNone(latest)
        self.assertEqual({"tasks": 5}, latest.data)

    def test_latest_returns_none_when_empty(self) -> None:
        cache = EvidenceCache()
        self.assertIsNone(cache.latest("context_packet"))

    def test_history_returns_limited_entries(self) -> None:
        cache = EvidenceCache()
        for i in range(10):
            cache.record("ctx", {"i": i})
        history = cache.history("ctx", limit=3)
        self.assertEqual(3, len(history))
        self.assertEqual(7, history[0].data["i"])

    def test_max_snapshots_enforced(self) -> None:
        cache = EvidenceCache(max_snapshots=5)
        for i in range(10):
            cache.record("ctx", {"i": i})
        self.assertEqual(5, len(cache._snapshots))
        self.assertEqual(5, cache._snapshots[0].data["i"])

    def test_diff_latest_detects_changes(self) -> None:
        cache = EvidenceCache()
        cache.record("ctx", {"tasks": 3, "status": "ok"})
        cache.record("ctx", {"tasks": 5, "status": "ok"})
        diff = cache.diff_latest("ctx")
        self.assertIn("tasks", diff)
        self.assertEqual(3, diff["tasks"]["old"])
        self.assertEqual(5, diff["tasks"]["new"])
        self.assertNotIn("status", diff)

    def test_diff_returns_none_with_single_snapshot(self) -> None:
        cache = EvidenceCache()
        cache.record("ctx", {"tasks": 3})
        self.assertIsNone(cache.diff_latest("ctx"))

    def test_summary_text(self) -> None:
        cache = EvidenceCache()
        cache.record("context_packet", {"x": 1})
        summary = cache.summary_text()
        self.assertIn("context_packet", summary)
        self.assertIn("ago", summary)


class QueryClientTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_context_packet_parses_json(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"metrics": {"total_runs": 5}}),
            stderr="",
        )
        client = HarnessQueryClient(cli_command="fake-harness")
        result = client.context_packet()
        self.assertTrue(result.ok)
        self.assertEqual(5, result.data["metrics"]["total_runs"])

    @patch("subprocess.run")
    def test_error_on_nonzero_exit(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="database locked",
        )
        client = HarnessQueryClient(cli_command="fake-harness")
        result = client.context_packet()
        self.assertFalse(result.ok)
        self.assertIn("database locked", result.error)

    @patch("subprocess.run")
    def test_db_path_passed_to_cli(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        client = HarnessQueryClient(cli_command="harness", db_path="/tmp/test.db")
        client.status()
        call_args = mock_run.call_args[0][0]
        self.assertIn("--db", call_args)
        self.assertIn("/tmp/test.db", call_args)

    @patch("subprocess.run")
    def test_project_id_scoping(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        client = HarnessQueryClient(cli_command="harness")
        client.context_packet(project_id="proj_123")
        call_args = mock_run.call_args[0][0]
        self.assertIn("--project-id", call_args)
        self.assertIn("proj_123", call_args)


class AgentTests(unittest.TestCase):
    def _make_agent(self, llm_response: str = "Test answer") -> ObserverAgent:
        client = MagicMock(spec=HarnessQueryClient)
        client.context_packet.return_value = QueryResult(
            command="context-packet",
            data={"metrics": {"total_runs": 3}, "focus_tasks": []},
        )
        client.ops_report.return_value = QueryResult(
            command="ops-report",
            data={"pending_affirmations": []},
        )
        client.summary.return_value = QueryResult(
            command="summary",
            data={"project_count": 1},
        )
        client.events.return_value = QueryResult(
            command="events",
            data={"events": []},
        )
        cache = EvidenceCache()
        agent = ObserverAgent(
            query_client=client,
            evidence_cache=cache,
            llm_command="echo 'Test answer'",
        )
        return agent

    @patch("subprocess.run")
    def test_ask_returns_llm_answer(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="The system has 3 runs.", stderr="")
        agent = self._make_agent()
        response = agent.ask("How many runs?")
        self.assertEqual("The system has 3 runs.", response.answer)
        self.assertIn("context-packet", response.evidence_sources)

    @patch("subprocess.run")
    def test_ask_fetches_ops_for_backlog_questions(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="No backlog.", stderr="")
        agent = self._make_agent()
        agent.ask("what's the backlog?")
        agent.query_client.ops_report.assert_called()

    @patch("subprocess.run")
    def test_ask_fetches_summary_for_overview_questions(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Overview.", stderr="")
        agent = self._make_agent()
        agent.ask("give me an overview")
        agent.query_client.summary.assert_called()

    @patch("subprocess.run")
    def test_ask_fetches_task_report_when_id_mentioned(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Task details.", stderr="")
        agent = self._make_agent()
        agent.query_client.task_report.return_value = QueryResult(
            command="task-report", data={"task": {}}
        )
        agent.ask("what happened to task_abc123?")
        agent.query_client.task_report.assert_called_with("task_abc123")

    @patch("subprocess.run")
    def test_conversation_history_maintained(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Answer.", stderr="")
        agent = self._make_agent()
        agent.ask("first question")
        agent.ask("second question")
        self.assertEqual(4, len(agent._conversation))

    @patch("subprocess.run")
    def test_llm_failure_returns_error(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="LLM broke")
        agent = self._make_agent()
        response = agent.ask("test")
        self.assertIsNotNone(response.error)

    def test_process_event_returns_notification_for_notable_events(self) -> None:
        agent = self._make_agent()
        msg = agent.process_event({"event_type": "task_failed", "entity_id": "task_123", "payload": {}})
        self.assertIsNotNone(msg)
        self.assertIn("task_failed", msg)

    def test_process_event_ignores_routine_events(self) -> None:
        agent = self._make_agent()
        msg = agent.process_event({"event_type": "run_created", "entity_id": "run_123", "payload": {}})
        self.assertIsNone(msg)

    def test_extract_task_id(self) -> None:
        self.assertEqual("task_abc123", ObserverAgent._extract_task_id("what happened to task_abc123?"))
        self.assertIsNone(ObserverAgent._extract_task_id("how are things?"))


class WebhookReceiverTests(unittest.TestCase):
    def test_webhook_receives_events(self) -> None:
        received = []
        receiver = WebhookReceiver(port=0)  # OS picks a free port

        # Use a real port
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        receiver = WebhookReceiver(port=port)
        receiver.start(lambda event: received.append(event))
        time.sleep(0.2)

        try:
            event = {"event_type": "task_failed", "entity_id": "task_123"}
            data = json.dumps(event).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            time.sleep(0.3)
            self.assertEqual(1, len(received))
            self.assertEqual("task_failed", received[0]["event_type"])
        finally:
            receiver.stop()


class ObserverHookTests(unittest.TestCase):
    def test_notify_filters_by_event_type(self) -> None:
        with patch("accruvia_harness.observer_hook._post_event") as mock_post:
            notify_observer("http://localhost:8900", "run_created", "run", "run_123", {})
            mock_post.assert_not_called()

            notify_observer("http://localhost:8900", "task_failed", "task", "task_123", {})
            mock_post.assert_called_once()

    def test_notable_event_types_are_defined(self) -> None:
        self.assertIn("task_failed", NOTIFY_EVENT_TYPES)
        self.assertIn("task_completed", NOTIFY_EVENT_TYPES)
        self.assertIn("branch_winner_selected", NOTIFY_EVENT_TYPES)


class ObserverConfigTests(unittest.TestCase):
    def test_config_includes_observer_webhook_url(self) -> None:
        from accruvia_harness.config import HarnessConfig
        import os
        env = {**os.environ, "ACCRUVIA_OBSERVER_WEBHOOK_URL": "http://localhost:8900/events"}
        with patch.dict(os.environ, env):
            config = HarnessConfig.from_env()
            self.assertEqual("http://localhost:8900/events", config.observer_webhook_url)

    def test_config_defaults_to_none(self) -> None:
        from accruvia_harness.config import HarnessConfig
        import os
        env = {k: v for k, v in os.environ.items() if k != "ACCRUVIA_OBSERVER_WEBHOOK_URL"}
        with patch.dict(os.environ, env, clear=True):
            config = HarnessConfig.from_env()
            self.assertIsNone(config.observer_webhook_url)
