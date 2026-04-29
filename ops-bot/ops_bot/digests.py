"""
DigestScheduler — sends daily digests to all OPS_BOT_ALLOWED_CHAT_IDS.

Default schedule: 09:00 UTC (morning) and 22:00 UTC (evening).
Override via OPS_BOT_DIGEST_TIMES_UTC env var (see digest_times.py).

Each digest composes:
  - Pulse health
  - Today's cost
  - Yesterday's cost (morning digest only)
  - Latest deploy
  - Cards in feed (from /admin/cost.json?detail=1)
  - Engine kill switches
  - Active snoozes (if any)

If all data fetches fail entirely, a one-line fallback is sent instead of
silently skipping.

Implementation: one asyncio Task that sleeps until the next scheduled time,
fires the digest, then sleeps again.  Safe to kill and restart at any time.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable, List, Optional, Tuple

from aiogram import Bot

from .config import RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID
from .digest_times import get_digest_times_utc
from .formatting import format_digest
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError
from . import snooze as _snooze

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[str], Awaitable[None]]


def _seconds_until(hour: int, minute: int, now_utc: Optional[datetime] = None) -> float:
    """
    Return the number of seconds until the next occurrence of HH:MM UTC.
    Always returns a positive number (0 < result <= 86400).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    target = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_utc:
        target += timedelta(days=1)
    return (target - now_utc).total_seconds()


def _next_digest(
    times: List[Tuple[int, int]],
    now_utc: Optional[datetime] = None,
) -> Tuple[float, int, int]:
    """
    Given a sorted list of (hour, minute) digest times, return
    (seconds_until_next, hour, minute) for the soonest upcoming slot.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    best_secs: Optional[float] = None
    best_h, best_m = times[0]

    for h, m in times:
        secs = _seconds_until(h, m, now_utc)
        if best_secs is None or secs < best_secs:
            best_secs = secs
            best_h, best_m = h, m

    return best_secs, best_h, best_m  # type: ignore[return-value]


def _digest_kind(hour: int, minute: int) -> str:
    """Return "morning" for 09:xx UTC, "evening" for anything else."""
    return "morning" if hour < 12 else "evening"


class DigestScheduler:
    """
    Schedules and sends daily digests.

    Parameters
    ----------
    bot           : aiogram Bot instance
    allowed_ids   : list of Telegram chat IDs to broadcast to
    pulse_client  : PulseClient
    railway_client: RailwayClient (may be None — Railway fields degrade gracefully)
    """

    def __init__(
        self,
        bot: Bot,
        allowed_ids: List[int],
        pulse_client: PulseClient,
        railway_client: Optional[RailwayClient],
    ) -> None:
        self._bot = bot
        self._allowed_ids = allowed_ids
        self._pulse = pulse_client
        self._railway = railway_client
        self._task: asyncio.Task | None = None

    async def _broadcast(self, text: str) -> None:
        for chat_id in self._allowed_ids:
            try:
                await self._bot.send_message(chat_id, text)
            except Exception as exc:
                logger.error("digest: failed to send to chat %s: %s", chat_id, exc)

    async def _fetch_digest_data(self, kind: str) -> Optional[dict]:
        """
        Fetch all data needed for a digest.  Returns a dict of raw data,
        or None if every Pulse call failed (triggers the fallback message).
        """
        any_pulse_ok = False

        health = None
        try:
            health = await self._pulse.health()
            any_pulse_ok = True
        except PulseError as exc:
            logger.warning("digest: health fetch failed: %s", exc)

        cost_today = None
        try:
            cost_today = await self._pulse.cost(days=1)
            any_pulse_ok = True
        except PulseError as exc:
            logger.warning("digest: cost fetch failed: %s", exc)

        cost_yesterday = None
        if kind == "morning":
            try:
                cost_yesterday = await self._pulse.cost(days=2)
                any_pulse_ok = True
            except PulseError as exc:
                logger.warning("digest: cost (yesterday) fetch failed: %s", exc)

        cost_detail = None
        try:
            cost_detail = await self._pulse.cost_detail()
            any_pulse_ok = True
        except PulseError as exc:
            logger.info("digest: cost_detail fetch failed (non-blocking): %s", exc)

        deployment = None
        engine_vars = None
        if self._railway is not None:
            try:
                deployment = await self._railway.latest_deployment(
                    RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID
                )
            except RailwayError as exc:
                logger.warning("digest: deployment fetch failed: %s", exc)

            try:
                engine_vars = await self._railway.variables(
                    RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
                )
            except RailwayError as exc:
                logger.warning("digest: variables fetch failed: %s", exc)

        if not any_pulse_ok and deployment is None and engine_vars is None:
            return None  # Total failure — trigger fallback

        return {
            "health": health,
            "cost_today": cost_today,
            "cost_yesterday": cost_yesterday,
            "cost_detail": cost_detail,
            "deployment": deployment,
            "engine_vars": engine_vars,
        }

    async def _send_digest(self, kind: str) -> None:
        """Fetch data and send the digest.  Handles total failure gracefully."""
        logger.info("digest: sending %s digest", kind)
        data = await self._fetch_digest_data(kind)

        if data is None:
            # All fetches failed — send a fallback one-liner.
            fallback = f"[ops-bot] {kind} digest — Pulse unreachable"
            logger.warning("digest: all data fetches failed — sending fallback")
            await self._broadcast(fallback)
            return

        active_snoozes = _snooze.current()

        text = format_digest(
            digest_kind=kind,
            health=data["health"],
            cost_today=data["cost_today"],
            cost_yesterday=data["cost_yesterday"],
            deployment=data["deployment"],
            cost_detail=data["cost_detail"],
            engine_vars=data["engine_vars"],
            active_snoozes=active_snoozes if active_snoozes else None,
        )
        await self._broadcast(text)
        logger.info("digest: %s digest sent to %d chat(s)", kind, len(self._allowed_ids))

    async def _run(self) -> None:
        """Main scheduler loop.  Sleeps until next digest time, fires, repeats."""
        times = get_digest_times_utc()
        logger.info(
            "digest: scheduler started — times UTC: %s",
            ", ".join(f"{h:02d}:{m:02d}" for h, m in times),
        )

        while True:
            sleep_secs, next_h, next_m = _next_digest(times)
            kind = _digest_kind(next_h, next_m)
            logger.info(
                "digest: next %s digest at %02d:%02d UTC (~%.0fs)",
                kind,
                next_h,
                next_m,
                sleep_secs,
            )

            await asyncio.sleep(sleep_secs)

            try:
                await self._send_digest(kind)
            except Exception as exc:
                logger.error("digest: unexpected error sending %s digest: %s", kind, exc)

    def start(self) -> None:
        """Launch the background digest scheduler task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        logger.info("digest: scheduler task started")

    def stop(self) -> None:
        """Cancel the background digest scheduler task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("digest: scheduler task stopped")
