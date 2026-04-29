"""
FeedAlerter — polls Pulse /api/feed every 5 minutes and alerts on:

  1. Card-count low:    feed has < 5 cards
  2. Hook diversity collapse: > 80% of cards share the same hook_type

Both use once-per-UTC-day dedup (keyed on date + condition).

The alert ends with an inline keyboard:
  [FEED]  [RERUN]  [DISMISS]
"""
import asyncio
import logging
from datetime import date
from typing import Callable, Awaitable, List, Optional, Set

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .formatting import format_feed_alert_low_cards, format_feed_alert_hook_collapse
from .pulse_client import PulseClient, PulseError
from . import snooze as _snooze

logger = logging.getLogger(__name__)

FEED_POLL_INTERVAL = 300  # 5 minutes
CARD_COUNT_MIN = 5
HOOK_COLLAPSE_PCT = 80  # percent


SendFn = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _feed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="FEED", callback_data="action:feed"),
        InlineKeyboardButton(text="RERUN", callback_data="action:rerun"),
        InlineKeyboardButton(text="DISMISS", callback_data="action:dismiss"),
    ]])


def _today_iso() -> str:
    return date.today().isoformat()


class FeedAlerter:
    """
    Fires WARN alerts on feed health issues.

    Parameters
    ----------
    pulse_client  : PulseClient
    send_fn       : async callable(text, reply_markup) — broadcasts to all chat IDs
    poll_interval : seconds between feed polls (default 300)
    """

    def __init__(
        self,
        pulse_client: PulseClient,
        send_fn: SendFn,
        poll_interval: int = FEED_POLL_INTERVAL,
    ) -> None:
        self._pulse = pulse_client
        self._send = send_fn
        self._poll_interval = poll_interval

        # Dedup set of "date:condition" keys.
        self._fired: Set[str] = set()
        self._current_day: str = _today_iso()
        self._task: asyncio.Task | None = None

    def _dedup_key(self, condition: str) -> str:
        return f"{self._current_day}:{condition}"

    def _already_fired(self, condition: str) -> bool:
        return self._dedup_key(condition) in self._fired

    def _mark_fired(self, condition: str) -> None:
        self._fired.add(self._dedup_key(condition))

    def _check_day_rollover(self) -> None:
        today = _today_iso()
        if today != self._current_day:
            logger.info(
                "feed_alerter: day rollover %s -> %s, resetting dedup set",
                self._current_day,
                today,
            )
            self._current_day = today
            self._fired = set()

    @staticmethod
    def _dominant_hook(cards: List[dict]) -> Optional[tuple]:
        """
        Return (hook_type, pct) if one hook type accounts for > HOOK_COLLAPSE_PCT%
        of cards, otherwise None.
        """
        if not cards:
            return None
        counts: dict = {}
        for card in cards:
            hook = card.get("hook_type") or card.get("bet_type") or "unknown"
            counts[hook] = counts.get(hook, 0) + 1
        top_hook, top_count = max(counts.items(), key=lambda x: x[1])
        pct = int(top_count / len(cards) * 100)
        if pct > HOOK_COLLAPSE_PCT:
            return top_hook, pct
        return None

    async def _check_and_alert(self) -> None:
        """Single poll cycle."""
        self._check_day_rollover()

        if _snooze.is_snoozed("feed"):
            logger.debug("feed_alerter: snoozed, skipping")
            return

        try:
            feed_data = await self._pulse.feed()
        except PulseError as exc:
            logger.warning("feed_alerter: poll failed: %s", exc)
            return

        cards = feed_data.get("cards", [])
        count = len(cards)

        # --- Card-count low ---
        if count < CARD_COUNT_MIN and not self._already_fired("low_cards"):
            self._mark_fired("low_cards")
            msg = format_feed_alert_low_cards(count)
            logger.info("feed_alerter: low card count %d — sending alert", count)
            try:
                await self._send(msg, _feed_keyboard())
            except Exception as exc:
                logger.error("feed_alerter: failed to send low-cards alert: %s", exc)

        # --- Hook diversity collapse ---
        dominant = self._dominant_hook(cards)
        if dominant is not None and not self._already_fired("hook_collapse"):
            hook_type, pct = dominant
            self._mark_fired("hook_collapse")
            msg = format_feed_alert_hook_collapse(hook_type, pct, count)
            logger.info(
                "feed_alerter: hook diversity collapse %s=%d%% (%d cards) — sending alert",
                hook_type,
                pct,
                count,
            )
            try:
                await self._send(msg, _feed_keyboard())
            except Exception as exc:
                logger.error("feed_alerter: failed to send hook-collapse alert: %s", exc)

    async def _poll_loop(self) -> None:
        """Background poll loop. Runs until cancelled. Crash-safe."""
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("feed_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("feed_alerter: polling started (interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("feed_alerter: polling stopped")
