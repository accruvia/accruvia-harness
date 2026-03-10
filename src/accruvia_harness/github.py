from __future__ import annotations

import json
import subprocess
from typing import Callable

from .issues import ExternalIssue

def _default_runner(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout


class GitHubCLI:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self.runner = runner or _default_runner

    def fetch_issue(self, repo: str, issue_number: str) -> ExternalIssue:
        raw = self.runner(["gh", "api", f"repos/{repo}/issues/{issue_number}"])
        payload = json.loads(raw)
        return ExternalIssue(
            issue_id=str(payload["number"]),
            title=payload["title"],
            body=payload.get("body") or "",
            state=payload["state"],
            url=payload.get("html_url") or "",
        )

    def list_open_issues(self, repo: str, limit: int) -> list[ExternalIssue]:
        raw = self.runner(["gh", "api", f"repos/{repo}/issues?state=open&per_page={limit}"])
        payload = json.loads(raw)
        return [
            ExternalIssue(
                issue_id=str(item["number"]),
                title=item["title"],
                body=item.get("body") or "",
                state=item["state"],
                url=item.get("html_url") or "",
            )
            for item in payload
            if "pull_request" not in item
        ]

    def add_comment(self, repo: str, issue_number: str, message: str) -> None:
        self.runner(["gh", "issue", "comment", issue_number, "--repo", repo, "--body", message])

    def close_issue(self, repo: str, issue_number: str) -> None:
        self.runner(["gh", "issue", "close", issue_number, "--repo", repo])

    def reopen_issue(self, repo: str, issue_number: str) -> None:
        self.runner(["gh", "issue", "reopen", issue_number, "--repo", repo])
