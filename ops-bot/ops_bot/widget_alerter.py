"""
WidgetAlerter — polls the live widget HTML at Pulse `/` every 5 min and fires
a CRITICAL alert when the rendered page itself is broken even though the
backend /health and /api/feed look fine.

This catches the gap between "API is up" and "users see a working widget":
- 5xx on the root path (Cloudflare cache poisoning, edge issues)
- 200 but malformed HTML (template error, missing JS bundle reference)
- Stale or partial response from the cache layer

Sentinels checked on each successful 200 response:
- the page contains the title `Pulse — News-driven bets`
- the page contains `<div id="app"` (mount point)
- the response is `text/html`

Snooze kind: "frontend".
Recovery: when sentinels return after a fail, push a recovery notice.

Inline keyboard on alert: [PREVIEW] [STATUS] [DISMISS]
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .formatting import format_widget_alert, format_widget_recovery
from . import snooze as _snooze

logger = logging.getLogger(__name__)

WIDGET_POLL_INTERVAL = 300  # 5 min — slower than /health since this is heavier
CONSECUTIVE_FAIL_THRESHOLD = 2
REQUEST_TIMEOUT = 8.0

EXPECTED_TITLE = "Pulse — News-driven bets"
EXPECTED_MOUNT = '<div id="app"'


SendFn = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _widget_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="PREVIEW", callback_data="action:preview"),
        InlineKeyboardButton(text="STATUS", callback_data="action:status"),
        InlineKeyboardButton(text="DISMISS", callback_data="action:dismiss"),
    ]])


def evaluate_widget_response(
    status_code: int,
    content_type: str,
    body: str,
) -> Optional[str]:
    """
    Return None if the response looks healthy.
    Return a short failure reason string if not.
    """
    if status_code != 200:
        return f"http {status_code}"
    if "text/html" not in (content_type or "").lower():
        return f"non-html content-type: {content_type or 'missing'}"
    if EXPECTED_TITLE not in body:
        return "missing title sentinel"
    if EXPECTED_MOUNT not in body:
        return "missing mount-point sentinel"
    if len(body) < 500:
        return f"suspiciously short body ({len(body)} bytes)"
    return None


class WidgetAlerter:
    """
    Fires a CRITICAL alert after CONSECUTIVE_FAIL_THRESHOLD consecutive
    widget-render failures. Sends a recovery notice when sentinels come back.

    Parameters
    ----------
    widget_url    : full URL to the widget root, e.g. https://pulse-poc-production.up.railway.app/
    send_fn       : async callable(text, reply_markup) — broadcasts to all chat IDs
    poll_interval : seconds between checks (default 300)
    fail_threshold: consecutive failures before alerting (default 2)
    """

    def __init__(
        self,
        widget_url: str,
        send_fn: SendFn,
        poll_interval: int = WIDGET_POLL_INTERVAL,
        fail_threshold: int = CONSECUTIVE_FAIL_THRESHOLD,
    ) -> None:
        self._widget_url = widget_url
        self._send = send_fn
        self._poll_interval = poll_interval
        self._fail_threshold = fail_threshold

        self._consecutive_fails: int = 0
        self._alert_fired: bool = False
        self._down_since: Optional[float] = None
        self._last_reason: Optional[str] = None
        self._task: asyncio.Task | None = None

    def _down_minutes(self) -> int:
        if self._down_since is None:
            return 0
        return max(1, int((time.monotonic() - self._down_since) / 60))

    async def _probe(self) -> Optional[str]:
        """Return None if widget renders, else a failure-reason string."""
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(self._widget_url)
            return evaluate_widget_response(
                resp.status_code,
                resp.headers.get("content-type", ""),
                resp.text,
            )
        except httpx.TimeoutException:
            return "timeout"
        except Exception as exc:
            return f"request failed: {type(exc).__name__}"

    async def _check_and_alert(self) -> None:
        snoozed = _snooze.is_snoozed("frontend")
        reason = await self._probe()

        if reason is None:
            if self._alert_fired:
                down_mins = self._down_minutes()
                self._consecutive_fails = 0
                self._alert_fired = False
                self._down_since = None
                self._last_reason = None
                logger.info("widget_alerter: widget recovered after ~%dm", down_mins)
                if not snoozed:
                    try:
                        await self._send(format_widget_recovery(down_mins), None)
                    except Exception as exc:
                        logger.error("widget_alerter: failed to send recovery: %s", exc)
            else:
                self._consecutive_fails = 0
                self._down_since = None
                self._last_reason = None
            return

        # Failed.
        if self._down_since is None:
            self._down_since = time.monotonic()

        self._consecutive_fails += 1
        self._last_reason = reason
        logger.info(
            "widget_alerter: widget probe failed #%d (reason=%s, threshold=%d)",
            self._consecutive_fails,
            reason,
            self._fail_threshold,
        )

        if self._consecutive_fails >= self._fail_threshold and not self._alert_fired:
            self._alert_fired = True
            down_mins = self._down_minutes()
            logger.info(
                "widget_alerter: threshold reached — sending CRITICAL alert (~%dm, reason=%s)",
                down_mins,
                reason,
            )
            if snoozed:
                logger.info("widget_alerter: snoozed, suppressing alert")
                return
            try:
                await self._send(
                    format_widget_alert(down_mins, reason),
                    _widget_keyboard(),
                )
            except Exception as exc:
                logger.error("widget_alerter: failed to send alert: %s", exc)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("widget_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "widget_alerter: polling %s every %ds",
            self._widget_url,
            self._poll_interval,
        )

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("widget_alerter: polling stopped")
