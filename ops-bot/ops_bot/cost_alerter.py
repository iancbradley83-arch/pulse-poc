"""
Cost alerter — polls Pulse /admin/cost every 300s and sends CRITICAL alerts
when today's spend crosses a threshold for the first time.

Dedup key: "{date_iso}:{threshold}"

On boot:
  - Reads current cost from Pulse.
  - Marks already-crossed thresholds as fired (no re-spam on redeploy).
  - Sends boot ping to all allowed chat IDs.

At UTC midnight:
  - Resets the dedup set and last-seen total.
"""
import asyncio
import logging
from datetime import date, timezone, datetime
from typing import Callable, Awaitable, List, Set

from .config import COST_THRESHOLDS, DAILY_BUDGET, COST_POLL_INTERVAL
from .formatting import format_cost_alert, format_boot_ping
from .pulse_client import PulseClient, PulseError

logger = logging.getLogger(__name__)

# Type alias for the send-message callback.
SendFn = Callable[[str], Awaitable[None]]


def _today_iso() -> str:
    return date.today().isoformat()


class CostAlerter:
    """
    Fires threshold alerts at most once per threshold per calendar day.

    Parameters
    ----------
    pulse_client : PulseClient
    send_fn      : async callable(text) — sends message to all allowed chat IDs
    thresholds   : ordered list of USD thresholds (default from config)
    poll_interval: seconds between polls (default from config)
    """

    def __init__(
        self,
        pulse_client: PulseClient,
        send_fn: SendFn,
        thresholds: List[float] = COST_THRESHOLDS,
        poll_interval: int = COST_POLL_INTERVAL,
        daily_budget: float = DAILY_BUDGET,
    ) -> None:
        self._pulse = pulse_client
        self._send = send_fn
        self._thresholds = sorted(thresholds)
        self._poll_interval = poll_interval
        self._daily_budget = daily_budget

        # Dedup set of "date_iso:threshold_str" keys.
        self._fired: Set[str] = set()
        self._current_day = _today_iso()
        self._task: asyncio.Task | None = None

    def _dedup_key(self, threshold: float) -> str:
        return f"{self._current_day}:{threshold:.2f}"

    def _mark_fired(self, threshold: float) -> None:
        self._fired.add(self._dedup_key(threshold))

    def _already_fired(self, threshold: float) -> bool:
        return self._dedup_key(threshold) in self._fired

    def _check_day_rollover(self) -> None:
        today = _today_iso()
        if today != self._current_day:
            logger.info("cost_alerter: day rollover %s -> %s, resetting dedup set", self._current_day, today)
            self._current_day = today
            self._fired = set()

    async def _get_today_spend(self) -> float:
        """Return today's spend in USD, or raise PulseError."""
        cost = await self._pulse.cost(days=1)
        return float(cost.get("total_usd", 0.0))

    async def initialise(self) -> float:
        """
        Called at boot. Reads current spend, marks already-crossed thresholds
        as fired so a redeploy doesn't re-spam. Returns current spend.
        """
        try:
            spend = await self._get_today_spend()
        except PulseError as exc:
            logger.warning("cost_alerter: initialise — could not fetch cost: %s", exc)
            return 0.0

        for threshold in self._thresholds:
            if spend >= threshold:
                self._mark_fired(threshold)
                logger.info(
                    "cost_alerter: boot-recovery — threshold $%.2f already crossed (spend $%.2f), marked as fired",
                    threshold,
                    spend,
                )
        return spend

    async def _check_and_alert(self) -> None:
        """Single poll cycle — fetch spend and fire any new threshold alerts."""
        self._check_day_rollover()
        try:
            spend = await self._get_today_spend()
        except PulseError as exc:
            logger.warning("cost_alerter: poll failed: %s", exc)
            return

        for threshold in self._thresholds:
            if spend >= threshold and not self._already_fired(threshold):
                self._mark_fired(threshold)
                msg = format_cost_alert(spend, threshold, self._daily_budget)
                logger.info(
                    "cost_alerter: threshold $%.2f crossed (spend $%.2f) — sending alert",
                    threshold,
                    spend,
                )
                try:
                    await self._send(msg)
                except Exception as exc:
                    logger.error("cost_alerter: failed to send alert: %s", exc)

    async def _poll_loop(self) -> None:
        """Background poll loop. Runs until cancelled."""
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("cost_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("cost_alerter: polling started (interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("cost_alerter: polling stopped")
