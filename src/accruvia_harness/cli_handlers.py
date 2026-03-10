from __future__ import annotations

from .commands.common import CLIContext, build_context
from .commands.core import handle_core_command
from .commands.interrogation import handle_interrogation_command
from .commands.issues import handle_issue_command
from .commands.runtime import handle_runtime_command


def handle_command(args, ctx: CLIContext) -> None:
    for handler in (
        handle_runtime_command,
        handle_core_command,
        handle_issue_command,
        handle_interrogation_command,
    ):
        if handler(args, ctx):
            return
    raise RuntimeError(f"Unsupported command: {args.command}")
