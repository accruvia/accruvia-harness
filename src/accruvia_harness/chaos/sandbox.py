"""Isolated sandbox for chaos testing -- own DB, own worktree, resource limits."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from accruvia_harness.store import SQLiteHarnessStore

logger = logging.getLogger(__name__)


@dataclass
class ChaosSandbox:
    """Isolated environment for chaos monkey execution."""

    sandbox_root: Path
    db_path: Path
    worktree_path: Path | None
    store: SQLiteHarnessStore | None = None
    memory_limit_mb: int = 2048
    cpu_limit_seconds: int = 300

    def initialize(self, source_db: Path, source_repo: Path | None = None) -> None:
        self.sandbox_root.mkdir(parents=True, exist_ok=True)

        # Checkpoint WAL to ensure all data is in the main DB file, then copy
        import sqlite3
        try:
            src_conn = sqlite3.connect(source_db)
            src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            src_conn.close()
        except sqlite3.Error:
            pass  # best-effort; copy may still work
        shutil.copy2(source_db, self.db_path)
        self.store = SQLiteHarnessStore(str(self.db_path))
        self.store.initialize()
        logger.info("Sandbox DB initialized at %s", self.db_path)

        # Create git worktree if repo provided
        if source_repo and source_repo.exists():
            branch_name = f"chaos/{os.getpid()}"
            try:
                subprocess.run(
                    ["git", "worktree", "add", "-b", branch_name,
                     str(self.worktree_path)],
                    cwd=source_repo,
                    capture_output=True, check=True, timeout=30,
                )
                logger.info("Sandbox worktree at %s (branch %s)",
                            self.worktree_path, branch_name)
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                logger.warning("Worktree creation failed: %s", exc)
                self.worktree_path = None

    def apply_resource_limits(self) -> None:
        """Apply resource limits so chaos cannot OOM the host."""
        import resource

        mem_bytes = self.memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (self.cpu_limit_seconds, self.cpu_limit_seconds),
        )
        logger.info("Resource limits: %dMB memory, %ds CPU",
                     self.memory_limit_mb, self.cpu_limit_seconds)

    def teardown(self, source_repo: Path | None = None) -> None:
        if self.worktree_path and self.worktree_path.exists() and source_repo:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force",
                     str(self.worktree_path)],
                    cwd=source_repo,
                    capture_output=True, check=True, timeout=30,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                logger.warning("Failed to remove worktree %s", self.worktree_path)

        if self.sandbox_root.exists():
            shutil.rmtree(self.sandbox_root, ignore_errors=True)
        logger.info("Sandbox torn down")


@contextmanager
def chaos_sandbox(
    source_db: Path,
    source_repo: Path | None = None,
    memory_limit_mb: int = 2048,
    cpu_limit_seconds: int = 300,
):
    """Context manager that yields an isolated ChaosSandbox."""
    root = Path(tempfile.mkdtemp(prefix="chaos_"))
    sandbox = ChaosSandbox(
        sandbox_root=root,
        db_path=root / "chaos.db",
        worktree_path=root / "worktree" if source_repo else None,
        memory_limit_mb=memory_limit_mb,
        cpu_limit_seconds=cpu_limit_seconds,
    )
    try:
        sandbox.initialize(source_db, source_repo)
        sandbox.apply_resource_limits()
        yield sandbox
    finally:
        sandbox.teardown(source_repo)
