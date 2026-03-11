"""Telegram bot adapter using raw HTTP API — no external dependencies."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .agent import ObserverAgent

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


@dataclass(slots=True)
class TelegramMessage:
    chat_id: int
    text: str
    message_id: int


class TelegramAdapter:
    """Thin adapter that bridges Telegram messages to the ObserverAgent."""

    def __init__(
        self,
        bot_token: str,
        agent: ObserverAgent,
        allowed_chat_ids: set[int] | None = None,
        poll_timeout: int = 30,
    ) -> None:
        self.bot_token = bot_token
        self.agent = agent
        self.allowed_chat_ids = allowed_chat_ids
        self.poll_timeout = poll_timeout
        self._offset: int = 0
        self._running = False
        self._message_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram-observer")
        self._pending = threading.BoundedSemaphore(value=32)

    def _api_call(self, method: str, payload: dict | None = None) -> dict:
        url = TELEGRAM_API.format(token=self.bot_token, method=method)
        if payload:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        else:
            request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=self.poll_timeout + 10) as response:
            return json.loads(response.read())

    def _get_updates(self) -> list[dict]:
        payload = {"offset": self._offset, "timeout": self.poll_timeout}
        try:
            result = self._api_call("getUpdates", payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Telegram poll failed: %s", exc)
            return []
        updates = result.get("result", [])
        if updates:
            self._offset = max(u["update_id"] for u in updates) + 1
        return updates

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        *,
        parse_mode: str | None = None,
    ) -> None:
        # Telegram limits messages to 4096 chars
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_to:
                payload["reply_to_message_id"] = reply_to
                reply_to = None  # only reply to first chunk
            try:
                self._api_call("sendMessage", payload)
            except (urllib.error.URLError, OSError) as exc:
                logger.error("Failed to send Telegram message: %s", exc)

    def send_notification(self, chat_id: int, text: str) -> None:
        """Send a proactive notification to a chat."""
        self._send_message(chat_id, text)

    def _handle_message(self, message: dict) -> None:
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        message_id = message.get("message_id")
        if not chat_id or not text:
            return
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logger.info("Ignoring message from unauthorized chat %s", chat_id)
            return
        if text.startswith("/"):
            self._handle_command(chat_id, text, message_id)
            return
        self._submit_question(chat_id, text, message_id)

    def _submit_question(self, chat_id: int, text: str, message_id: int) -> None:
        if not self._pending.acquire(blocking=False):
            logger.warning("Telegram observer queue is full; dropping message for chat %s", chat_id)
            self._send_message(chat_id, "Observer is busy right now. Please try again shortly.", reply_to=message_id)
            return
        future = self._executor.submit(self._answer_question, chat_id, text, message_id)
        future.add_done_callback(lambda _: self._pending.release())

    def _answer_question(self, chat_id: int, text: str, message_id: int) -> None:
        """Ask the agent and reply. Runs in a background thread."""
        try:
            self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except (urllib.error.URLError, OSError):
            pass
        # Serialize LLM calls so we don't overwhelm the backend
        with self._message_lock:
            try:
                response = self.agent.ask(text)
            except Exception:
                logger.exception("Agent.ask() failed for chat %s", chat_id)
                self._send_message(chat_id, "Internal error processing your question.", reply_to=message_id)
                return
        if response.error:
            reply = f"Error: {response.error}"
        else:
            reply = response.answer
            if response.evidence_sources:
                reply += f"\n\n_Sources: {', '.join(response.evidence_sources)}_"
        self._send_message(chat_id, reply, reply_to=message_id)

    def _handle_command(self, chat_id: int, text: str, message_id: int) -> None:
        cmd = text.split()[0].lower()
        if cmd == "/start":
            self._send_message(chat_id, (
                "Accruvia Observer active. Ask me about harness state.\n\n"
                "Examples:\n"
                "- What's the backlog look like?\n"
                "- What happened to task\\_abc123?\n"
                "- Any promotions waiting?\n"
                "- What changed recently?\n\n"
                "Commands: /status /cache /help"
            ), reply_to=message_id)
        elif cmd == "/status":
            self._submit_question(chat_id, "Give me a brief status overview of all projects and tasks.", message_id)
        elif cmd == "/cache":
            self._send_message(chat_id, self.agent.cache.summary_text(), reply_to=message_id)
        elif cmd == "/help":
            self._send_message(chat_id, (
                "Just ask questions in plain English. I query the harness and answer from evidence.\n\n"
                "/status — quick overview\n"
                "/cache — what evidence I have cached\n"
                "/help — this message"
            ), reply_to=message_id)
        else:
            # Treat unknown commands as questions
            self._handle_message({"chat": {"id": chat_id}, "text": text[1:], "message_id": message_id})

    def run(self) -> None:
        """Start long-polling the Telegram API. Blocks until stopped."""
        logger.info("Telegram observer bot starting...")
        self._running = True
        while self._running:
            updates = self._get_updates()
            for update in updates:
                message = update.get("message")
                if message:
                    try:
                        self._handle_message(message)
                    except Exception:
                        logger.exception("Error handling message")
            if not updates:
                time.sleep(0.5)

    def stop(self) -> None:
        self._running = False
        self._executor.shutdown(wait=False, cancel_futures=True)
