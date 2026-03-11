from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class ExternalIssue:
    issue_id: str
    title: str
    body: str
    state: str
    url: str
    labels: list[str] | None = None
    milestone: str | None = None
    assignees: list[str] | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "labels": list(self.labels or []),
            "milestone": self.milestone,
            "assignees": list(self.assignees or []),
            "url": self.url,
            "state": self.state,
        }


class IssueProvider(Protocol):
    def fetch_issue(self, repo: str, issue_id: str) -> ExternalIssue: ...

    def list_open_issues(self, repo: str, limit: int) -> list[ExternalIssue]: ...

    def add_comment(self, repo: str, issue_id: str, message: str) -> None: ...

    def close_issue(self, repo: str, issue_id: str) -> None: ...

    def reopen_issue(self, repo: str, issue_id: str) -> None: ...
