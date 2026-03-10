from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote


@dataclass(slots=True)
class GitLabIssue:
    iid: str
    title: str
    description: str
    state: str
    web_url: str


def _default_runner(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout


class GitLabCLI:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self.runner = runner or _default_runner

    def fetch_issue(self, repo: str, issue_iid: str) -> GitLabIssue:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(["glab", "api", f"projects/{encoded_repo}/issues/{issue_iid}"])
        payload = json.loads(raw)
        return GitLabIssue(
            iid=str(payload["iid"]),
            title=payload["title"],
            description=payload.get("description") or "",
            state=payload["state"],
            web_url=payload.get("web_url") or "",
        )

    def list_open_issues(self, repo: str, limit: int) -> list[GitLabIssue]:
        encoded_repo = quote(repo, safe="")
        raw = self.runner(
            ["glab", "api", f"projects/{encoded_repo}/issues?state=opened&per_page={limit}"]
        )
        payload = json.loads(raw)
        return [
            GitLabIssue(
                iid=str(item["iid"]),
                title=item["title"],
                description=item.get("description") or "",
                state=item["state"],
                web_url=item.get("web_url") or "",
            )
            for item in payload
        ]

    def add_note(self, repo: str, issue_iid: str, message: str) -> None:
        self.runner(
            ["glab", "issue", "note", issue_iid, "--repo", repo, "--message", message]
        )

    def close_issue(self, repo: str, issue_iid: str) -> None:
        self.runner(["glab", "issue", "close", issue_iid, "--repo", repo])
