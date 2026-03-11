from __future__ import annotations

import json
import logging
import subprocess
from typing import Callable

from .issues import ExternalIssue

logger = logging.getLogger(__name__)


def _default_runner(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("GitHub CLI (gh) not found. Is it installed and on PATH?")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"GitHub CLI command timed out: {' '.join(args[:3])}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"GitHub CLI failed (exit {exc.returncode}): {exc.stderr[:500]}") from exc
    return completed.stdout


def _issue_from_payload(payload: dict[str, object]) -> ExternalIssue:
    labels = [
        str(item.get("name"))
        for item in payload.get("labels", [])
        if isinstance(item, dict) and item.get("name")
    ]
    milestone = None
    if isinstance(payload.get("milestone"), dict):
        title = payload["milestone"].get("title")
        if title:
            milestone = str(title)
    assignees = [
        str(item.get("login"))
        for item in payload.get("assignees", [])
        if isinstance(item, dict) and item.get("login")
    ]
    return ExternalIssue(
        issue_id=str(payload["number"]),
        title=str(payload["title"]),
        body=str(payload.get("body") or ""),
        state=str(payload["state"]),
        url=str(payload.get("html_url") or ""),
        labels=labels,
        milestone=milestone,
        assignees=assignees,
    )


class GitHubCLI:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self.runner = runner or _default_runner

    def fetch_issue(self, repo: str, issue_number: str) -> ExternalIssue:
        raw = self.runner(["gh", "api", f"repos/{repo}/issues/{issue_number}"])
        return _issue_from_payload(json.loads(raw))

    def list_open_issues(self, repo: str, limit: int) -> list[ExternalIssue]:
        raw = self.runner(["gh", "api", f"repos/{repo}/issues?state=open&per_page={limit}"])
        payload = json.loads(raw)
        return [
            _issue_from_payload(item)
            for item in payload
            if "pull_request" not in item
        ]

    def add_comment(self, repo: str, issue_number: str, message: str) -> None:
        self.runner(["gh", "issue", "comment", issue_number, "--repo", repo, "--body", message])

    def close_issue(self, repo: str, issue_number: str) -> None:
        self.runner(["gh", "issue", "close", issue_number, "--repo", repo])

    def reopen_issue(self, repo: str, issue_number: str) -> None:
        self.runner(["gh", "issue", "reopen", issue_number, "--repo", repo])

    def create_pull_request(self, repo: str, title: str, body: str, head: str, base: str) -> str | None:
        raw = self.runner(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
                "--head",
                head,
                "--base",
                base,
            ]
        )
        return raw.strip() or None
