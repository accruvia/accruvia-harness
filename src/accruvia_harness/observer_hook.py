"""Fire-and-forget webhook POST for observer notifications."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

NOTIFY_EVENT_TYPES = frozenset({
    "task_completed",
    "task_failed",
    "task_status_changed",
    "promotion_rejected",
    "promotion_approved",
    "branch_winner_selected",
    "run_blocked",
})

# Bounded pool prevents unbounded thread accumulation when the observer is slow/down.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="observer-hook")


def notify_observer(webhook_url: str, event_type: str, entity_type: str, entity_id: str, payload: dict) -> None:
    """POST an event to the observer webhook. Non-blocking, fire-and-forget."""
    if event_type not in NOTIFY_EVENT_TYPES:
        return
    event = {
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload": payload,
    }
    _pool.submit(_post_event, webhook_url, event)


def _post_event(url: str, event: dict) -> None:
    try:
        data = json.dumps(event).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("Observer webhook POST failed (non-fatal): %s", exc)
