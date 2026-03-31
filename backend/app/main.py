"""PULSE POC — FastAPI application."""

import json
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.models.schemas import (
    Game, Tweet, StatDisplay, ProgressDisplay, BadgeType, Card, CardType,
)
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.services.game_simulator import GameSimulator
from app.engine.event_detector import EventDetector
from app.engine.entity_resolver import EntityResolver
from app.engine.market_matcher import MarketMatcher
from app.engine.relevance_scorer import RelevanceScorer
from app.engine.narrative_generator import NarrativeGenerator
from app.engine.card_assembler import CardAssembler
from app.api.routes import create_routes

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

# ── Mount routes ──
router = create_routes(catalog, feed, simulator)
app.include_router(router)

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
    """Generate pre-match context cards for tonight's games."""
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
