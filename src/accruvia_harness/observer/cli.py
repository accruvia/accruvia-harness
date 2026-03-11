"""CLI entry point for the observer process."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from .agent import ObserverAgent
from .evidence_cache import EvidenceCache
from .query_client import HarnessQueryClient
from .telegram import TelegramAdapter
from .webhook import WebhookReceiver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accruvia-observer",
        description="OpenClaw observer: read-only interrogation agent over Accruvia Harness state.",
    )
    parser.add_argument("--db", default=None, help="Path to the harness SQLite database.")
    parser.add_argument("--cli", default="accruvia-harness", help="Path to the harness CLI command.")
    parser.add_argument("--llm-command", default=None, help="Shell command for LLM invocation (receives prompt on stdin, writes response to stdout).")
    parser.add_argument("--project-id", default=None, help="Scope queries to a specific project.")
    parser.add_argument("--telegram-token", default=None, help="Telegram bot token.")
    parser.add_argument("--telegram-chat-id", default=None, help="Comma-separated allowed Telegram chat IDs.")
    parser.add_argument("--webhook-port", type=int, default=8900, help="Port for the event webhook receiver.")
    parser.add_argument("--poll-interval", type=int, default=900, help="Seconds between periodic evidence polls (0 to disable).")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging.")
    return parser


def _periodic_poll(agent: ObserverAgent, interval: int, stop_event: threading.Event) -> None:
    """Poll the harness periodically to keep the evidence cache warm."""
    while not stop_event.is_set():
        try:
            ctx = agent.query_client.context_packet(agent.project_id)
            if ctx.ok:
                agent.cache.record("context_packet", ctx.data)
            ops = agent.query_client.ops_report(agent.project_id)
            if ops.ok:
                agent.cache.record("ops_report", ops.data)
        except Exception as exc:
            logging.getLogger(__name__).warning("Periodic poll failed: %s", exc)
        stop_event.wait(interval)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("accruvia_observer")

    # Resolve config from args and env
    llm_command = args.llm_command or os.environ.get("OPENCLAW_LLM_COMMAND")
    if not llm_command:
        logger.error("LLM command required. Set --llm-command or OPENCLAW_LLM_COMMAND.")
        sys.exit(1)

    telegram_token = args.telegram_token or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        logger.error("Telegram bot token required. Set --telegram-token or OPENCLAW_TELEGRAM_BOT_TOKEN.")
        sys.exit(1)

    db_path = args.db or os.environ.get("OPENCLAW_HARNESS_DB")
    cli_command = args.cli or os.environ.get("OPENCLAW_HARNESS_CLI", "accruvia-harness")
    project_id = args.project_id or os.environ.get("OPENCLAW_PROJECT_ID")

    allowed_chat_ids: set[int] | None = None
    chat_id_str = args.telegram_chat_id or os.environ.get("OPENCLAW_TELEGRAM_CHAT_ID")
    if chat_id_str:
        allowed_chat_ids = {int(cid.strip()) for cid in chat_id_str.split(",") if cid.strip()}

    # Build components
    query_client = HarnessQueryClient(cli_command=cli_command, db_path=db_path)
    evidence_cache = EvidenceCache()
    agent = ObserverAgent(
        query_client=query_client,
        evidence_cache=evidence_cache,
        llm_command=llm_command,
        project_id=project_id,
    )
    telegram = TelegramAdapter(
        bot_token=telegram_token,
        agent=agent,
        allowed_chat_ids=allowed_chat_ids,
    )

    stop_event = threading.Event()

    # Start webhook receiver
    webhook = WebhookReceiver(port=args.webhook_port)

    def on_event(event: dict) -> None:
        notification = agent.process_event(event)
        if notification and allowed_chat_ids:
            for chat_id in allowed_chat_ids:
                telegram.send_notification(chat_id, f"*Harness Alert*\n{notification}")

    webhook.start(on_event)

    # Start periodic polling
    poll_thread = None
    if args.poll_interval > 0:
        poll_thread = threading.Thread(
            target=_periodic_poll,
            args=(agent, args.poll_interval, stop_event),
            daemon=True,
        )
        poll_thread.start()
        logger.info("Periodic polling every %ds", args.poll_interval)

    # Handle shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        stop_event.set()
        telegram.stop()
        webhook.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Observer started. Telegram bot polling, webhook on port %d.", args.webhook_port)
    telegram.run()


if __name__ == "__main__":
    main()
