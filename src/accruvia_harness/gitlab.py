from __future__ import annotations

import json
import logging
import subprocess
from typing import Callable
from urllib.parse import quote

from .issues import ExternalIssue

logger = logging.getLogger(__name__)


def _default_runner(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("GitLab CLI (glab) not found. Is it installed and on PATH?")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"GitLab CLI command timed out: {' '.join(args[:3])}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"GitLab CLI failed (exit {exc.returncode}): {exc.stderr[:500]}") from exc
    return completed.stdout


def _issue_from_payload(payload: dict[str, object]) -> ExternalIssue:
    milestone = None
    if isinstance(payload.get("milestone"), dict):
        title = payload["milestone"].get("title")
        if title:
            milestone = str(title)
    assignees = [
        str(item.get("username") or item.get("name"))
        for item in payload.get("assignees", [])
        if isinstance(item, dict) and (item.get("username") or item.get("name"))
    ]
    labels = [str(item) for item in payload.get("labels", [])]
    return ExternalIssue(
        issue_id=str(payload["iid"]),
        title=str(payload["title"]),
        body=str(payload.get("description") or ""),
        state=str(payload["state"]),
        url=str(payload.get("web_url") or ""),
        labels=labels,
        milestone=milestone,
        assignees=assignees,
    )


class GitLabCLI:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self.runner = runner or _default_runner

    def fetch_issue(self, repo: str, issue_iid: str) -> ExternalIssue:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(["glab", "api", f"projects/{encoded_repo}/issues/{issue_iid}"])
        return _issue_from_payload(json.loads(raw))

    def list_open_issues(self, repo: str, limit: int) -> list[ExternalIssue]:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(
            ["glab", "api", f"projects/{encoded_repo}/issues?state=opened&per_page={limit}"]
        )
        payload = json.loads(raw)
        return [
            _issue_from_payload(item)
            for item in payload
        ]

    def add_comment(self, repo: str, issue_iid: str, message: str) -> None:
        self.runner(
            ["glab", "issue", "note", issue_iid, "--repo", repo, "--message", message]
        )

    def close_issue(self, repo: str, issue_iid: str) -> None:
        self.runner(["glab", "issue", "close", issue_iid, "--repo", repo])

    def reopen_issue(self, repo: str, issue_iid: str) -> None:
        self.runner(["glab", "issue", "reopen", issue_iid, "--repo", repo])
