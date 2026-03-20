from __future__ import annotations

import sys
from pathlib import Path


def enforce_repo_src_first() -> None:
    """Temporary pre-release import safety guard for local test execution.

    TODO(remove after packaged release): drop this once local installs/imports are
    deterministic enough that test commands no longer need to force `src` ahead of
    generated workspaces.
    """

    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    src_text = str(src_path)
    current = [entry for entry in sys.path if entry != src_text]
    sys.path[:] = [src_text, *current]


enforce_repo_src_first()
