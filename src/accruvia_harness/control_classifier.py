from __future__ import annotations

from pathlib import Path

from .domain import FailureClassification


class FailureClassifier:
    def classify(self, evidence: str | None, *, extra_evidence: list[str] | None = None) -> FailureClassification:
        snippets = [item for item in [evidence, *(extra_evidence or [])] if item]
        haystack = "\n".join(snippets).lower()
        evidence_list = snippets[:5]

        if "rate limit" in haystack or "429" in haystack:
            return FailureClassification(
                classification="provider_rate_limit",
                confidence=0.95,
                retry_recommended=False,
                cooldown_seconds=1800,
                evidence=evidence_list,
            )
        if "credit" in haystack and ("exhaust" in haystack or "insufficient" in haystack):
            return FailureClassification(
                classification="credit_exhaustion",
                confidence=0.95,
                retry_recommended=False,
                cooldown_seconds=0,
                evidence=evidence_list,
            )
        if "connection refused" in haystack or "service unavailable" in haystack or "503" in haystack:
            return FailureClassification(
                classification="provider_outage",
                confidence=0.8,
                retry_recommended=False,
                cooldown_seconds=1800,
                evidence=evidence_list,
            )
        if "timed out" in haystack or "timeout" in haystack:
            return FailureClassification(
                classification="timeout",
                confidence=0.8,
                retry_recommended=True,
                cooldown_seconds=0,
                evidence=evidence_list,
            )
        if "killed" in haystack or "stale" in haystack or "hung" in haystack:
            return FailureClassification(
                classification="hung_process",
                confidence=0.7,
                retry_recommended=True,
                cooldown_seconds=0,
                evidence=evidence_list,
            )
        if "traceback" in haystack or "exception" in haystack or "error:" in haystack:
            return FailureClassification(
                classification="system_failure",
                confidence=0.65,
                retry_recommended=False,
                cooldown_seconds=0,
                evidence=evidence_list,
            )
        return FailureClassification(
            classification="unknown",
            confidence=0.2,
            retry_recommended=False,
            cooldown_seconds=0,
            evidence=evidence_list,
        )

    def classify_paths(self, *paths: str | Path) -> FailureClassification:
        snippets: list[str] = []
        for path in paths:
            file_path = Path(path)
            if not file_path.exists():
                continue
            try:
                snippets.append(file_path.read_text(encoding="utf-8")[-4000:])
            except OSError:
                continue
        return self.classify(None, extra_evidence=snippets)
