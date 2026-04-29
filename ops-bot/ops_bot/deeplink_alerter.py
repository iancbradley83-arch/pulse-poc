"""
DeeplinkAlerter — polls /api/feed every 10 min, HEAD-requests up to 5 cards'
deep_links, and alerts when >= 3 of 5 sampled return non-2xx.

Dedup key: "{date_iso}:{failure_mode}" — once per UTC day per failure mode.

Respects snooze.is_snoozed("deeplink").

Alert format:
  [ops-bot] WARN — deep_links failing for 3/5 sampled cards  ·  /preview to see  ·  /pause to halt publishing

Inline keyboard: [PREVIEW]  [PAUSE]  [DISMISS]
"""
import asyncio
import logging
from datetime import date
from typing import Callable, Awaitable, List, Optional, Set

import httpx
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .pulse_client import PulseClient, PulseError
from . import snooze as _snooze

logger = logging.getLogger(__name__)

DEEPLINK_POLL_INTERVAL = 600  # 10 minutes
DEEPLINK_SAMPLE_SIZE = 5
DEEPLINK_FAIL_THRESHOLD = 3
DEEPLINK_HEAD_TIMEOUT = 3.0  # seconds

SendFn = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _deeplink_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="PREVIEW", callback_data="action:preview"),
        InlineKeyboardButton(text="PAUSE", callback_data="action:pause"),
        InlineKeyboardButton(text="DISMISS", callback_data="action:dismiss"),
    ]])


def _today_iso() -> str:
    return date.today().isoformat()


async def _head_status(url: str) -> Optional[int]:
    """HEAD request with DEEPLINK_HEAD_TIMEOUT. Returns status or None on error."""
    try:
        async with httpx.AsyncClient(
            timeout=DEEPLINK_HEAD_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.head(url)
            return resp.status_code
    except httpx.TimeoutException:
        logger.debug("deeplink_alerter: HEAD %s timed out", url)
        return None
    except Exception as exc:
        logger.debug("deeplink_alerter: HEAD %s error: %s", url, exc)
        return None


def _is_2xx(status: Optional[int]) -> bool:
    return status is not None and 200 <= status < 300


class DeeplinkAlerter:
    """
    Fires a WARN alert when >= DEEPLINK_FAIL_THRESHOLD of DEEPLINK_SAMPLE_SIZE
    sampled deep_links return non-2xx.

    Parameters
    ----------
    pulse_client  : PulseClient
    send_fn       : async callable(text, reply_markup) — broadcasts to all chat IDs
    poll_interval : seconds between feed polls (default 600)
    """

    def __init__(
        self,
        pulse_client: PulseClient,
        send_fn: SendFn,
        poll_interval: int = DEEPLINK_POLL_INTERVAL,
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
                "deeplink_alerter: day rollover %s -> %s, resetting dedup set",
                self._current_day,
                today,
            )
            self._current_day = today
            self._fired = set()

    async def _sample_deep_links(self) -> List[Optional[str]]:
        """Return up to DEEPLINK_SAMPLE_SIZE deep_link URLs from the feed."""
        try:
            feed_data = await self._pulse.feed()
        except PulseError as exc:
            logger.warning("deeplink_alerter: feed fetch failed: %s", exc)
            return []

        cards = feed_data.get("cards", [])
        urls: List[Optional[str]] = []
        for card in cards:
            url = card.get("deep_link") or card.get("deeplink") or card.get("url") or None
            if url:
                urls.append(url)
            if len(urls) >= DEEPLINK_SAMPLE_SIZE:
                break
        return urls

    async def _check_and_alert(self) -> None:
        """Single poll cycle."""
        self._check_day_rollover()

        if _snooze.is_snoozed("deeplink"):
            logger.debug("deeplink_alerter: snoozed, skipping")
            return

        urls = await self._sample_deep_links()
        if not urls:
            logger.debug("deeplink_alerter: no deep_links to check")
            return

        # Concurrent HEAD requests.
        statuses: List[Optional[int]] = list(
            await asyncio.gather(
                *[_head_status(url) for url in urls],
                return_exceptions=False,
            )
        )

        fail_count = sum(1 for s in statuses if not _is_2xx(s))
        total_checked = len(statuses)

        logger.debug(
            "deeplink_alerter: checked %d deep_links, %d failed",
            total_checked,
            fail_count,
        )

        if fail_count >= DEEPLINK_FAIL_THRESHOLD and not self._already_fired("deeplink_fail"):
            self._mark_fired("deeplink_fail")
            msg = (
                f"[ops-bot] WARN — deep_links failing for {fail_count}/{total_checked} sampled cards"
                f"  ·  /preview to see  ·  /pause to halt publishing"
            )
            logger.info(
                "deeplink_alerter: %d/%d deep_links failing — sending alert",
                fail_count,
                total_checked,
            )
            try:
                await self._send(msg, _deeplink_keyboard())
            except Exception as exc:
                logger.error("deeplink_alerter: failed to send alert: %s", exc)

    async def _poll_loop(self) -> None:
        """Background poll loop. Runs until cancelled. Crash-safe."""
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("deeplink_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "deeplink_alerter: polling started (interval=%ds)", self._poll_interval
        )

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("deeplink_alerter: polling stopped")
