from __future__ import annotations

from pathlib import Path


def verify_repo_import_path(module_file: str | Path, repo_root: str | Path) -> Path:
    """Ensure accruvia_harness resolves from this repo's ./src tree.

    Temporary pre-release guard.
    TODO(remove after packaged release): delete this once local installs/imports are
    deterministic enough that test entrypoints no longer need to defend against
    stale generated workspaces shadowing the repo code.
    """

    module_path = Path(module_file).resolve()
    expected_root = (Path(repo_root).resolve() / "src").resolve()
    try:
        module_path.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError(
            "Unsafe test import path detected: accruvia_harness resolved outside the repo src tree. "
            f"expected_root={expected_root} actual_module={module_path}"
        ) from exc
    return module_path
