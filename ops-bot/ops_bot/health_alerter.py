"""
HealthAlerter — polls Pulse /health every 60s and fires a CRITICAL alert
after 2 consecutive failures (~2 minutes of downtime).

Recovery: when /health returns 200 after being down, push a recovery notice
and re-arm the alerter (ready to fire again on the next outage).

Dedup: once the CRITICAL alert fires, stay silent until /health recovers.

The alert ends with an inline keyboard:
  [STATUS]  [REDEPLOY]  [DISMISS]
"""
import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .formatting import format_health_alert, format_health_recovery
from .pulse_client import PulseClient, PulseError
from . import snooze as _snooze

logger = logging.getLogger(__name__)

HEALTH_POLL_INTERVAL = 60  # seconds
CONSECUTIVE_FAIL_THRESHOLD = 2  # polls before alerting


SendFn = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="STATUS", callback_data="action:status"),
        InlineKeyboardButton(text="REDEPLOY", callback_data="action:redeploy"),
        InlineKeyboardButton(text="DISMISS", callback_data="action:dismiss"),
    ]])


class HealthAlerter:
    """
    Fires a CRITICAL alert after CONSECUTIVE_FAIL_THRESHOLD consecutive /health
    failures.  Sends a recovery notice when /health comes back.

    Parameters
    ----------
    pulse_client  : PulseClient
    send_fn       : async callable(text, reply_markup) — broadcasts to all chat IDs
    poll_interval : seconds between health checks (default 60)
    fail_threshold: consecutive failures before alerting (default 2)
    """

    def __init__(
        self,
        pulse_client: PulseClient,
        send_fn: SendFn,
        poll_interval: int = HEALTH_POLL_INTERVAL,
        fail_threshold: int = CONSECUTIVE_FAIL_THRESHOLD,
    ) -> None:
        self._pulse = pulse_client
        self._send = send_fn
        self._poll_interval = poll_interval
        self._fail_threshold = fail_threshold

        self._consecutive_fails: int = 0
        self._alert_fired: bool = False
        self._down_since: Optional[float] = None  # monotonic timestamp
        self._task: asyncio.Task | None = None

    def _down_minutes(self) -> int:
        if self._down_since is None:
            return 0
        return max(1, int((time.monotonic() - self._down_since) / 60))

    async def _check_health(self) -> bool:
        """Return True if /health is ok, False on any failure."""
        try:
            resp = await self._pulse.health()
            return bool(resp.get("ok", False))
        except PulseError:
            return False

    async def _check_and_alert(self) -> None:
        """Single poll cycle."""
        snoozed = _snooze.is_snoozed("health")

        healthy = await self._check_health()

        if healthy:
            if self._alert_fired:
                # Recovery — reset state and push recovery notice.
                down_mins = self._down_minutes()
                self._consecutive_fails = 0
                self._alert_fired = False
                self._down_since = None
                logger.info("health_alerter: /health recovered after ~%dm", down_mins)
                if not snoozed:
                    try:
                        await self._send(format_health_recovery(down_mins), None)
                    except Exception as exc:
                        logger.error("health_alerter: failed to send recovery: %s", exc)
            else:
                # All good.
                self._consecutive_fails = 0
                self._down_since = None
            return

        # Unhealthy.
        if self._down_since is None:
            self._down_since = time.monotonic()

        self._consecutive_fails += 1
        logger.info(
            "health_alerter: /health fail #%d (threshold=%d)",
            self._consecutive_fails,
            self._fail_threshold,
        )

        if self._consecutive_fails >= self._fail_threshold and not self._alert_fired:
            self._alert_fired = True
            down_mins = self._down_minutes()
            logger.info(
                "health_alerter: threshold reached — sending CRITICAL alert (~%dm down)",
                down_mins,
            )
            if snoozed:
                logger.info("health_alerter: snoozed, suppressing alert")
                return
            try:
                await self._send(format_health_alert(down_mins), _health_keyboard())
            except Exception as exc:
                logger.error("health_alerter: failed to send alert: %s", exc)

    async def _poll_loop(self) -> None:
        """Background poll loop. Runs until cancelled. Crash-safe."""
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("health_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("health_alerter: polling started (interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("health_alerter: polling stopped")
