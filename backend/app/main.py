"""PULSE POC — FastAPI application."""

import json
import logging
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import (
    ANTHROPIC_API_KEY,
    PULSE_DATA_SOURCE,
    PULSE_DB_PATH,
    PULSE_NEWS_CACHE_TTL_HOURS,
    PULSE_NEWS_MAX_FIXTURES,
    PULSE_NEWS_MAX_SEARCHES,
    PULSE_NEWS_MODEL,
    PULSE_PUBLISH_THRESHOLD,
    ROGUE_BASE_URL,
    ROGUE_CATALOGUE_DAYS_AHEAD,
    ROGUE_CATALOGUE_MAX_EVENTS,
    ROGUE_CONFIG_JWT,
    ROGUE_RATE_LIMIT_PER_SECOND,
    ROGUE_SOCCER_SPORT_ID,
)
from app.models.schemas import (
    Game, Tweet, StatDisplay, ProgressDisplay, BadgeType, Card, CardType,
)
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.services.game_simulator import GameSimulator
from app.services.rogue_client import RogueClient
from app.services.catalogue_loader import fetch_soccer_snapshot
from app.services.rogue_prematch import build_prematch_cards
from app.services.candidate_store import CandidateStore
from app.services.mock_news_ingester import MockNewsIngester
from app.services.candidate_engine import CandidateEngine
from app.engine.event_detector import EventDetector
from app.engine.entity_resolver import EntityResolver
from app.engine.market_matcher import MarketMatcher
from app.engine.relevance_scorer import RelevanceScorer
from app.engine.narrative_generator import NarrativeGenerator
from app.engine.card_assembler import CardAssembler
from app.engine.news_entity_resolver import NewsEntityResolver
from app.engine.candidate_builder import CandidateBuilder
from app.engine.news_scorer import NewsScorer, PolicyLayer
from app.api.routes import create_routes
from app.api.admin import create_admin_routes

logger = logging.getLogger("pulse")
DATA_DIR = Path(__file__).parent / "data"

# ── Initialize services ──
catalog = MarketCatalog()
feed = FeedManager()
detector = EventDetector()
resolver = EntityResolver()
matcher = MarketMatcher(catalog)
scorer = RelevanceScorer()
narrator = NarrativeGenerator()
assembler = CardAssembler()

simulator = GameSimulator(
    catalog=catalog, feed=feed, detector=detector,
    matcher=matcher, scorer=scorer, narrator=narrator,
    assembler=assembler,
)

# ── Create app ──
app = FastAPI(title="PULSE POC", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Candidate engine plumbing ──
from typing import Optional as _Optional
candidate_store = CandidateStore(PULSE_DB_PATH)
candidate_engine: _Optional[CandidateEngine] = None  # initialised in startup

# ── Mount routes ──
router = create_routes(catalog, feed, simulator)
app.include_router(router)
admin_router = create_admin_routes(candidate_store, catalog, simulator)
app.include_router(admin_router)

# ── Static files ──
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ── WebSocket ──
@app.websocket("/ws/feed")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    feed.register_ws(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        feed.unregister_ws(websocket)


# ── Generate pre-match cards on startup ──
@app.on_event("startup")
async def generate_prematch_cards():
    """Generate pre-match cards.

    Data-source controlled by PULSE_DATA_SOURCE env var:
      - "mock" (default): hand-crafted LAL/ARS/KC demo cards from local JSON.
      - "rogue": real pre-match soccer fixtures pulled from the Rogue API.
    """
    if PULSE_DATA_SOURCE == "rogue":
        await _load_rogue_prematch()
        return
    await _load_mock_prematch()


async def _load_rogue_prematch():
    if not ROGUE_CONFIG_JWT:
        logger.warning(
            "[PULSE] PULSE_DATA_SOURCE=rogue but ROGUE_CONFIG_JWT is empty — falling back to mock."
        )
        await _load_mock_prematch()
        return

    logger.info("[PULSE] Loading Rogue soccer catalogue (days=%s, max=%s)",
                ROGUE_CATALOGUE_DAYS_AHEAD, ROGUE_CATALOGUE_MAX_EVENTS)

    client = RogueClient(
        base_url=ROGUE_BASE_URL,
        config_jwt=ROGUE_CONFIG_JWT,
        per_second=ROGUE_RATE_LIMIT_PER_SECOND,
    )
    try:
        games, markets, _ = await fetch_soccer_snapshot(
            client,
            sport_id=ROGUE_SOCCER_SPORT_ID,
            days_ahead=ROGUE_CATALOGUE_DAYS_AHEAD,
            max_events=ROGUE_CATALOGUE_MAX_EVENTS,
        )
    finally:
        await client.close()

    if not games:
        logger.warning("[PULSE] Rogue returned no usable fixtures — falling back to mock.")
        await _load_mock_prematch()
        return

    catalog.replace_all(markets)
    # Expose the real fixtures to the rest of the app by registering them
    # on the simulator's game registry — the API routes read from there.
    simulator._games = {g.id: g for g in games}

    # Baseline pre-match cards (1X2 per fixture). The candidate engine below
    # overlays news-driven cards on top.
    cards = build_prematch_cards(games, catalog, assembler)
    for card in cards:
        feed.add_prematch_card(card)

    await _run_candidate_engine(simulator._games)

    logger.info("[PULSE] Rogue mode — %d games, %d markets, %d cards",
                len(games), len(markets), len(feed.prematch_cards))


async def _run_candidate_engine(games_by_id: dict[str, Game]):
    """Run the news-driven engine across the live catalogue.

    Uses MockNewsIngester by default (no API key). When ANTHROPIC_API_KEY is
    set, we can swap in the real NewsIngester — imported lazily so missing
    SDK deps don't break the mock path.
    """
    global candidate_engine
    await candidate_store.init()

    if ANTHROPIC_API_KEY:
        try:
            from anthropic import AsyncAnthropic
            from app.services.news_ingester import NewsIngester

            anth = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            ingester = NewsIngester(
                client=anth,
                store=candidate_store,
                model=PULSE_NEWS_MODEL,
                max_searches=PULSE_NEWS_MAX_SEARCHES,
                cache_ttl_seconds=PULSE_NEWS_CACHE_TTL_HOURS * 3600,
            )
            logger.info("[PULSE] Candidate engine using real LLM ingester (%s)", PULSE_NEWS_MODEL)
        except Exception as exc:
            logger.warning("[PULSE] LLM ingester unavailable (%s) — using mock", exc)
            ingester = MockNewsIngester(candidate_store)
    else:
        ingester = MockNewsIngester(candidate_store)
        logger.info("[PULSE] Candidate engine using mock news (no ANTHROPIC_API_KEY)")

    resolver = NewsEntityResolver(games_by_id)
    builder = CandidateBuilder(catalog)
    scorer = NewsScorer()
    policy = PolicyLayer(publish_threshold=PULSE_PUBLISH_THRESHOLD)

    candidate_engine = CandidateEngine(
        ingester=ingester, resolver=resolver, builder=builder,
        scorer=scorer, policy=policy, store=candidate_store,
    )

    counts = await candidate_engine.run_once(
        games_by_id, max_fixtures=PULSE_NEWS_MAX_FIXTURES,
    )
    logger.info("[PULSE] Candidate engine counts: %s", counts)

    # Promote published candidates into the public feed
    published = await candidate_store.list_candidates(
        above_threshold_only=True, status="published", limit=100,
    )
    import time as _time
    for cand in published:
        game = games_by_id.get(cand.game_id)
        if game is None:
            continue
        market_id = cand.market_ids[0] if cand.market_ids else None
        market = catalog.get(market_id) if market_id else None
        if market is None:
            continue
        news = await candidate_store.get_news_item(cand.news_item_id) if cand.news_item_id else None
        narrative = (news.summary if news else cand.narrative) or (news.headline if news else "")
        badge = {
            "injury": BadgeType.NEWS, "team_news": BadgeType.NEWS,
            "transfer": BadgeType.NEWS, "manager_quote": BadgeType.NEWS,
            "tactical": BadgeType.TRENDING, "preview": BadgeType.TRENDING,
            "article": BadgeType.NEWS,
        }.get(cand.hook_type.value, BadgeType.TRENDING)
        card = assembler.assemble_prematch(
            game=game, market=market, narrative=narrative or "",
            badge=badge, relevance=cand.score, stats=[], tweets=[],
        )
        # Design-handoff fields so the Hero variant has source / recency / hook
        card.hook_type = cand.hook_type.value
        card.headline = news.headline if news else (cand.narrative or "")
        card.source_name = news.source_name if news else None
        # Rough ago: ingestion time vs now (published_at often missing from LLM output)
        ingested = news.ingested_at if news else cand.created_at
        if ingested:
            card.ago_minutes = max(0, int((_time.time() - ingested) / 60))
        feed.add_prematch_card(card)


async def _load_mock_prematch():
    games_raw = json.loads((DATA_DIR / "mock_games.json").read_text())
    tweets_raw = json.loads((DATA_DIR / "mock_tweets.json").read_text())
    players_raw = json.loads((DATA_DIR / "mock_players.json").read_text())

    games = {g["id"]: Game(**g) for g in games_raw}
    tweets = [Tweet(**t) for t in tweets_raw]
    players = {p["id"]: p for p in players_raw}

    # ── Card 1: LeBron milestone (LAL vs BOS) ──
    game = games["game_lal_bos"]
    market = catalog.get("mkt_lebron_pts")
    lebron = players["lebron"]
    relevant_tweets = [t for t in tweets if "lebron" in t.player_ids and t.time_ago in ("2h ago", "45m")]

    feed.add_prematch_card(assembler.assemble_prematch(
        game=game,
        market=market,
        narrative=f"LeBron is 12 points away from breaking the all-time NBA scoring record tonight",
        badge=BadgeType.MILESTONE,
        relevance=0.96,
        stats=[
            StatDisplay(label="Avg Last 5", value="28.4", color="green"),
            StatDisplay(label="Pts Needed", value="12"),
            StatDisplay(label="Prob. Tonight", value="93%", color="accent"),
        ],
        progress=ProgressDisplay(
            label="Career Points",
            current=38376,
            target=38388,
            fill_color="accent",
        ),
        tweets=[t for t in tweets if t.id == "tw1"],
    ))

    # ── Card 2: Arsenal vs Chelsea (form + Saka return) ──
    game = games["game_ars_che"]
    market = catalog.get("mkt_ars_che_result")
    relevant_tweets = [t for t in tweets if "ars" in t.team_ids and t.time_ago == "1h"]

    feed.add_prematch_card(assembler.assemble_prematch(
        game=game,
        market=market,
        narrative="Arsenal are unbeaten in 14 straight home matches — Chelsea haven't won at the Emirates since 2021",
        badge=BadgeType.TRENDING,
        relevance=0.88,
        stats=[
            StatDisplay(label="ARS Form (L5)", value="W4 D1", color="green"),
            StatDisplay(label="ARS xG/Game", value="2.31"),
            StatDisplay(label="CHE xG/Game", value="1.64"),
        ],
        tweets=[t for t in tweets if t.id == "tw7"],
    ))

    # ── Card 3: Chiefs vs Eagles (Mahomes return) ──
    game = games["game_kc_phi"]
    market = catalog.get("mkt_kc_phi_spread")

    feed.add_prematch_card(assembler.assemble_prematch(
        game=game,
        market=market,
        narrative="Mahomes practiced in full today for the first time in 3 weeks — line has shifted 2.5 points since Monday",
        badge=BadgeType.NEWS,
        relevance=0.91,
        stats=[
            StatDisplay(label="Open Line", value="KC -1.5", color="orange"),
            StatDisplay(label="Current", value="KC -4.0", color="green"),
        ],
        tweets=[t for t in tweets if t.id == "tw6"],
    ))

    # ── Card 4: Saka goalscorer prop ──
    game = games["game_ars_che"]
    market = catalog.get("mkt_saka_goal")

    feed.add_prematch_card(assembler.assemble_prematch(
        game=game,
        market=market,
        narrative="Saka returns to full training ahead of London derby — his first match back could be explosive",
        badge=BadgeType.NEWS,
        relevance=0.82,
        stats=[
            StatDisplay(label="Goals This Season", value="14", color="green"),
            StatDisplay(label="Assists", value="11"),
            StatDisplay(label="Matches", value="28"),
        ],
        tweets=[],
    ))

    print(f"[PULSE] Generated {len(feed.prematch_cards)} pre-match cards")


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True)
else:
    # When imported by uvicorn directly, ensure PORT is respected
    import os
    _port = os.getenv("PORT")
    if _port:
        import logging
        logging.getLogger("uvicorn").info(f"PORT env var detected: {_port}")
