"""Lightweight HTTP server that receives event webhooks from the harness."""

from __future__ import annotations

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

logger = logging.getLogger(__name__)


class _WebhookHandler(BaseHTTPRequestHandler):
    callback: Callable[[dict], None] | None = None

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        # Process the event before responding so failures aren't silently lost
        if self.callback and body:
            try:
                event = json.loads(body)
                self.callback(event)
            except Exception:
                logger.exception("Error processing webhook event")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"error")
                return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        logger.debug(format, *args)


class WebhookReceiver:
    """Receives harness event webhooks and dispatches to a callback."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8900) -> None:
        self.host = host
        self.port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, callback: Callable[[dict], None]) -> None:
        """Start the webhook server in a background thread."""
        handler = type("Handler", (_WebhookHandler,), {"callback": staticmethod(callback)})
        self._server = HTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Webhook receiver listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
