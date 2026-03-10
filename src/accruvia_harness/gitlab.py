from __future__ import annotations

import json
import subprocess
from typing import Callable
from urllib.parse import quote

from .issues import ExternalIssue
def _default_runner(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout


class GitLabCLI:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self.runner = runner or _default_runner

    def fetch_issue(self, repo: str, issue_iid: str) -> ExternalIssue:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(["glab", "api", f"projects/{encoded_repo}/issues/{issue_iid}"])
        payload = json.loads(raw)
        return ExternalIssue(
            issue_id=str(payload["iid"]),
            title=payload["title"],
            body=payload.get("description") or "",
            state=payload["state"],
            url=payload.get("web_url") or "",
        )

    def list_open_issues(self, repo: str, limit: int) -> list[ExternalIssue]:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(
            ["glab", "api", f"projects/{encoded_repo}/issues?state=opened&per_page={limit}"]
        )
        payload = json.loads(raw)
        return [
            ExternalIssue(
                issue_id=str(item["iid"]),
                title=item["title"],
                body=item.get("description") or "",
                state=item["state"],
                url=item.get("web_url") or "",
            )
            for item in payload
        ]

    def add_comment(self, repo: str, issue_iid: str, message: str) -> None:
        self.runner(
            ["glab", "issue", "note", issue_iid, "--repo", repo, "--message", message]
        )

    def close_issue(self, repo: str, issue_iid: str) -> None:
        self.runner(["glab", "issue", "close", issue_iid, "--repo", repo])
