"""
Pure action logic for Stage 3 write commands.

Each function returns (success: bool, summary: str).
No confirm UX lives here — that's in handlers.py.
No side-effects beyond the Railway / Pulse API calls.

Constants used from config:
  RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID
"""
import logging
from typing import Tuple

from .config import RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError

logger = logging.getLogger(__name__)

ActionResult = Tuple[bool, str]


async def pause(railway_client: RailwayClient) -> ActionResult:
    """
    Set PULSE_RERUN_ENABLED=false and PULSE_NEWS_INGEST_ENABLED=false
    on the pulse-poc service.
    """
    errors = []
    for name in ("PULSE_RERUN_ENABLED", "PULSE_NEWS_INGEST_ENABLED"):
        try:
            await railway_client.set_variable(
                RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
                name, "false",
            )
            logger.info("pause: set %s=false", name)
        except RailwayError as exc:
            logger.error("pause: failed to set %s: %s", name, exc)
            errors.append(f"{name}: {exc}")

    if errors:
        return False, "partial failure — " + "; ".join(errors)
    return True, "PULSE_RERUN_ENABLED=false, PULSE_NEWS_INGEST_ENABLED=false"


async def resume(railway_client: RailwayClient) -> ActionResult:
    """
    Set PULSE_RERUN_ENABLED=true and PULSE_NEWS_INGEST_ENABLED=true
    on the pulse-poc service.
    """
    errors = []
    for name in ("PULSE_RERUN_ENABLED", "PULSE_NEWS_INGEST_ENABLED"):
        try:
            await railway_client.set_variable(
                RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
                name, "true",
            )
            logger.info("resume: set %s=true", name)
        except RailwayError as exc:
            logger.error("resume: failed to set %s: %s", name, exc)
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
    """
    try:
        await railway_client.set_variable(
            RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID,
            name, value,
        )
        logger.info("flag: set %s=%s", name, value)
        return True, f"{name}={value}"
    except RailwayError as exc:
        logger.error("flag: failed to set %s=%s: %s", name, value, exc)
        return False, f"Railway error: {exc}"


async def redeploy(railway_client: RailwayClient) -> ActionResult:
    """
    Trigger a Railway deploymentRedeploy on the latest deployment of pulse-poc.
    Looks up the latest deployment ID first, then calls the mutation.
    """
    try:
        deployment = await railway_client.latest_deployment(
            RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID
        )
        deployment_id = deployment["id"]
    except RailwayError as exc:
        logger.error("redeploy: could not fetch latest deployment: %s", exc)
        return False, f"could not fetch deployment: {exc}"

    try:
        result = await railway_client.redeploy_latest(deployment_id)
        new_id = result.get("id", "?")
        status = result.get("status", "?")
        logger.info("redeploy: triggered deployment %s (status=%s)", new_id, status)
        return True, f"redeploy triggered — new deployment {new_id[:8]} (status={status})"
    except RailwayError as exc:
        logger.error("redeploy: mutation failed: %s", exc)
        return False, f"Railway error: {exc}"
