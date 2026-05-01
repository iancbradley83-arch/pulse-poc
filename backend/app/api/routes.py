"""REST API routes for the feed."""
from __future__ import annotations

import logging
from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel
from app.models.schemas import Card
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.services.game_simulator import GameSimulator
from app.engine.feed_ranker import rank_cards
from app.config import PULSE_BET_TYPE_MIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ── Slim wire-only feed projections ────────────────────────────────────
#
# These models are NOT persisted — they exist purely to shrink the JSON
# the CDN caches at the edge. The persisted CandidateCard / Card stays
# rich; we project to FeedCard right before serializing the response.
#
# Fields kept reflect exactly what backend/app/static/app.js (+ clicks.js,
# reactions.js) actually reads off a card object. See PR body for the
# audited drop list and the byte-size before/after.
#
# DO NOT add fields here without confirming the frontend reads them — the
# whole point of this projection is to keep the wire surface honest.


class FeedTeam(BaseModel):
    short_name: str
    color: str


class FeedGame(BaseModel):
    id: str
    home_team: FeedTeam
    away_team: FeedTeam
    broadcast: str = ""
    start_time: str = ""


class FeedSelection(BaseModel):
    label: str
    odds: str
    selection_id: Optional[str] = None


class FeedMarket(BaseModel):
    market_type: str
    label: str
    selections: list[FeedSelection] = []


class FeedLeg(BaseModel):
    label: str
    market_label: Optional[str] = None
    selection_id: Optional[str] = None
    odds: float = 0.0


class FeedCard(BaseModel):
    id: str
    # Frontend uses card.hook_type primarily; card.badge is a fallback that
    # only matters when hook_type is missing AND badge == "news". We collapse
    # that fallback server-side so we don't have to ship the badge string.
    hook_type: Optional[str] = None
    headline: Optional[str] = None
    narrative_hook: str = ""
    source_name: Optional[str] = None
    ago_minutes: Optional[int] = None
    relevance_score: float = 0.0
    published_at: Optional[float] = None
    bet_type: str = "single"
    legs: list[FeedLeg] = []
    total_odds: Optional[float] = None
    virtual_selection: Optional[str] = None
    storyline_id: Optional[str] = None
    suspended: bool = False
    deep_link: Optional[str] = None
    game: FeedGame
    market: Optional[FeedMarket] = None


def _project_card(card: Card) -> dict:
    """Project a persisted Card down to the wire-only FeedCard shape, then
    dump to a plain dict ready for JSON serialization. None-valued optional
    fields are stripped — they cost bytes and the frontend treats absent and
    null identically (`card.foo || fallback`)."""
    g = card.game
    home = g.home_team
    away = g.away_team
    feed_game = FeedGame(
        id=g.id,
        home_team=FeedTeam(short_name=home.short_name, color=home.color),
        away_team=FeedTeam(short_name=away.short_name, color=away.color),
        broadcast=g.broadcast or "",
        start_time=g.start_time or "",
    )

    feed_market: Optional[FeedMarket] = None
    if card.market is not None:
        feed_market = FeedMarket(
            market_type=card.market.market_type,
            label=card.market.label,
            selections=[
                FeedSelection(
                    label=s.label,
                    odds=s.odds,
                    selection_id=s.selection_id,
                )
                for s in card.market.selections
            ],
        )

    feed_legs = [
        FeedLeg(
            label=l.label,
            market_label=l.market_label,
            selection_id=l.selection_id,
            odds=l.odds,
        )
        for l in card.legs
    ]

    # hook_type fallback — collapse the frontend's `card.badge === 'news'`
    # branch so we never need to ship the badge field on the wire. Anything
    # else (no hook_type, no news badge) becomes 'preview' — matches app.js.
    hook_type = card.hook_type
    if hook_type is None:
        badge_value = card.badge.value if card.badge is not None else None
        hook_type = "article" if badge_value == "news" else "preview"

    fc = FeedCard(
        id=card.id,
        hook_type=hook_type,
        headline=card.headline,
        narrative_hook=card.narrative_hook or "",
        source_name=card.source_name,
        ago_minutes=card.ago_minutes,
        relevance_score=card.relevance_score,
        published_at=card.published_at,
        bet_type=card.bet_type,
        legs=feed_legs,
        total_odds=card.total_odds,
        virtual_selection=card.virtual_selection,
        storyline_id=card.storyline_id,
        suspended=card.suspended,
        deep_link=card.deep_link,
        game=feed_game,
        market=feed_market,
    )
    return fc.model_dump(exclude_none=True)


def _project_card_dict(card_dict: dict) -> dict:
    """Same projection, but starting from an already-dumped dict (the
    fallback / live paths in feed_manager call .model_dump() before us).
    Re-hydrate to a Card so we go through one canonical projection path."""
    try:
        return _project_card(Card(**card_dict))
    except Exception:
        # If the dict is malformed for some reason, return it unchanged
        # rather than dropping the card — better a fat card than a missing
        # one. Logged so we can spot drift if it ever happens.
        logger.warning("[PULSE] feed projection failed; returning raw card")
        return card_dict


def create_routes(
    catalog: MarketCatalog,
    feed: FeedManager,
    simulator: GameSimulator,
) -> APIRouter:

    # HEAD support: free-tier uptime monitors (UptimeRobot etc.) default to
    # HEAD; without this they get 405 and false-alert.
    @router.api_route("/feed", methods=["GET", "HEAD"])
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
                return {"cards": [_project_card(c) for c in ranked]}
            except Exception as exc:
                logger.warning(
                    "[PULSE] feed_ranker failed, falling back to relevance "
                    "sort: %s", exc,
                )
                raw = feed.get_prematch_feed(sport=sport, limit=limit)
                return {"cards": [_project_card_dict(c) for c in raw]}
        else:
            raw = feed.get_live_feed(game_id=game_id, limit=limit)
            return {"cards": [_project_card_dict(c) for c in raw]}

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
