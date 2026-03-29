from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import ControlBreadcrumb, new_id
from .store import SQLiteHarnessStore


class BreadcrumbWriter:
    def __init__(self, store: SQLiteHarnessStore, workspace_root: str | Path) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root)

    @property
    def breadcrumbs_root(self) -> Path:
        return self.workspace_root / "control" / "breadcrumbs"

    def write_bundle(
        self,
        *,
        entity_type: str,
        entity_id: str,
        meta: dict[str, Any],
        evidence: dict[str, Any],
        decision: dict[str, Any],
        worker_run_id: str | None = None,
        classification: str | None = None,
        summary: str | None = None,
    ) -> Path:
        bundle_id = new_id("breadcrumb")
        bundle_dir = self.breadcrumbs_root / entity_type / entity_id / bundle_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (bundle_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (bundle_dir / "decision.json").write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if summary:
            (bundle_dir / "summary.txt").write_text(summary.strip() + "\n", encoding="utf-8")

        self.store.create_control_breadcrumb(
            ControlBreadcrumb(
                id=bundle_id,
                entity_type=entity_type,
                entity_id=entity_id,
                worker_run_id=worker_run_id,
                classification=classification or str(decision.get("classification") or ""),
                path=str(bundle_dir),
            )
        )
        return bundle_dir
