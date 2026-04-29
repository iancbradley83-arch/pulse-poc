"""
Command handlers for the ops-bot.

Stage 1 commands: /help, /status, /cost [days]
Stage 2 commands: /feed, /cards, /card, /embed, /logs, /runbook, /env
Stage 3 commands: /pause, /resume, /rerun, /flag, /redeploy, /snooze
                  + callback_query handler for inline-keyboard buttons

Each handler degrades gracefully: if any upstream call fails it shows
partial info and appends the appropriate unreachable notice.

Stage 3 confirm pattern:
  1. User types /pause (or taps [PAUSE] button)
  2. Bot replies with confirm prompt + inline keyboard [confirm pause] [cancel]
  3. User has 30s to tap [confirm pause] OR reply 'yes'
  4. On confirm: execute action, reply with result
  5. On cancel or expiry: reply with appropriate message
"""
import logging
import time
from typing import Optional

import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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
    format_confirm_prompt,
    format_action_result,
    _team_name,
)
from . import help_topics as _help_topics
from .feed_audit import build_feed_summary, get_page
from .pulse_client import PulseClient, PulseError
from .railway_client import RailwayClient, RailwayError
from . import runbook as _runbook
from . import confirm as _confirm
from . import snooze as _snooze
from . import write_actions as _actions

logger = logging.getLogger(__name__)

router = Router()

# These are set by main.py after the clients are initialised.
_pulse_client: Optional[PulseClient] = None
_railway_client: Optional[RailwayClient] = None


def set_clients(pulse: PulseClient, railway: Optional[RailwayClient]) -> None:
    global _pulse_client, _railway_client
    _pulse_client = pulse
    _railway_client = railway


_HELP_UNDERSCORE_RE = re.compile(r"^/help_([a-z]+)(?:@\w+)?(?:\s|$)")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = message.text or ""
    parts = text.strip().split(None, 1)
    if len(parts) >= 2 and parts[1].strip():
        await message.answer(_help_topics.render(parts[1].strip()))
        return
    await message.answer(format_help())


@router.message(F.text.regexp(_HELP_UNDERSCORE_RE))
async def cmd_help_underscore(message: Message) -> None:
    """Match /help_status, /help_cost, etc. — tappable shortcuts from /help footer."""
    m = _HELP_UNDERSCORE_RE.match(message.text or "")
    if not m:
        return
    await message.answer(_help_topics.render(m.group(1)))


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


async def _do_cards(message: Message, page: int) -> None:
    """Shared body for /cards, /cards N, /cards_N."""
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
    await _do_cards(message, page)


# Telegram makes /cards_2-style tokens tappable as commands. Match them via
# a regex on message.text since aiogram's Command filter doesn't accept the
# underscore-suffix form.
_CARDS_UNDERSCORE_RE = re.compile(r"^/cards_(\d+)(?:@\w+)?(?:\s|$)")


@router.message(F.text.regexp(_CARDS_UNDERSCORE_RE))
async def cmd_cards_underscore(message: Message) -> None:
    """Match /cards_2, /cards_3, etc. — tappable pagination from the footer."""
    m = _CARDS_UNDERSCORE_RE.match(message.text or "")
    if not m:
        return
    page = int(m.group(1)) or 1
    await _do_cards(message, page)


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
            hook = c.get("hook_type") or c.get("bet_type") or "?"
            game = c.get("game") or {}
            home = _team_name(game.get("home_team") or game.get("home"))
            away = _team_name(game.get("away_team") or game.get("away"))
            game_str = f"{home} vs {away}" if home != "?" or away != "?" else ""
            rows.append(f"  {cid}  {hook}  {game_str}".rstrip())
        body = "\n".join(rows)
        await message.answer(
            f"usage: /card <id>\n\nrecent cards:\n{body}\n\nor /cards for the full list"
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


# ---------------------------------------------------------------------------
# Stage 3 helpers
# ---------------------------------------------------------------------------

def _confirm_keyboard(action_id: str) -> InlineKeyboardMarkup:
    """Return the [confirm <action>] [cancel] inline keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"confirm {action_id}",
            callback_data=f"confirm:{action_id}",
        ),
        InlineKeyboardButton(
            text="cancel",
            callback_data="confirm:cancel",
        ),
    ]])


async def _send_confirm_prompt(
    message: Message,
    action_id: str,
    detail: str,
    args=None,
) -> None:
    """Register a pending confirm and send the prompt with inline keyboard."""
    chat_id = message.chat.id
    _confirm.register(chat_id, action_id, args)
    text = format_confirm_prompt(action_id, detail)
    await message.answer(text, reply_markup=_confirm_keyboard(action_id))


async def _execute_action(action_id: str, args, chat_id: int, reply_fn) -> None:
    """
    Execute a confirmed action and call reply_fn(text) with the result.

    reply_fn is an async callable that sends a message back to the chat.
    """
    success = False
    summary = "unknown action"

    if action_id == "pause":
        if _railway_client is None:
            await reply_fn("(Railway API unavailable — cannot pause)")
            return
        success, summary = await _actions.pause(_railway_client)

    elif action_id == "resume":
        if _railway_client is None:
            await reply_fn("(Railway API unavailable — cannot resume)")
            return
        success, summary = await _actions.resume(_railway_client)

    elif action_id == "rerun":
        success, summary = await _actions.rerun(_pulse_client)

    elif action_id == "redeploy":
        if _railway_client is None:
            await reply_fn("(Railway API unavailable — cannot redeploy)")
            return
        success, summary = await _actions.redeploy(_railway_client)

    elif action_id == "flag":
        if _railway_client is None:
            await reply_fn("(Railway API unavailable — cannot set flag)")
            return
        name, value = args
        success, summary = await _actions.flag(_railway_client, name, value)

    await reply_fn(format_action_result(action_id, success, summary))


# ---------------------------------------------------------------------------
# Stage 3 — /pause
# ---------------------------------------------------------------------------

@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """/pause — flip PULSE_RERUN_ENABLED and PULSE_NEWS_INGEST_ENABLED to false."""
    if _railway_client is None:
        await message.answer("(Railway API unavailable — cannot pause)")
        return
    await _send_confirm_prompt(
        message,
        action_id="pause",
        detail="sets PULSE_RERUN_ENABLED=false, PULSE_NEWS_INGEST_ENABLED=false",
    )


# ---------------------------------------------------------------------------
# Stage 3 — /resume
# ---------------------------------------------------------------------------

@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    """/resume — flip PULSE_RERUN_ENABLED and PULSE_NEWS_INGEST_ENABLED to true."""
    if _railway_client is None:
        await message.answer("(Railway API unavailable — cannot resume)")
        return
    await _send_confirm_prompt(
        message,
        action_id="resume",
        detail="sets PULSE_RERUN_ENABLED=true, PULSE_NEWS_INGEST_ENABLED=true",
    )


# ---------------------------------------------------------------------------
# Stage 3 — /rerun
# ---------------------------------------------------------------------------

@router.message(Command("rerun"))
async def cmd_rerun(message: Message) -> None:
    """/rerun — POST /admin/rerun to trigger engine cycle."""
    await _send_confirm_prompt(
        message,
        action_id="rerun",
        detail="POST /admin/rerun — triggers candidate engine cycle",
    )


# ---------------------------------------------------------------------------
# Stage 3 — /flag <NAME> <true|false>
# ---------------------------------------------------------------------------

@router.message(Command("flag"))
async def cmd_flag(message: Message) -> None:
    """/flag <NAME> <value> — generic env-var flip on pulse-poc."""
    text = message.text or ""
    parts = text.strip().split(None, 2)

    if len(parts) < 3:
        await message.answer(
            "usage: /flag <NAME> <value>\n\n"
            "example: /flag PULSE_RERUN_ENABLED false"
        )
        return

    if _railway_client is None:
        await message.answer("(Railway API unavailable — cannot set flag)")
        return

    name = parts[1].strip().upper()
    value = parts[2].strip()

    await _send_confirm_prompt(
        message,
        action_id="flag",
        detail=f"sets {name}={value} on pulse-poc",
        args=(name, value),
    )


# ---------------------------------------------------------------------------
# Stage 3 — /redeploy
# ---------------------------------------------------------------------------

@router.message(Command("redeploy"))
async def cmd_redeploy(message: Message) -> None:
    """/redeploy — Railway deploymentRedeploy on the latest deployment."""
    if _railway_client is None:
        await message.answer("(Railway API unavailable — cannot redeploy)")
        return
    await _send_confirm_prompt(
        message,
        action_id="redeploy",
        detail="Railway deploymentRedeploy on latest pulse-poc deployment",
    )


# ---------------------------------------------------------------------------
# Stage 3 — /snooze [kind] [duration]
# ---------------------------------------------------------------------------

@router.message(Command("snooze"))
async def cmd_snooze(message: Message) -> None:
    """/snooze [kind] [duration] — suppress a class of push alerts."""
    text = message.text or ""
    parts = text.strip().split(None, 2)

    # Bare /snooze — show current state.
    if len(parts) == 1:
        state = _snooze.current()
        if not state:
            await message.answer("no active snoozes")
        else:
            lines = ["active snoozes:"]
            for kind, info in sorted(state.items()):
                remaining = info["remaining_seconds"]
                h = remaining // 3600
                m = (remaining % 3600) // 60
                if h:
                    dur_str = f"{h}h {m}m"
                else:
                    dur_str = f"{m}m"
                lines.append(f"  {kind}: {dur_str} remaining")
            await message.answer("\n".join(lines))
        return

    kind = parts[1].strip().lower()
    if kind not in _snooze.VALID_KINDS:
        valid = ", ".join(sorted(_snooze.VALID_KINDS))
        await message.answer(
            f"unknown kind '{kind}'. valid: {valid}\n\n"
            "usage: /snooze <kind> <30m|1h|2h|off>"
        )
        return

    if len(parts) < 3:
        await message.answer(
            f"usage: /snooze {kind} <30m|1h|2h|off>\n\n"
            "example: /snooze cost 1h"
        )
        return

    duration_str = parts[2].strip()
    duration_seconds = _snooze.parse_duration(duration_str)

    if duration_seconds is None:
        await message.answer(
            f"unrecognised duration '{duration_str}'. use 30m, 1h, 2h, or off"
        )
        return

    if duration_seconds == 0:
        _snooze.clear(kind)
        await message.answer(f"{kind} alert snooze cleared")
        return

    _snooze.snooze(kind, duration_seconds)
    h = duration_seconds // 3600
    m = (duration_seconds % 3600) // 60
    if h:
        dur_human = f"{h}h" if not m else f"{h}h {m}m"
    else:
        dur_human = f"{m}m"
    await message.answer(f"{kind} alerts snoozed for {dur_human}")


# ---------------------------------------------------------------------------
# Stage 3 — 'yes' text fallback for pending confirms
# ---------------------------------------------------------------------------

@router.message(F.text.lower() == "yes")
async def cmd_yes(message: Message) -> None:
    """
    'yes' reply within 30s of a confirm prompt executes the pending action.
    """
    chat_id = message.chat.id
    entry = _confirm.peek(chat_id)
    if entry is None:
        await message.answer("no pending confirmation (or it expired after 30s)")
        return

    action_id, _, args = entry
    # Consume the pending confirm via resolve.
    resolved_args = _confirm.resolve(chat_id, action_id)
    if resolved_args is None:
        await message.answer("confirmation expired (30s window)")
        return

    async def reply(text: str) -> None:
        await message.answer(text)

    await _execute_action(action_id, resolved_args, chat_id, reply)


# ---------------------------------------------------------------------------
# Stage 3 — callback_query handler for inline-keyboard button taps
# ---------------------------------------------------------------------------

@router.callback_query()
async def handle_callback_query(callback: CallbackQuery) -> None:
    """
    Handle all inline-keyboard button taps.

    callback_data patterns:
      confirm:<action_id>   — user tapped [confirm <action>]
      confirm:cancel        — user tapped [cancel]
      action:pause          — user tapped [PAUSE] on a cost alert
      action:breakdown      — user tapped [BREAKDOWN] on a cost alert
      action:dismiss        — user tapped [DISMISS] on a cost alert
    """
    data = callback.data or ""
    chat_id: int
    if callback.message is not None:
        chat_id = callback.message.chat.id
    elif callback.from_user is not None:
        chat_id = callback.from_user.id
    else:
        await callback.answer("cannot determine chat")
        return

    # -----------------------------------------------------------------------
    # confirm:<action_id>
    # -----------------------------------------------------------------------
    if data.startswith("confirm:"):
        action_id = data[len("confirm:"):]

        if action_id == "cancel":
            # Remove any pending confirm for this chat.
            entry = _confirm.peek(chat_id)
            if entry is not None:
                _confirm.resolve(chat_id, entry[0])
            await callback.answer("cancelled")
            if callback.message:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return

        # Resolve the pending confirm.
        args = _confirm.resolve(chat_id, action_id)
        if args is None:
            # Check if the entry existed but expired.
            await callback.answer("expired — send the command again", show_alert=True)
            if callback.message:
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return

        # Remove the buttons so the prompt can't be double-tapped.
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        await callback.answer("executing...")

        async def reply(text: str) -> None:
            if callback.message:
                await callback.message.answer(text)

        await _execute_action(action_id, args, chat_id, reply)
        return

    # -----------------------------------------------------------------------
    # action:pause — tapped [PAUSE] on a cost alert
    # -----------------------------------------------------------------------
    if data == "action:pause":
        if _railway_client is None:
            await callback.answer("Railway API unavailable", show_alert=True)
            return
        # Register a fresh confirm (same flow as /pause).
        _confirm.register(
            chat_id, "pause",
            detail="sets PULSE_RERUN_ENABLED=false, PULSE_NEWS_INGEST_ENABLED=false",
        )
        await callback.answer()
        if callback.message:
            text = format_confirm_prompt(
                "pause",
                "sets PULSE_RERUN_ENABLED=false, PULSE_NEWS_INGEST_ENABLED=false",
            )
            await callback.message.answer(text, reply_markup=_confirm_keyboard("pause"))
        return

    # -----------------------------------------------------------------------
    # action:breakdown — tapped [BREAKDOWN] on a cost alert
    # -----------------------------------------------------------------------
    if data == "action:breakdown":
        await callback.answer()
        detail = None
        try:
            detail = await _pulse_client.cost_detail()
        except PulseError as exc:
            logger.warning("callback breakdown: fetch failed: %s", exc)
        if detail is None:
            if callback.message:
                await callback.message.answer("(Pulse unreachable)")
        else:
            from .formatting import format_breakdown
            if callback.message:
                await callback.message.answer(format_breakdown(detail))
        return

    # -----------------------------------------------------------------------
    # action:dismiss — tapped [DISMISS] on a cost alert
    # -----------------------------------------------------------------------
    if data == "action:dismiss":
        await callback.answer("dismissed")
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    # Unknown callback_data — answer silently so Telegram spinner stops.
    await callback.answer()
