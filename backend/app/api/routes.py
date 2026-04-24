"""REST API routes for the feed."""
from __future__ import annotations

import logging
from typing import Optional
from fastapi import APIRouter, Query
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.services.game_simulator import GameSimulator
from app.engine.feed_ranker import rank_cards
from app.config import PULSE_BET_TYPE_MIX

logger = logging.getLogger(__name__)

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
        # 50 keeps the full mix visible. Ordering now driven by
        # app.engine.feed_ranker (score + bet-type quota + variety guard);
        # see docs/refresh-and-ordering.md §2.
        limit: int = 50,
    ):
        if type == "prematch":
            # Apply ranker v1: score-based sort, PULSE_BET_TYPE_MIX quota
            # interleaving, variety guard, drop no-shows + dupes. Falls back
            # to the FeedManager's relevance-only list if anything blows up.
            try:
                pool = list(feed.prematch_cards)
                if sport:
                    pool = [c for c in pool if c.game.sport.value == sport]
                ranked = rank_cards(pool, PULSE_BET_TYPE_MIX, limit=limit)
                return {"cards": [c.model_dump() for c in ranked]}
            except Exception as exc:
                logger.warning(
                    "[PULSE] feed_ranker failed, falling back to relevance "
                    "sort: %s", exc,
                )
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
