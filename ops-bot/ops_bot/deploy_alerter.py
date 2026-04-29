"""
DeployAlerter — polls Railway for the latest pulse-poc deployment every 60s
and sends a CRITICAL alert if it transitions to FAILED or CRASHED.

Dedup key: "{deployment_id}:{status}"

On boot:
  - Fetches current deployment status.
  - If already FAILED/CRASHED, marks as fired (no spam on bot redeploy).

The alert message ends with an inline keyboard:
  [REDEPLOY]  [LOGS]  [DISMISS]
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional, Set

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID
from .formatting import format_deploy_alert
from .railway_client import RailwayClient, RailwayError
from . import snooze as _snooze

logger = logging.getLogger(__name__)

DEPLOY_POLL_INTERVAL = 60  # seconds
ALERT_STATUSES = {"FAILED", "CRASHED"}

SendFn = Callable[[str, Optional[InlineKeyboardMarkup]], Awaitable[None]]


def _deploy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="REDEPLOY", callback_data="action:redeploy"),
        InlineKeyboardButton(text="LOGS", callback_data="action:logs"),
        InlineKeyboardButton(text="DISMISS", callback_data="action:dismiss"),
    ]])


class DeployAlerter:
    """
    Fires an alert when the latest pulse-poc deployment transitions to FAILED
    or CRASHED. Deduplicates per deployment ID + status.

    Parameters
    ----------
    railway_client : RailwayClient
    send_fn        : async callable(text, reply_markup) — broadcasts to all chat IDs
    poll_interval  : seconds between Railway polls (default 60)
    """

    def __init__(
        self,
        railway_client: RailwayClient,
        send_fn: SendFn,
        poll_interval: int = DEPLOY_POLL_INTERVAL,
    ) -> None:
        self._railway = railway_client
        self._send = send_fn
        self._poll_interval = poll_interval

        # Dedup set of "deployment_id:status" keys.
        self._fired: Set[str] = set()
        self._task: asyncio.Task | None = None

    def _dedup_key(self, deployment_id: str, status: str) -> str:
        return f"{deployment_id}:{status}"

    def _already_fired(self, deployment_id: str, status: str) -> bool:
        return self._dedup_key(deployment_id, status) in self._fired

    def _mark_fired(self, deployment_id: str, status: str) -> None:
        self._fired.add(self._dedup_key(deployment_id, status))

    async def _fetch_latest(self) -> Optional[dict]:
        """Return latest deployment dict or None on error."""
        try:
            return await self._railway.latest_deployment(RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID)
        except RailwayError as exc:
            logger.warning("deploy_alerter: fetch failed: %s", exc)
            return None

    async def initialise(self) -> None:
        """
        Called at boot. Reads current deployment status; if already FAILED/CRASHED,
        marks as fired so a bot redeploy doesn't re-alert for an old failure.
        """
        deployment = await self._fetch_latest()
        if deployment is None:
            return

        status = deployment.get("status", "")
        dep_id = deployment.get("id", "")
        if status in ALERT_STATUSES:
            self._mark_fired(dep_id, status)
            logger.info(
                "deploy_alerter: boot-recovery — deployment %s already %s, marked as fired",
                dep_id[:8],
                status,
            )

    async def _check_and_alert(self) -> None:
        """Single poll cycle — fetch deployment and fire alert if warranted."""
        if _snooze.is_snoozed("deploy"):
            logger.debug("deploy_alerter: snoozed, skipping")
            return

        deployment = await self._fetch_latest()
        if deployment is None:
            return

        status = deployment.get("status", "")
        dep_id = deployment.get("id", "")
        commit = (deployment.get("commitHash", "") or "")[:7] or "unknown"

        if status in ALERT_STATUSES and not self._already_fired(dep_id, status):
            self._mark_fired(dep_id, status)
            msg = format_deploy_alert(commit, status)
            logger.info(
                "deploy_alerter: deployment %s is %s — sending alert",
                dep_id[:8],
                status,
            )
            try:
                await self._send(msg, _deploy_keyboard())
            except Exception as exc:
                logger.error("deploy_alerter: failed to send alert: %s", exc)

    async def _poll_loop(self) -> None:
        """Background poll loop. Runs until cancelled. Crash-safe."""
        while True:
            try:
                await self._check_and_alert()
            except Exception as exc:
                logger.error("deploy_alerter: unexpected error in poll loop: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("deploy_alerter: polling started (interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("deploy_alerter: polling stopped")
