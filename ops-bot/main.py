"""
pulse-ops-bot — entry point.

Starts two concurrent coroutines:
  1. aiogram long-poll bot
  2. aiohttp health server on PORT (default 8080)

Safe to kill and restart at any time (stateless restart).
On boot, sends a ping to all allowed chat IDs.
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
from ops_bot.formatting import format_boot_ping
from ops_bot.handlers import router, set_clients
from ops_bot.pulse_client import PulseClient, PulseError
from ops_bot.railway_client import RailwayClient

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


async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
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

    # Auth middleware — applied to all updates.
    dp.message.middleware(AllowlistMiddleware(allowed_ids))

    # Wire command handlers.
    set_clients(pulse_client, railway_client)
    dp.include_router(router)

    # --- Cost alerter ---
    async def broadcast(text: str) -> None:
        for chat_id in allowed_ids:
            try:
                await bot.send_message(chat_id, text)
            except Exception as exc:
                logger.error("broadcast: could not send to %s: %s", chat_id, exc)

    alerter = CostAlerter(pulse_client, broadcast)

    # --- Health server ---
    health_runner = await start_health_server(health_port)

    # --- Boot sequence ---
    spend = await alerter.initialise()
    logger.info("boot: current spend $%.2f", spend)

    if allowed_ids:
        await send_boot_ping(bot, allowed_ids, pulse_client)
    else:
        logger.warning("OPS_BOT_ALLOWED_CHAT_IDS is empty — boot ping skipped")

    # Start background cost polling.
    alerter.start()

    logger.info("ops-bot ready — starting long-poll")

    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        logger.info("ops-bot shutting down")
        alerter.stop()
        await pulse_client.close()
        if railway_client:
            await railway_client.close()
        await bot.session.close()
        await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
