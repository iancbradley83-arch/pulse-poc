"""
Pure action logic for Stage 3 write commands.

Each function returns (success: bool, summary: str).
No confirm UX lives here — that's in handlers.py.
No side-effects beyond the Railway / Pulse API calls.

Constants used from config:
  RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID

Retry policy: every Railway write goes through `_retry_railway`, which
retries once after 2 seconds on `RailwayError`. Railway's API surfaces
intermittent server-side issues with the same wording as actual
permission failures (`Not Authorized`); a single transient hiccup
shouldn't read as a hard failure to the user. See incident
2026-05-03-empty-feed-stale-catalogue.md.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Tuple, TypeVar

from .config import RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError

logger = logging.getLogger(__name__)

ActionResult = Tuple[bool, str]

# One retry, 2-second backoff. Tighter than typical exponential because
# the user is staring at Telegram waiting for confirmation; we'd rather
# fail fast than make them wait.
RAILWAY_RETRY_BACKOFF_SECONDS = 2.0

_T = TypeVar("_T")


async def _retry_railway(
    op_name: str,
    fn: Callable[[], Awaitable[_T]],
) -> _T:
    """Run a Railway-API coroutine with one retry on RailwayError.

    `fn` is a no-arg async callable. On the first RailwayError, sleeps
    `RAILWAY_RETRY_BACKOFF_SECONDS` and retries once. If the retry also
    raises, the exception propagates to the caller.
    """
    try:
        return await fn()
    except RailwayError as exc:
        logger.warning(
            "%s: railway transient error, retrying in %.1fs: %s",
            op_name, RAILWAY_RETRY_BACKOFF_SECONDS, exc,
        )
        await asyncio.sleep(RAILWAY_RETRY_BACKOFF_SECONDS)
        return await fn()


async def pause(railway_client: RailwayClient) -> ActionResult:
    """
    Set PULSE_RERUN_ENABLED=false and PULSE_NEWS_INGEST_ENABLED=false
    on the pulse-poc service. One-time retry on transient RailwayError.
    """
    errors = []
    for name in ("PULSE_RERUN_ENABLED", "PULSE_NEWS_INGEST_ENABLED"):
        try:
            await _retry_railway(
                f"pause:{name}",
                lambda n=name: railway_client.set_variable(
                    RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
                    n, "false",
                ),
            )
            logger.info("pause: set %s=false", name)
        except RailwayError as exc:
            logger.error("pause: failed to set %s after retry: %s", name, exc)
            errors.append(f"{name}: {exc}")

    if errors:
        return False, "partial failure — " + "; ".join(errors)
    return True, "PULSE_RERUN_ENABLED=false, PULSE_NEWS_INGEST_ENABLED=false"


async def resume(railway_client: RailwayClient) -> ActionResult:
    """
    Set PULSE_RERUN_ENABLED=true and PULSE_NEWS_INGEST_ENABLED=true
    on the pulse-poc service. One-time retry on transient RailwayError.
    """
    errors = []
    for name in ("PULSE_RERUN_ENABLED", "PULSE_NEWS_INGEST_ENABLED"):
        try:
            await _retry_railway(
                f"resume:{name}",
                lambda n=name: railway_client.set_variable(
                    RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
                    n, "true",
                ),
            )
            logger.info("resume: set %s=true", name)
        except RailwayError as exc:
            logger.error("resume: failed to set %s after retry: %s", name, exc)
            errors.append(f"{name}: {exc}")

    if errors:
        return False, "partial failure — " + "; ".join(errors)
    return True, "PULSE_RERUN_ENABLED=true, PULSE_NEWS_INGEST_ENABLED=true"


async def rerun(pulse_client: PulseClient) -> ActionResult:
    """
    POST /admin/rerun on Pulse. Fire-and-forget on Pulse side (60s Railway edge
    cap means the engine response arrives in logs, not in the HTTP response).
    """
    try:
        result = await pulse_client.post_admin_rerun()
        ok = result.get("ok", False)
        if ok:
            return True, "rerun triggered — engine will run on next cycle"
        return False, f"Pulse returned ok=false: {result}"
    except PulseError as exc:
        logger.error("rerun: failed: %s", exc)
        return False, f"Pulse unreachable: {exc}"


async def flag(
    railway_client: RailwayClient,
    name: str,
    value: str,
) -> ActionResult:
    """
    Generic env-var flip: set NAME=value on pulse-poc via variableUpsert.
    value should be "true" or "false" (or any string).
    One-time retry on transient RailwayError.
    """
    try:
        await _retry_railway(
            f"flag:{name}",
            lambda: railway_client.set_variable(
                RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
                name, value,
            ),
        )
        logger.info("flag: set %s=%s", name, value)
        return True, f"{name}={value}"
    except RailwayError as exc:
        logger.error("flag: failed to set %s=%s after retry: %s", name, value, exc)
        return False, f"Railway error: {exc}"


async def redeploy(railway_client: RailwayClient) -> ActionResult:
    """
    Trigger a Railway deploymentRedeploy on the latest deployment of pulse-poc.
    Looks up the latest deployment ID first, then calls the mutation.
    One-time retry on transient RailwayError for both lookups and mutation.
    """
    try:
        deployment = await _retry_railway(
            "redeploy:latest_deployment",
            lambda: railway_client.latest_deployment(
                RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID,
            ),
        )
        deployment_id = deployment["id"]
    except RailwayError as exc:
        logger.error("redeploy: could not fetch latest deployment after retry: %s", exc)
        return False, f"could not fetch deployment: {exc}"

    try:
        result = await _retry_railway(
            "redeploy:mutation",
            lambda: railway_client.redeploy_latest(deployment_id),
        )
        new_id = result.get("id", "?")
        status = result.get("status", "?")
        logger.info("redeploy: triggered deployment %s (status=%s)", new_id, status)
        return True, f"redeploy triggered — new deployment {new_id[:8]} (status={status})"
    except RailwayError as exc:
        logger.error("redeploy: mutation failed after retry: %s", exc)
        return False, f"Railway error: {exc}"
