"""
Command handlers for the ops-bot.

Stage 1 commands: /help, /status, /cost [days]

Each handler degrades gracefully: if any upstream call fails it shows
partial info and appends the appropriate unreachable notice.
"""
import logging
import time
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .config import (
    RAILWAY_PROJECT_ID,
    RAILWAY_SERVICE_ID,
    RAILWAY_ENVIRONMENT_ID,
)
from .formatting import format_help, format_status, format_cost
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError

logger = logging.getLogger(__name__)

router = Router()

# These are set by main.py after the clients are initialised.
_pulse_client: Optional[PulseClient] = None
_railway_client: Optional[RailwayClient] = None


def set_clients(pulse: PulseClient, railway: Optional[RailwayClient]) -> None:
    global _pulse_client, _railway_client
    _pulse_client = pulse
    _railway_client = railway


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(format_help())


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    start = time.monotonic()
    pulse_unreachable = False
    railway_unreachable = False

    # Fetch Pulse health.
    health = None
    try:
        health = await _pulse_client.health()
    except PulseError as exc:
        logger.warning("status: health check failed: %s", exc)
        pulse_unreachable = True

    # Fetch today's cost.
    cost = None
    try:
        cost = await _pulse_client.cost(days=1)
    except PulseError as exc:
        logger.warning("status: cost fetch failed: %s", exc)
        pulse_unreachable = True

    # Fetch feed count.
    feed = None
    try:
        feed = await _pulse_client.feed()
    except PulseError as exc:
        logger.warning("status: feed fetch failed: %s", exc)
        pulse_unreachable = True

    # Fetch Railway deployment + engine vars.
    deployment = None
    engine_vars = None
    if _railway_client is not None:
        try:
            deployment = await _railway_client.latest_deployment(
                RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID
            )
        except RailwayError as exc:
            logger.warning("status: deployment fetch failed: %s", exc)
            railway_unreachable = True

        try:
            engine_vars = await _railway_client.variables(
                RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
            )
        except RailwayError as exc:
            logger.warning("status: variables fetch failed: %s", exc)
            railway_unreachable = True
    else:
        railway_unreachable = True

    elapsed = int(time.monotonic() - start)

    text = format_status(
        health=health,
        cost=cost,
        deployment=deployment,
        feed=feed,
        engine_vars=engine_vars,
        pulse_unreachable=pulse_unreachable,
        railway_unreachable=railway_unreachable,
        check_age_seconds=elapsed,
    )
    await message.answer(text)


@router.message(Command("cost"))
async def cmd_cost(message: Message) -> None:
    # Parse optional days argument.
    days = 3
    if message.text:
        parts = message.text.strip().split()
        if len(parts) >= 2:
            try:
                days = int(parts[1])
                if days < 1:
                    days = 1
                elif days > 30:
                    days = 30
            except ValueError:
                await message.answer("usage: /cost [days] — days must be a number")
                return

    pulse_unreachable = False
    cost = None
    try:
        cost = await _pulse_client.cost(days=days)
    except PulseError as exc:
        logger.warning("cost: fetch failed: %s", exc)
        pulse_unreachable = True

    if cost is None or not cost.get("days"):
        if pulse_unreachable:
            await message.answer("(Pulse unreachable)")
        else:
            await message.answer("no cost data available")
        return

    text = format_cost(cost["days"], days)
    if pulse_unreachable:
        text += "\n(Pulse unreachable)"
    await message.answer(text)
