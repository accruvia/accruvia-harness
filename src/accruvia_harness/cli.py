from __future__ import annotations

from .cli_handlers import build_context, handle_command
from .cli_parser import build_parser
from .config import HarnessConfig
from .logging_utils import HarnessLogger, classify_error


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = HarnessConfig.from_env(args.db, args.workspace, args.log_path)
    logger = HarnessLogger(config.log_path)
    logger.log("cli_invoked", command=args.command)

    try:
        context = build_context(config)
        handle_command(args, context)
    except Exception as exc:
        logger.log(
            "cli_error",
            command=args.command,
            error_class=exc.__class__.__name__,
            error_category=classify_error(exc),
            message=str(exc),
        )
        raise


if __name__ == "__main__":
    main()
