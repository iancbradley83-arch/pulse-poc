"""
pulse-ops-bot — entry point.

Starts two concurrent coroutines:
  1. aiogram long-poll bot
  2. aiohttp health server on PORT (default 8080)

Safe to kill and restart at any time (stateless restart).
On boot, sends a ping to all allowed chat IDs.

Stage 3: callback_query outer middleware registered so inline-button taps
go through the same auth gate as typed commands.
"""
import asyncio
import logging
import sys
from typing import List

from aiohttp import web
from aiogram import Bot, Dispatcher

import ops_bot.config as cfg
from ops_bot.auth import AllowlistMiddleware
from ops_bot.cost_alerter import CostAlerter
from ops_bot.deploy_alerter import DeployAlerter
from ops_bot.digests import DigestScheduler
from ops_bot.feed_alerter import FeedAlerter
from ops_bot.formatting import format_boot_ping
from ops_bot.handlers import router, set_clients
from ops_bot.deeplink_alerter import DeeplinkAlerter
from ops_bot.health_alerter import HealthAlerter
from ops_bot.pulse_client import PulseClient, PulseError
from ops_bot.railway_client import RailwayClient
from ops_bot import webhooks as _webhooks

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def start_health_server(port: int, broadcast_fn) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)

    # Stage 4 lite — webhook scaffolding (inert until env vars + public domain set).
    async def sentry_handler(request: web.Request) -> web.Response:
        return await _webhooks.handle_sentry(request, broadcast_fn)

    async def report_handler(request: web.Request) -> web.Response:
        return await _webhooks.handle_report(request, broadcast_fn)

    app.router.add_post("/sentry", sentry_handler)
    app.router.add_post("/report", report_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("health server listening on port %d", port)
    return runner


# ---------------------------------------------------------------------------
# Boot ping
# ---------------------------------------------------------------------------

async def send_boot_ping(
    bot: Bot,
    allowed_ids: List[int],
    pulse_client: PulseClient,
) -> None:
    pulse_ok = False
    spend_usd = 0.0
    limit_usd = 3.0

    try:
        health = await pulse_client.health()
        pulse_ok = bool(health.get("ok", False))
    except PulseError as exc:
        logger.warning("boot ping: health check failed: %s", exc)

    try:
        cost = await pulse_client.cost(days=1)
        spend_usd = float(cost.get("total_usd", 0.0))
        limit_usd = float(cost.get("limit_usd", 3.0))
    except PulseError as exc:
        logger.warning("boot ping: cost fetch failed: %s", exc)

    msg = format_boot_ping(pulse_ok, spend_usd, limit_usd)

    for chat_id in allowed_ids:
        try:
            await bot.send_message(chat_id, msg)
            logger.info("boot ping sent to chat %s", chat_id)
        except Exception as exc:
            logger.warning("boot ping: could not send to chat %s: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # --- Config ---
    token = cfg.get_telegram_token()
    allowed_ids = cfg.get_allowed_chat_ids()
    pulse_base_url = cfg.get_pulse_base_url()
    pulse_admin_user = cfg.get_pulse_admin_user()
    pulse_admin_pass = cfg.get_pulse_admin_pass()
    railway_api_token = cfg.get_railway_api_token()
    health_port = cfg.get_health_port()

    logger.info(
        "starting ops-bot: allowed_ids=%s pulse=%s health_port=%d",
        allowed_ids,
        pulse_base_url,
        health_port,
    )

    # --- Clients ---
    pulse_client = PulseClient(pulse_base_url, pulse_admin_user, pulse_admin_pass)

    railway_client: RailwayClient | None = None
    if railway_api_token:
        railway_client = RailwayClient(railway_api_token)
    else:
        logger.warning("RAILWAY_API_TOKEN not set — Railway features will be unavailable")

    # --- Telegram bot ---
    bot = Bot(token=token)
    dp = Dispatcher()

    # Auth middleware — registered on BOTH message and callback_query so
    # inline-button taps go through the same allowlist gate as typed commands.
    auth_mw = AllowlistMiddleware(allowed_ids)
    dp.message.outer_middleware(auth_mw)
    dp.callback_query.outer_middleware(auth_mw)

    # Wire command handlers.
    set_clients(pulse_client, railway_client)
    dp.include_router(router)

    # --- Cost alerter ---
    # Pass bot + allowed_ids so the alerter can attach inline keyboards.
    async def broadcast(text: str) -> None:
        for chat_id in allowed_ids:
            try:
                await bot.send_message(chat_id, text)
            except Exception as exc:
                logger.error("broadcast: could not send to %s: %s", chat_id, exc)

    alerter = CostAlerter(
        pulse_client,
        broadcast,
        bot=bot,
        allowed_ids=allowed_ids,
    )

    # broadcast_with_kb is used by the new alerters which send inline keyboards.
    async def broadcast_with_kb(text: str, reply_markup=None) -> None:
        from aiogram.types import InlineKeyboardMarkup
        for chat_id in allowed_ids:
            try:
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
            except Exception as exc:
                logger.error("broadcast_with_kb: could not send to %s: %s", chat_id, exc)

    # --- Stage 2B alerters ---
    deploy_alerter: DeployAlerter | None = None
    if railway_client is not None:
        deploy_alerter = DeployAlerter(railway_client, broadcast_with_kb)

    health_alerter = HealthAlerter(pulse_client, broadcast_with_kb)
    feed_alerter = FeedAlerter(pulse_client, broadcast_with_kb)
    deeplink_alerter = DeeplinkAlerter(pulse_client, broadcast_with_kb)

    # --- Digest scheduler ---
    digest_scheduler = DigestScheduler(bot, allowed_ids, pulse_client, railway_client)

    # --- Health server ---
    health_runner = await start_health_server(health_port, broadcast)

    # --- Boot sequence ---
    spend = await alerter.initialise()
    logger.info("boot: current spend $%.2f", spend)

    if deploy_alerter is not None:
        await deploy_alerter.initialise()

    if allowed_ids:
        await send_boot_ping(bot, allowed_ids, pulse_client)
    else:
        logger.warning("OPS_BOT_ALLOWED_CHAT_IDS is empty — boot ping skipped")

    # Start background polling tasks.
    alerter.start()
    health_alerter.start()
    feed_alerter.start()
    deeplink_alerter.start()
    if deploy_alerter is not None:
        deploy_alerter.start()
    digest_scheduler.start()

    logger.info("ops-bot ready — starting long-poll")

    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        logger.info("ops-bot shutting down")
        alerter.stop()
        health_alerter.stop()
        feed_alerter.stop()
        deeplink_alerter.stop()
        if deploy_alerter is not None:
            deploy_alerter.stop()
        digest_scheduler.stop()
        await pulse_client.close()
        if railway_client:
            await railway_client.close()
        await bot.session.close()
        await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
