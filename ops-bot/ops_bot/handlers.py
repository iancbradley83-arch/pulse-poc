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
from .formatting import (
    format_help,
    format_status,
    format_cost,
    format_breakdown,
    format_feed_audit,
    format_feed_page,
    format_card_detail,
    format_embed,
    format_logs,
    format_env_var,
)
from .feed_audit import build_feed_summary, get_page
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError
from . import runbook as _runbook

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

    # Fetch enriched cost detail for the cards-in-feed + $/card line.
    # Optional — failure here doesn't mark Pulse unreachable; we just skip the line.
    cost_detail = None
    try:
        cost_detail = await _pulse_client.cost_detail()
    except PulseError as exc:
        logger.info("status: cost_detail fetch failed (non-blocking): %s", exc)

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
        cost_detail=cost_detail,
    )
    await message.answer(text)


@router.message(Command("breakdown"))
async def cmd_breakdown(message: Message) -> None:
    pulse_unreachable = False
    detail = None
    try:
        detail = await _pulse_client.cost_detail()
    except PulseError as exc:
        logger.warning("breakdown: fetch failed: %s", exc)
        pulse_unreachable = True

    if detail is None:
        if pulse_unreachable:
            await message.answer("(Pulse unreachable)")
        else:
            await message.answer("no breakdown data available")
        return

    text = format_breakdown(detail)
    if pulse_unreachable:
        text += "\n(Pulse unreachable)"
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


# ---------------------------------------------------------------------------
# Stage 2 — /feed [page <n>]
# ---------------------------------------------------------------------------

@router.message(Command("feed"))
async def cmd_feed(message: Message) -> None:
    """/feed — feed audit summary. Use /cards [page] for the paginated card list."""
    feed_data = None
    try:
        feed_data = await _pulse_client.feed()
    except PulseError as exc:
        logger.warning("feed: fetch failed: %s", exc)
        await message.answer("(Pulse unreachable)")
        return

    cards = feed_data.get("cards", [])
    summary = build_feed_summary(cards)
    text = format_feed_audit(summary)
    if cards:
        text += "\n\nuse /cards to scroll the actual cards"
    await message.answer(text)


@router.message(Command("cards"))
async def cmd_cards(message: Message) -> None:
    """/cards [page] — paginated card list (5 per page)."""
    text = message.text or ""
    parts = text.strip().split()
    page = 1
    if len(parts) >= 2:
        try:
            page = int(parts[1])
            if page < 1:
                page = 1
        except ValueError:
            await message.answer("usage: /cards [page]  — page must be a number")
            return

    feed_data = None
    try:
        feed_data = await _pulse_client.feed()
    except PulseError as exc:
        logger.warning("cards: fetch failed: %s", exc)
        await message.answer("(Pulse unreachable)")
        return

    cards = feed_data.get("cards", [])
    page_cards, total_pages = get_page(cards, page)
    out = format_feed_page(page_cards, page, total_pages, len(cards))
    await message.answer(out)


# ---------------------------------------------------------------------------
# Stage 2 — /card <id>
# ---------------------------------------------------------------------------

@router.message(Command("card"))
async def cmd_card(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split(None, 1)

    # No arg → list a few card IDs from the feed so the user has something to copy.
    if len(parts) < 2 or not parts[1].strip():
        try:
            feed_data = await _pulse_client.feed()
        except PulseError:
            await message.answer("usage: /card <id>  — Pulse unreachable, try later")
            return
        cards = feed_data.get("cards", [])[:5]
        if not cards:
            await message.answer("usage: /card <id>  — no cards in feed right now")
            return
        rows = []
        for c in cards:
            cid = (c.get("id") or "")[:8]
            game = c.get("game") or {}
            home = game.get("home_team") or game.get("home") or ""
            away = game.get("away_team") or game.get("away") or ""
            rows.append(f"  {cid}  {home} vs {away}".rstrip())
        body = "\n".join(rows)
        await message.answer(
            f"usage: /card <id>\n\nrecent cards:\n{body}\n\nor see /cards for the full list"
        )
        return

    card_id = parts[1].strip()

    feed_data = None
    try:
        feed_data = await _pulse_client.feed()
    except PulseError as exc:
        logger.warning("card: feed fetch failed: %s", exc)
        await message.answer("(Pulse unreachable)")
        return

    cards = feed_data.get("cards", [])

    matched = None
    for card in cards:
        cid = card.get("id") or ""
        if cid == card_id:
            matched = card
            break

    if matched is None and len(card_id) <= 8:
        for card in cards:
            cid = card.get("id") or ""
            if cid[:8] == card_id[:8]:
                matched = card
                break

    if matched is None:
        await message.answer(f"card {card_id} not in feed")
        return

    await message.answer(format_card_detail(matched))


# ---------------------------------------------------------------------------
# Stage 2 — /embed <slug>
# ---------------------------------------------------------------------------

@router.message(Command("embed"))
async def cmd_embed(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split(None, 1)

    # If no arg, fetch the embed list and surface it as a hint.
    slug = parts[1].strip() if len(parts) >= 2 and parts[1].strip() else None

    try:
        data = await _pulse_client.embeds()
    except PulseError as exc:
        err = str(exc)
        if "404" in err:
            await message.answer(
                "endpoint not exposed yet — need PULSE pr to expose /admin/embeds.json"
            )
        else:
            logger.warning("embed: fetch failed: %s", exc)
            await message.answer("(Pulse unreachable)")
        return

    embeds = data.get("embeds", [])

    if slug is None:
        available = ", ".join(e.get("slug", "?") for e in embeds) or "none"
        await message.answer(f"usage: /embed <slug>\n\nconfigured: {available}")
        return

    matched = next((e for e in embeds if e.get("slug") == slug), None)
    if matched is None:
        available = ", ".join(e.get("slug", "?") for e in embeds) or "none"
        await message.answer(f"no embed with slug '{slug}'. available: {available}")
        return

    await message.answer(format_embed(matched))


# ---------------------------------------------------------------------------
# Stage 2 — /logs [n]
# ---------------------------------------------------------------------------

@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split()
    n = 20
    if len(parts) >= 2:
        try:
            n = int(parts[1])
            if n < 1:
                n = 1
            elif n > 100:
                n = 100
        except ValueError:
            await message.answer("usage: /logs [n]  — n must be a number (max 100)")
            return

    if _railway_client is None:
        await message.answer("(Railway API unreachable)")
        return

    try:
        entries = await _railway_client.recent_logs(RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, n)
    except RailwayError as exc:
        logger.warning("logs: fetch failed: %s", exc)
        await message.answer("(Railway API unreachable)")
        return

    await message.answer(format_logs(entries, n))


# ---------------------------------------------------------------------------
# Stage 2 — /runbook <topic>
# ---------------------------------------------------------------------------

@router.message(Command("runbook"))
async def cmd_runbook(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split(None, 1)

    # No topic → list available topics so the user knows what to ask for.
    if len(parts) < 2 or not parts[1].strip():
        topics = await _runbook.list_topics()
        if topics is None:
            await message.answer("usage: /runbook <topic>  — runbook unavailable, try later")
            return
        body = "\n".join(f"  {t}" for t in topics) or "  (none)"
        await message.answer(f"usage: /runbook <topic>\n\navailable topics:\n{body}")
        return

    topic = parts[1].strip()
    result = await _runbook.lookup(topic)
    await message.answer(result)


# ---------------------------------------------------------------------------
# Stage 2 — /env <key>
# ---------------------------------------------------------------------------

@router.message(Command("env"))
async def cmd_env(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("usage: /env <key>")
        return

    key = parts[1].strip()

    if _railway_client is None:
        await message.answer(format_env_var(key, None, railway_unreachable=True))
        return

    try:
        variables = await _railway_client.variables(
            RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
        )
    except RailwayError as exc:
        logger.warning("env: variables fetch failed: %s", exc)
        await message.answer(format_env_var(key, None, railway_unreachable=True))
        return

    value = variables.get(key)
    await message.answer(format_env_var(key, value))
