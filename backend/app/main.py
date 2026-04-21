"""PULSE POC — FastAPI application."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# Configure logging BEFORE any module-level loggers are created. Without this,
# our `logger.info(...)` calls go nowhere on Railway — only WARNING+ slips
# through Python's default lastResort handler. Keep it INFO so we can see
# pricing decisions in Railway log shipping.
logging.basicConfig(
    level=os.getenv("PULSE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

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
from app.services.featured_bb import fetch_and_build_featured_bb_cards
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
from app.engine.combo_builder import ComboBuilder
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


# Boot-time static version stamp. Every Railway restart bumps it, which
# invalidates browser caches of /static/app.js and /static/styles.css without
# needing manual cache-control headers on the static mount.
STATIC_VERSION = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
_INDEX_HTML = (STATIC_DIR / "index.html").read_text().replace("{{ VERSION }}", STATIC_VERSION)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(
        _INDEX_HTML,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ── Debug: live calculate_bets probe ────────────────────────────────────
# Temporary diagnostic endpoint while we verify the Betting API integration.
# Returns the raw Rogue calculate_bets response for any selection IDs.
# Disable in prod by unsetting PULSE_DEBUG_CALC_BETS once we have what we
# need (or leave on — it's read-only and uses the same JWT we already use).
@app.get("/debug/calc_bets")
async def debug_calc_bets(ids: str, oddsStyle: str = "decimal", locale: str = "en"):
    if os.getenv("PULSE_DEBUG_CALC_BETS", "true").lower() != "true":
        raise HTTPException(403, "PULSE_DEBUG_CALC_BETS=false")
    if not ROGUE_CONFIG_JWT:
        raise HTTPException(503, "ROGUE_CONFIG_JWT not set")
    selection_ids = [s.strip() for s in ids.split(",") if s.strip()]
    if not selection_ids:
        raise HTTPException(400, "ids query param required (comma-separated)")
    client = RogueClient(
        base_url=ROGUE_BASE_URL,
        config_jwt=ROGUE_CONFIG_JWT,
        per_second=ROGUE_RATE_LIMIT_PER_SECOND,
    )
    try:
        result = await client.calculate_bets(
            selection_ids, odds_style=oddsStyle, locale=locale,
        )
        return JSONResponse({"selection_ids": selection_ids, "result": result})
    except Exception as exc:
        return JSONResponse(
            {"selection_ids": selection_ids, "error": str(exc), "type": type(exc).__name__},
            status_code=500,
        )
    finally:
        await client.close()


# ── Debug: probe featured BBs ───────────────────────────────────────────
# Inspect the /v1/featured/betbuilder response shape so we can design the
# integration. Same gating as /debug/calc_bets.
@app.get("/debug/featured_bb")
async def debug_featured_bb(locale: str = "en"):
    if os.getenv("PULSE_DEBUG_CALC_BETS", "true").lower() != "true":
        raise HTTPException(403, "PULSE_DEBUG_CALC_BETS=false")
    if not ROGUE_CONFIG_JWT:
        raise HTTPException(503, "ROGUE_CONFIG_JWT not set")
    client = RogueClient(
        base_url=ROGUE_BASE_URL, config_jwt=ROGUE_CONFIG_JWT,
        per_second=ROGUE_RATE_LIMIT_PER_SECOND,
    )
    try:
        result = await client.featured_betbuilders(locale=locale)
        return JSONResponse({"locale": locale, "result": result})
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc), "type": type(exc).__name__}, status_code=500,
        )
    finally:
        await client.close()


@app.get("/debug/boosted")
async def debug_boosted(locale: str = "en"):
    if os.getenv("PULSE_DEBUG_CALC_BETS", "true").lower() != "true":
        raise HTTPException(403, "PULSE_DEBUG_CALC_BETS=false")
    if not ROGUE_CONFIG_JWT:
        raise HTTPException(503, "ROGUE_CONFIG_JWT not set")
    client = RogueClient(
        base_url=ROGUE_BASE_URL, config_jwt=ROGUE_CONFIG_JWT,
        per_second=ROGUE_RATE_LIMIT_PER_SECOND,
    )
    try:
        result = await client.featured_boosted_selections(locale=locale)
        return JSONResponse({"locale": locale, "result": result})
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc), "type": type(exc).__name__}, status_code=500,
        )
    finally:
        await client.close()


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
_rerun_task: _Optional["asyncio.Task"] = None  # noqa: F821


@app.on_event("startup")
async def generate_prematch_cards():
    """Generate pre-match cards.

    Data-source controlled by PULSE_DATA_SOURCE env var:
      - "mock" (default): hand-crafted LAL/ARS/KC demo cards from local JSON.
      - "rogue": real pre-match soccer fixtures pulled from the Rogue API.
    """
    global _rerun_task
    if PULSE_DATA_SOURCE == "rogue":
        await _load_rogue_prematch()
        # Kick off the periodic rerun loop AFTER initial load completes.
        # The loop sleeps first, so it doesn't compete with the boot pass.
        import asyncio as _asyncio
        _rerun_task = _asyncio.create_task(_scheduled_rerun_loop())
        return
    await _load_mock_prematch()


# In-flight on-demand rerun state. Guarded so /admin/rerun can't kick off
# parallel runs. While one is running, the endpoint returns 409.
_ondemand_rerun_inflight: bool = False
_ondemand_rerun_started_at: float = 0.0
_ondemand_rerun_last_result: dict = {}


async def _do_ondemand_rerun():
    """Background task body for /admin/rerun. Runs the same staging-and-
    swap logic as the scheduled loop but is fire-and-forget so the HTTP
    request can return immediately (Railway's edge cuts at 60s, and a
    rerun takes ~3 min)."""
    global _ondemand_rerun_inflight, _ondemand_rerun_last_result
    t0 = time.time()
    try:
        staging = FeedManager()
        await _load_rogue_prematch(target_feed=staging, is_rerun=True)
        new_cards = list(staging.prematch_cards)
        if not new_cards:
            _ondemand_rerun_last_result = {
                "ok": False, "error": "rerun produced 0 cards — kept prior feed",
                "elapsed_s": round(time.time() - t0, 1),
                "finished_at": time.time(),
            }
            logger.warning("[PULSE] On-demand rerun produced 0 cards")
            return
        feed.replace_prematch_cards(new_cards)
        try:
            await feed.broadcast_feed_refresh()
        except Exception as exc:
            logger.warning("[PULSE] feed_refresh broadcast errored: %s", exc)
        elapsed = time.time() - t0
        _ondemand_rerun_last_result = {
            "ok": True, "cards": len(new_cards),
            "elapsed_s": round(elapsed, 1),
            "finished_at": time.time(),
        }
        logger.info(
            "[PULSE] On-demand rerun complete: %d cards swapped in (%.1fs)",
            len(new_cards), elapsed,
        )
    except Exception as exc:
        logger.exception("[PULSE] On-demand rerun failed: %s", exc)
        _ondemand_rerun_last_result = {
            "ok": False, "error": str(exc),
            "elapsed_s": round(time.time() - t0, 1),
            "finished_at": time.time(),
        }
    finally:
        _ondemand_rerun_inflight = False


@app.post("/admin/rerun")
async def admin_rerun():
    """On-demand candidate-engine rerun for demos. Fires-and-forgets so
    the HTTP response returns immediately (rerun takes ~3 min; Railway's
    edge cuts at 60s). Status of the most recent run is available at
    GET /admin/rerun/status. Frontend clients listen on the existing
    `/ws/feed` socket for `{type:"feed_refresh"}` to know when to re-pull.

    Guarded against parallel invocations — second call while one is in
    flight returns 409.

    No auth on the endpoint today (POC). Cost: same ~$0.30-0.50 per run
    as the scheduled loop.
    """
    global _ondemand_rerun_inflight, _ondemand_rerun_started_at
    if PULSE_DATA_SOURCE != "rogue":
        raise HTTPException(400, "rerun only valid when PULSE_DATA_SOURCE=rogue")
    if _ondemand_rerun_inflight:
        raise HTTPException(
            409,
            f"a rerun is already in flight (started {int(time.time() - _ondemand_rerun_started_at)}s ago)",
        )
    _ondemand_rerun_inflight = True
    _ondemand_rerun_started_at = time.time()
    import asyncio as _asyncio
    _asyncio.create_task(_do_ondemand_rerun())
    return {
        "ok": True,
        "status": "started",
        "estimated_seconds": 180,
        "poll": "/admin/rerun/status",
        "ws_event": "feed_refresh",
    }


@app.get("/admin/rerun/status")
async def admin_rerun_status():
    return {
        "in_flight": _ondemand_rerun_inflight,
        "started_at": _ondemand_rerun_started_at if _ondemand_rerun_inflight else None,
        "running_for_seconds": (
            round(time.time() - _ondemand_rerun_started_at, 1)
            if _ondemand_rerun_inflight else None
        ),
        "last_result": _ondemand_rerun_last_result or None,
    }


async def _load_rogue_prematch(
    target_feed: _Optional[FeedManager] = None,
    *,
    is_rerun: bool = False,
) -> None:
    """Build the Rogue-sourced pre-match feed.

    target_feed: which FeedManager to populate. Defaults to the live `feed`
    singleton (startup case). The scheduled rerun loop passes a fresh
    staging FeedManager and atomically swaps it into the live one once
    fully built — so rerunning never leaves the visible feed empty.

    is_rerun: when True, expires all currently-published candidates BEFORE
    re-running the engine. Without this, each rerun's freshly-generated
    candidates stack on top of prior runs (publish loop reads ALL
    status='published' rows, not just the latest cycle).
    """
    if target_feed is None:
        target_feed = feed
    if is_rerun:
        # Expire prior batch so the publish loop only sees this cycle's
        # candidates. Historical rows kept as status=EXPIRED for admin
        # visibility.
        try:
            n_expired = await candidate_store.expire_published_candidates()
            logger.info("[PULSE] Rerun: expired %d prior published candidates", n_expired)
        except Exception as exc:
            logger.warning("[PULSE] Rerun: expire prior candidates failed: %s", exc)
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

        if not games:
            logger.warning("[PULSE] Rogue returned no usable fixtures — falling back to mock.")
            await _load_mock_prematch()
            return

        catalog.replace_all(markets)
        simulator._games = {g.id: g for g in games}

        # Baseline pre-match cards (1X2 per fixture). The candidate engine
        # below overlays news-driven cards on top.
        cards = build_prematch_cards(games, catalog, assembler)
        for card in cards:
            target_feed.add_prematch_card(card)

        # Real correlated BB + combo prices come from the same RogueClient
        # via POST /v1/betting/calculateBets — no separate service or auth.
        await _run_candidate_engine(
            simulator._games,
            rogue_client=client,
            target_feed=target_feed,
        )

        # Surface operator-curated featured BBs (Apuesta Total's
        # /v1/featured/betbuilder picks). Each is priced via calculate_bets
        # and rendered as a self-contained Card — bypasses candidate_store
        # and MarketCatalog because the operator picks markets we don't
        # whitelist (Goalscorer, Corners O/U etc.). Wrapped in a try so any
        # failure (schema drift, transient 5xx) can't take down the whole
        # startup: the rest of the feed is already populated.
        if os.getenv("PULSE_FEATURED_BB_ENABLED", "true").lower() == "true":
            try:
                featured_max = int(os.getenv("PULSE_FEATURED_BB_MAX", "6"))
            except ValueError:
                featured_max = 6
            try:
                featured_cards = await fetch_and_build_featured_bb_cards(
                    client, simulator._games, max_count=featured_max,
                )
                for c in featured_cards:
                    target_feed.add_prematch_card(c)
                logger.info(
                    "[PULSE] Featured BBs added to feed: %d (out of %d max)",
                    len(featured_cards), featured_max,
                )
            except Exception as exc:
                logger.exception("[PULSE] Featured BB step failed: %s", exc)
    finally:
        await client.close()

    logger.info("[PULSE] Rogue mode — %d games, %d markets, %d cards",
                len(games), len(markets), len(target_feed.prematch_cards))


# ── Scheduled rerun loop ────────────────────────────────────────────────
#
# Rebuilds the entire Rogue-sourced pre-match feed every
# PULSE_RERUN_INTERVAL_SECONDS (default 4h, matching the news cache TTL of
# 6h so most fixtures should hit the cache on rerun). Builds into a
# staging FeedManager and atomic-swaps once complete — visible feed never
# goes empty during a rerun.
#
# Cost:
#   ~$0.30-0.50 per run (Haiku scout × ~8 fixtures + Sonnet rewrite × ~25
#   candidates). At 4h cadence: 6 runs/day = ~$2-3/day. Cache hits cut
#   this further, but Railway's ephemeral fs wipes the cache on every
#   redeploy — moving cache to a persistent volume is a follow-up.
#
# Levers:
#   PULSE_RERUN_ENABLED (default true)            — kill switch
#   PULSE_RERUN_INTERVAL_SECONDS (default 14400)  — 4h
#   PULSE_NEWS_MAX_FIXTURES (default 12)          — fewer fixtures = cheaper
#   PULSE_NEWS_CACHE_TTL_HOURS (default 6)        — longer cache = cheaper
async def _scheduled_rerun_loop():
    if PULSE_DATA_SOURCE != "rogue":
        return
    if os.getenv("PULSE_RERUN_ENABLED", "true").lower() != "true":
        logger.info("[PULSE] Scheduled rerun disabled (PULSE_RERUN_ENABLED=false)")
        return
    try:
        interval_s = int(os.getenv("PULSE_RERUN_INTERVAL_SECONDS", "14400"))
    except ValueError:
        interval_s = 14400
    interval_s = max(60, interval_s)  # safety floor 1 min
    logger.info(
        "[PULSE] Scheduled rerun loop active — every %ds (~%.1fh)",
        interval_s, interval_s / 3600,
    )
    import asyncio as _asyncio
    while True:
        try:
            await _asyncio.sleep(interval_s)
        except _asyncio.CancelledError:
            return
        t0 = time.time()
        logger.info("[PULSE] Scheduled rerun starting…")
        # Build into a fresh staging FeedManager — atomic swap at end means
        # the visible feed never goes empty.
        staging = FeedManager()
        try:
            await _load_rogue_prematch(target_feed=staging, is_rerun=True)
        except Exception as exc:
            logger.exception("[PULSE] Scheduled rerun failed (keeping prior feed): %s", exc)
            continue
        new_cards = list(staging.prematch_cards)
        if not new_cards:
            logger.warning("[PULSE] Scheduled rerun produced 0 cards — keeping prior feed")
            continue
        feed.replace_prematch_cards(new_cards)
        try:
            await feed.broadcast_feed_refresh()
        except Exception as exc:
            logger.warning("[PULSE] feed_refresh broadcast errored: %s", exc)
        elapsed = time.time() - t0
        logger.info(
            "[PULSE] Scheduled rerun complete: %d cards swapped in (%.1fs)",
            len(new_cards), elapsed,
        )


async def _run_candidate_engine(
    games_by_id: dict[str, Game],
    *,
    rogue_client: _Optional[RogueClient] = None,
    target_feed: _Optional[FeedManager] = None,
):
    """Run the news-driven engine across the live catalogue.

    Uses MockNewsIngester by default (no API key). When ANTHROPIC_API_KEY is
    set, we can swap in the real NewsIngester — imported lazily so missing
    SDK deps don't break the mock path. When a Rogue client is passed in,
    the ComboBuilder uses it for Bet Builder validation AND for real
    correlated pricing via POST /v1/betting/calculateBets.

    target_feed: which FeedManager to publish into. Defaults to live `feed`;
    scheduled rerun loop passes a staging FeedManager.
    """
    if target_feed is None:
        target_feed = feed
    global candidate_engine
    await candidate_store.init()

    rewriter = None
    if ANTHROPIC_API_KEY:
        try:
            from anthropic import AsyncAnthropic
            from app.services.news_ingester import NewsIngester
            from app.engine.narrative_rewriter import NarrativeRewriter

            anth = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            ingester = NewsIngester(
                client=anth,
                store=candidate_store,
                model=PULSE_NEWS_MODEL,
                max_searches=PULSE_NEWS_MAX_SEARCHES,
                cache_ttl_seconds=PULSE_NEWS_CACHE_TTL_HOURS * 3600,
            )
            # Copywriter pass — rewrites scout output into journalist voice
            # before the card hits the feed. Sonnet by default; override via env.
            rewriter = NarrativeRewriter(anth, model=os.getenv("PULSE_REWRITER_MODEL", "claude-sonnet-4-6"))
            logger.info("[PULSE] Candidate engine: scout=%s, rewriter=%s",
                        PULSE_NEWS_MODEL, os.getenv("PULSE_REWRITER_MODEL", "claude-sonnet-4-6"))
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
    # Bet Builder generator — only active when we have a Rogue client.
    # The same client provides real correlated BB pricing via the Betting
    # API (calculate_bets). Mock mode (no Rogue client) skips BBs entirely.
    combo_builder = ComboBuilder(catalog, rogue_client) if rogue_client is not None else None

    candidate_engine = CandidateEngine(
        ingester=ingester, resolver=resolver, builder=builder,
        scorer=scorer, policy=policy, store=candidate_store,
        combo_builder=combo_builder,
    )

    counts = await candidate_engine.run_once(
        games_by_id, max_fixtures=PULSE_NEWS_MAX_FIXTURES,
    )
    logger.info("[PULSE] Candidate engine counts: %s", counts)

    # Cross-event combo pricing — when (in a future stage) the engine starts
    # emitting BetType.COMBO candidates, fetch the operator-boosted price via
    # Rogue's calculate_bets endpoint. Today this is a no-op because nothing
    # produces COMBO candidates yet; the wiring is here so Stage 3d
    # ("Goalscorers of the Day" cross-event accumulators) gets real prices
    # the moment it ships. Combo math = `Bets[Type=='Combo'].TrueOdds`, which
    # already includes the operator's `ComboBonus.Percent` boost.
    if rogue_client is not None:
        from app.models.news import BetType as _BTypeForCombo
        all_pending = await candidate_store.list_candidates(limit=500)
        combo_priced = 0
        for c in all_pending:
            if c.bet_type != _BTypeForCombo.COMBO:
                continue
            if c.price_source == "rogue_calculate_bets":
                continue
            if not c.selection_ids or len(c.selection_ids) < 2:
                continue
            try:
                quote = await rogue_client.calculate_bets(c.selection_ids)
            except Exception as exc:
                logger.warning("[PULSE] calculate_bets errored for combo %s: %s", c.id, exc)
                continue
            if isinstance(quote, dict):
                bets = quote.get("Bets") or []
                combo_bet = next(
                    (b for b in bets if (b or {}).get("Type") == "Combo"),
                    None,
                )
                if combo_bet and isinstance(combo_bet.get("TrueOdds"), (int, float)):
                    c.total_odds = round(float(combo_bet["TrueOdds"]), 2)
                    c.price_source = "rogue_calculate_bets"
                    combo_priced += 1
        if combo_priced:
            await candidate_store.save_candidates([c for c in all_pending if c.price_source == "rogue_calculate_bets"])
            logger.info("[PULSE] calculate_bets combo pricing applied to %d candidates", combo_priced)

    # Promote published candidates into the public feed
    published = await candidate_store.list_candidates(
        above_threshold_only=True, status="published", limit=100,
    )
    from app.models.news import BetType as _BetType, CandidateStatus as _CandidateStatus
    from app.models.schemas import CardLeg as _CardLeg
    from app.engine.quality_gates import apply_gates as _apply_gates
    import time as _time
    rewrite_hit = 0
    rewrite_miss = 0
    gate_rejected = 0
    gate_reject_reasons: dict[str, int] = {}
    gated_updates: list = []
    for cand in published:
        game = games_by_id.get(cand.game_id)
        if game is None:
            continue
        market_id = cand.market_ids[0] if cand.market_ids else None
        market = catalog.get(market_id) if market_id else None
        if market is None:
            continue
        news = await candidate_store.get_news_item(cand.news_item_id) if cand.news_item_id else None

        # Resolve leg markets for Bet Builder candidates.
        is_bb = cand.bet_type == _BetType.BET_BUILDER and len(cand.selection_ids) >= 2
        legs: list[_CardLeg] = []
        total_odds: "float | None" = None
        if is_bb:
            for mid, sid in zip(cand.market_ids, cand.selection_ids):
                leg_market = catalog.get(mid)
                if leg_market is None:
                    continue
                leg_sel = next((s for s in leg_market.selections if s.selection_id == sid), None)
                if leg_sel is None:
                    continue
                try:
                    leg_odds = float(leg_sel.odds)
                except Exception:
                    leg_odds = 0.0
                legs.append(_CardLeg(
                    label=leg_sel.label,
                    market_label=leg_market.label,
                    odds=leg_odds,
                    selection_id=sid,
                ))
            if legs:
                # Prefer the price ComboBuilder stored on the candidate (real
                # correlated BB price from Kmianko, or naive product as a
                # last resort). Re-compute naive only if the candidate has no
                # stored total — keeps the gate working in old/mock paths.
                if cand.total_odds is not None:
                    total_odds = round(float(cand.total_odds), 2)
                else:
                    product = 1.0
                    for leg in legs:
                        if leg.odds > 0:
                            product *= leg.odds
                    total_odds = round(product, 2) if product > 1.0 else None
            else:
                is_bb = False  # couldn't resolve any leg → fall back to single

        # Journalist voice pass — replace the scout's raw headline + summary
        # with card-ready copy. Falls back to scout output on failure.
        final_headline = news.headline if news else (cand.narrative or "")
        final_angle = news.summary if news else (cand.narrative or "")
        if rewriter is not None and news is not None:
            # Pass total_odds to the rewriter ONLY when we have a real price
            # from Rogue's calculate_bets (rogue_calculate_bets) or — for
            # back-compat with anything still in the candidate store — the
            # legacy kmianko sources. Naive products are misleading; the LLM
            # is instructed to stay vague when total_odds is absent.
            rewriter_total: "float | None" = None
            if cand.price_source in ("rogue_calculate_bets", "kmianko_bb", "kmianko_combo") and total_odds:
                rewriter_total = total_odds
            rewrite = await rewriter.rewrite(
                news=news, market=market, game=game, candidate=cand,
                legs=legs if is_bb else None, total_odds=rewriter_total,
            )
            if rewrite:
                final_headline = rewrite.get("headline") or final_headline
                final_angle = rewrite.get("angle") or final_angle
                rewrite_hit += 1
            else:
                rewrite_miss += 1

        # Quality gate — fail-closed rules that drop bad candidates before
        # they hit the public feed. Rejected candidates stay in the store
        # with status=rejected + reason so the /admin table can surface
        # them for tuning.
        passes, gate_reason = _apply_gates(
            cand,
            headline=final_headline or "",
            angle=final_angle or "",
            game=game,
            legs=legs if is_bb else None,
            total_odds=total_odds if is_bb else None,
        )
        if not passes:
            gate_rejected += 1
            gate_reject_reasons[gate_reason or "unknown"] = gate_reject_reasons.get(gate_reason or "unknown", 0) + 1
            cand.status = _CandidateStatus.REJECTED
            cand.threshold_passed = False
            cand.reason = (cand.reason + " | gate: " + (gate_reason or "")).strip(" |")
            gated_updates.append(cand)
            continue

        badge = {
            "injury": BadgeType.NEWS, "team_news": BadgeType.NEWS,
            "transfer": BadgeType.NEWS, "manager_quote": BadgeType.NEWS,
            "tactical": BadgeType.TRENDING, "preview": BadgeType.TRENDING,
            "article": BadgeType.NEWS,
        }.get(cand.hook_type.value, BadgeType.TRENDING)
        card = assembler.assemble_prematch(
            game=game, market=market, narrative=final_angle or "",
            badge=badge, relevance=cand.score, stats=[], tweets=[],
        )
        card.hook_type = cand.hook_type.value
        card.headline = final_headline
        card.source_name = news.source_name if news else None
        ingested = news.ingested_at if news else cand.created_at
        if ingested:
            card.ago_minutes = max(0, int((_time.time() - ingested) / 60))
        if is_bb:
            card.legs = legs
            # Show the real correlated price when ComboBuilder fetched one
            # via Rogue calculate_bets. Hide it when we only have the naive
            # leg-product (over-states the correlated book price by ~1-2x).
            # The frontend renders "Price in bet slip" when total_odds is
            # null. The naive total is still used upstream for quality
            # gating regardless of whether it surfaces on the card.
            if cand.price_source in ("rogue_calculate_bets", "kmianko_bb"):
                card.total_odds = total_odds
            else:
                card.total_odds = None
            card.bet_type = "bet_builder"
        target_feed.add_prematch_card(card)

    if rewriter is not None:
        logger.info("[PULSE] NarrativeRewriter: %d hits, %d misses (fell back to scout copy)",
                    rewrite_hit, rewrite_miss)
    if gate_rejected:
        # Persist the REJECTED status change so the admin table reflects
        # what got blocked and why.
        await candidate_store.save_candidates(gated_updates)
        logger.info("[PULSE] Quality gates rejected %d candidates — reasons: %s",
                    gate_rejected, gate_reject_reasons)


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
