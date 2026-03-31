"""REST API routes for the feed."""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Query
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.services.game_simulator import GameSimulator

router = APIRouter(prefix="/api")


def create_routes(
    catalog: MarketCatalog,
    feed: FeedManager,
    simulator: GameSimulator,
) -> APIRouter:

    @router.get("/feed")
    async def get_feed(
        type: str = Query("prematch", regex="^(prematch|live)$"),
        sport: Optional[str] = None,
        game_id: Optional[str] = None,
        limit: int = 20,
    ):
        if type == "prematch":
            return {"cards": feed.get_prematch_feed(sport=sport, limit=limit)}
        else:
            return {"cards": feed.get_live_feed(game_id=game_id, limit=limit)}

    @router.get("/games")
    async def get_games():
        return {"games": [g.model_dump() for g in simulator._games.values()]}

    @router.get("/games/{game_id}/markets")
    async def get_game_markets(game_id: str):
        markets = catalog.get_by_game(game_id)
        return {"markets": [m.model_dump() for m in markets]}

    @router.post("/simulator/start")
    async def start_simulator():
        if simulator.is_running:
            return {"status": "already_running"}
        await simulator.start()
        return {"status": "started"}

    @router.post("/simulator/stop")
    async def stop_simulator():
        await simulator.stop()
        return {"status": "stopped"}

    @router.get("/simulator/status")
    async def simulator_status():
        return {"running": simulator.is_running}

    return router
