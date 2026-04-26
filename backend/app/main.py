"""PULSE POC — FastAPI application."""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

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
    PULSE_HOOK_BET_TYPE_PREFERENCE_JSON,
    PULSE_NEWS_CACHE_TTL_HOURS,
    PULSE_NEWS_INGEST_ENABLED,
    PULSE_NEWS_MAX_FIXTURES,
    PULSE_NEWS_MAX_SEARCHES,
    PULSE_NEWS_MODEL,
    PULSE_PUBLISH_THRESHOLD,
    PULSE_STORYLINE_EUROPE_CHASE_ENABLED,
    PULSE_STORYLINE_GOLDEN_BOOT_ENABLED,
    PULSE_STORYLINE_MIN_PARTICIPANTS,
    PULSE_STORYLINE_RELEGATION_ENABLED,
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
from app.services.catalogue_loader import GOALSCORER_DEFAULT_TOP_N, fetch_soccer_snapshot
from app.services.rogue_prematch import build_prematch_cards
from app.services.featured_bb import fetch_and_build_featured_bb_cards
from app.services.sse_pricing import SSEPricingManager
from app.services.candidate_store import CandidateStore
from app.services.mock_news_ingester import MockNewsIngester
from app.services.candidate_engine import CandidateEngine
from app.services.kmianko_slip_minter import KmiankoSlipMinter
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
from app.api.reactions import create_reactions_routes

logger = logging.getLogger("pulse")
DATA_DIR = Path(__file__).parent / "data"


# ── Stage 5 deep-link helper ────────────────────────────────────────────
# Build the operator's bet-slip URL from the env template appropriate for
# the card's bet_type. Called at every `add_prematch_card` site so the
# frontend just reads `card.deep_link`. When the kill switch
# PULSE_DEEPLINK_ENABLED is false, returns None → CTA stays dead.
def _build_deep_link(card: Card) -> "str | None":
    from app.config import (
        PULSE_DEEPLINK_ENABLED,
        PULSE_DEEPLINK_TEMPLATE_SINGLE,
        PULSE_DEEPLINK_TEMPLATE_BB,
        PULSE_DEEPLINK_TEMPLATE_COMBO,
        PULSE_DEEPLINK_TEMPLATE_BSCODE,
        PULSE_DEEPLINK_TEMPLATE_BSCODE_DIRECT,
        PULSE_DEEPLINK_USE_DIRECT_KMIANKO,
        PULSE_OPERATOR_WRAPPER_URL,
        PULSE_KMIANKO_BASE_URL,
        PULSE_KMIANKO_SPBKV3_PATH,
    )
    from urllib.parse import quote as _quote
    if not PULSE_DEEPLINK_ENABLED:
        return None
    # Stage 5b: when we have a server-minted bscode, every bet type uses
    # the same bscode URL — kmianko restores the full slip from the code
    # alone. Fall through to the PR #36 selectionId URLs when bscode is
    # missing (minter disabled / mint failed / no selection_ids).
    #
    # PULSE_DEEPLINK_USE_DIRECT_KMIANKO (default TRUE) emits the direct
    # kmianko URL instead of the apuestatotal.com wrapper — the wrapper's
    # Next.js fpath decoder strips `bscode` so the slip never hydrates.
    if card.bscode:
        try:
            if PULSE_DEEPLINK_USE_DIRECT_KMIANKO:
                return PULSE_DEEPLINK_TEMPLATE_BSCODE_DIRECT.format(
                    kmianko_base=PULSE_KMIANKO_BASE_URL.rstrip("/"),
                    spbkv3_path=PULSE_KMIANKO_SPBKV3_PATH,
                    bscode=_quote(card.bscode, safe=""),
                )
            return PULSE_DEEPLINK_TEMPLATE_BSCODE.format(
                wrapper=PULSE_OPERATOR_WRAPPER_URL,
                bscode=_quote(card.bscode, safe=""),
            )
        except Exception as exc:
            logger.debug("[PULSE] bscode deep_link build failed (%s) for card=%s", exc, card.id)
            # fall through to PR #36 URL
    bet_type = (card.bet_type or "single").lower()
    try:
        if bet_type == "bet_builder":
            # BBs prefer the virtual-selection id (0VS<piped>) because
            # kmianko's iframe treats it atomically and restores the full
            # leg stack. Fall back to the first selection_id when the
            # candidate pre-dates PR #16 (virtual_selection column).
            vs = card.virtual_selection or ""
            if not vs:
                ids = [l.selection_id for l in (card.legs or []) if l and l.selection_id]
                if not ids:
                    return None
                vs = ids[0]
            return PULSE_DEEPLINK_TEMPLATE_BB.format(
                virtual_selection=_quote(vs, safe=""),
                # Templates may or may not reference selection_ids — the
                # format call tolerates extra keys.
                selection_ids=_quote(",".join(
                    [l.selection_id for l in (card.legs or []) if l and l.selection_id]
                ), safe=","),
            )
        if bet_type == "combo":
            # Cross-event combos: kmianko accepts one selectionId at load
            # time, so we deep-link to the first leg and let the user add
            # the rest manually. See config.py for the known-limitation
            # note. When the operator ships server-minted `bscode` slip
            # codes we'll swap this for a multi-leg template.
            ids = [l.selection_id for l in (card.legs or []) if l and l.selection_id]
            if not ids:
                return None
            return PULSE_DEEPLINK_TEMPLATE_COMBO.format(
                selection_ids=_quote(ids[0], safe=""),
                virtual_selection="",
            )
        # Singles: first selection on the card's market. Some assembled
        # cards lose market.selections during the goalscorer trim; fall
        # back to nothing in that case rather than link to an unrelated
        # market.
        sel_id = None
        if card.market and card.market.selections:
            sel_id = card.market.selections[0].selection_id
        if not sel_id:
            return None
        return PULSE_DEEPLINK_TEMPLATE_SINGLE.format(
            selection_ids=_quote(sel_id, safe=""),
            virtual_selection="",
        )
    except Exception as exc:
        # Never block a card from rendering over a format error. Log once;
        # the CTA just falls back to dead.
        logger.debug("[PULSE] deep_link build failed (%s) for card=%s", exc, card.id)
        return None


def _attach_deep_link(card: Card) -> Card:
    """Populate card.deep_link in place (idempotent) and return the card.
    Called from every add_prematch_card site. Safe to call twice."""
    if card.deep_link is None:
        card.deep_link = _build_deep_link(card)
    return card


# ── Stage 5b — kmianko bscode minter ────────────────────────────────────
# Shared async instance. Tokens + minted codes cached in-memory; a
# semaphore caps concurrency so a 30-fixture publish burst doesn't hammer
# Kmianko. Kill switch: PULSE_KMIANKO_BSCODE_ENABLED=false.
from app.config import (
    PULSE_KMIANKO_BSCODE_ENABLED,
    PULSE_KMIANKO_BASE_URL,
    PULSE_KMIANKO_SPBKV3_PATH,
)

kmianko_slip_minter = KmiankoSlipMinter(
    base_url=PULSE_KMIANKO_BASE_URL,
    spbkv3_path=PULSE_KMIANKO_SPBKV3_PATH,
)


def _selections_for_mint(card: Card) -> list[str]:
    """Build the payload list for Kmianko's /share-betslip body.

    - BetBuilder: one element — the piped `0VS<leg>|<leg>|…` virtual
      selection id. Kmianko treats that atomically and restores the full
      BB slip. Falls back to individual leg ids when virtual_selection
      is missing (pre-PR-#16 cards, or featured BBs without a vid).
    - Combo / cross-event: every leg selection id as a separate element.
    - Single: the one selection id from `market.selections[0]`.
    """
    bet_type = (card.bet_type or "single").lower()
    if bet_type == "bet_builder":
        if card.virtual_selection:
            return [card.virtual_selection]
        return [l.selection_id for l in (card.legs or []) if l and l.selection_id]
    if bet_type == "combo":
        return [l.selection_id for l in (card.legs or []) if l and l.selection_id]
    # single
    if card.market and card.market.selections:
        sid = card.market.selections[0].selection_id
        if sid:
            return [sid]
    return []


async def _mint_and_stamp(card: Card) -> None:
    """Mint a bscode for this card's selections and stamp it on the Card
    + its deep_link. Safe to call multiple times; cached server-side.

    Failures are swallowed — when `card.bscode` stays None, the decorator
    falls back to PR #36's selectionId deep-link. Publishing must never
    block on a mint failure (principle: operator URL is best-effort).
    """
    if not PULSE_KMIANKO_BSCODE_ENABLED:
        return
    if card.bscode:
        return
    sel_ids = _selections_for_mint(card)
    if not sel_ids:
        return
    try:
        code = await kmianko_slip_minter.mint(
            sel_ids, bet_type=(card.bet_type or "single").lower(),
        )
    except Exception as exc:
        logger.warning("[PULSE] kmianko mint raised (ignored): %s", exc)
        return
    if code:
        card.bscode = code
        # Force rebuild so bscode variant wins over any prior selectionId URL.
        card.deep_link = None
        _attach_deep_link(card)


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

# Stage 5 — deep-link CTA. Every card the live feed ingests gets a
# pre-built operator URL stamped on it (see `_attach_deep_link` above).
# Staging feeds created during reruns install the same decorator below.
feed.set_decorator(_attach_deep_link)

# ── Create app ──
# Production hardening: Railway sets RAILWAY_ENVIRONMENT automatically.
# In prod: disable OpenAPI docs (attackers love a full API surface),
# require an explicit CORS allowlist, and apply security headers to every
# response. Dev stays permissive so local work isn't disrupted.
_IS_PROD = bool(os.environ.get("RAILWAY_ENVIRONMENT"))
_EXPOSE_DOCS = (not _IS_PROD) or os.environ.get("PULSE_EXPOSE_DOCS", "false").lower() == "true"

app = FastAPI(
    title="PULSE POC",
    version="0.1.0",
    docs_url="/docs" if _EXPOSE_DOCS else None,
    redoc_url="/redoc" if _EXPOSE_DOCS else None,
    openapi_url="/openapi.json" if _EXPOSE_DOCS else None,
)

# CORS: explicit allowlist in prod via PULSE_CORS_ALLOWED_ORIGINS (comma-separated).
# No allowlist in prod → deny cross-origin. Same-origin requests (app serving its
# own UI) don't need CORS headers, so the UI keeps working.
_cors_env = os.environ.get("PULSE_CORS_ALLOWED_ORIGINS", "").strip()
if _IS_PROD:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    _cors_credentials = bool(_cors_origins)
else:
    _cors_origins = ["*"]
    _cors_credentials = False  # `*` with credentials is invalid per the CORS spec

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    """Apply standard web-security response headers.

    HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
    Permissions-Policy, and a CSP tuned for this app (inline styles/scripts
    in index.html, same-origin SSE/fetch). CSP is set to report-only when
    PULSE_CSP_REPORT_ONLY=true so we can iterate without breaking the UI.

    Implemented as a *pure ASGI* middleware (not BaseHTTPMiddleware). The
    original BaseHTTPMiddleware version added a ~1s baseline latency to
    every request in production: BaseHTTPMiddleware bridges ASGI through
    anyio memory streams and a separate task, which on a busy event loop
    (the SSE pricing manager fires constantly) starves the response. Pure
    ASGI middleware just mutates the outgoing message in place — no extra
    task, no streams.
    """

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

    def __init__(self, app):
        self.app = app
        # Resolve the CSP header name once at startup. The env var was
        # checked per-request in the old middleware; it's a process-level
        # config flag, so reading it once is fine and saves the lookup.
        report_only = os.environ.get("PULSE_CSP_REPORT_ONLY", "false").lower() == "true"
        self._csp_header_name = (
            b"content-security-policy-report-only" if report_only else b"content-security-policy"
        )
        # Pre-encode the header tuples once. ASGI wants (bytes, bytes).
        self._extra_headers: list[tuple[bytes, bytes]] = [
            (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"geolocation=(), microphone=(), camera=(), interest-cohort=()"),
            (self._csp_header_name, self._CSP.encode("latin-1")),
        ]

    async def __call__(self, scope, receive, send):
        # Only touch HTTP responses. WebSocket / lifespan pass through.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = self._extra_headers

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                # Build a set of existing header names (lowercased) so we
                # mirror BaseHTTPMiddleware's `setdefault` semantics — don't
                # clobber a value the route already set.
                existing = {name for name, _ in headers}
                for name, value in extra:
                    if name not in existing:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeadersMiddleware)


class AnonCookieMiddleware:
    """Ensure every HTTP response carries a stable `pulse_anon_id` cookie.

    First-party, HttpOnly, Secure, SameSite=Lax, 1-year expiry. Generated
    server-side via `secrets.token_urlsafe(16)` on first visit. Used by
    the public reactions router to key thumbs-up/down per viewer without
    any login or fingerprinting. Pure ASGI for the same reason the
    SecurityHeadersMiddleware is — avoids the BaseHTTPMiddleware anyio
    bridge latency hit under SSE load.
    """

    _COOKIE_NAME = "pulse_anon_id"
    _MAX_AGE = 365 * 24 * 60 * 60  # 1 year in seconds

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _parse_cookie(raw: bytes, name: str) -> "str | None":
        if not raw:
            return None
        for part in raw.decode("latin-1").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v or None
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Pull the existing cookie (if any) from request headers.
        existing: "str | None" = None
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                existing = self._parse_cookie(value, self._COOKIE_NAME)
                break

        # If missing, mint one now and inject into request scope so downstream
        # handlers (e.g. /api/cards/{id}/react) see it on the SAME request —
        # not just on the next round-trip.
        new_id: "str | None" = None
        if not existing:
            import secrets as _secrets
            new_id = _secrets.token_urlsafe(16)
            headers = list(scope.get("headers", []))
            # Remove any existing (malformed) Cookie header, then append ours.
            headers = [(n, v) for n, v in headers if n != b"cookie"]
            cookie_val = f"{self._COOKIE_NAME}={new_id}".encode("latin-1")
            # If there were other cookies we stripped, lose them — this path
            # only runs when the header was missing or didn't contain our
            # cookie. Keep Starlette's Cookie dependency happy by preserving
            # other cookies we may have dropped above.
            for n, v in scope.get("headers", []):
                if n == b"cookie":
                    # rebuild: original cookies + our new one
                    combined = v + b"; " + cookie_val
                    headers.append((b"cookie", combined))
                    break
            else:
                headers.append((b"cookie", cookie_val))
            scope = dict(scope)
            scope["headers"] = headers

        set_cookie_value: "bytes | None" = None
        if new_id:
            # Secure flag on in prod (HTTPS guaranteed behind Railway edge).
            # In local dev we drop Secure so the cookie sticks over http.
            secure_attr = "; Secure" if _IS_PROD else ""
            set_cookie_value = (
                f"{self._COOKIE_NAME}={new_id}; "
                f"Max-Age={self._MAX_AGE}; Path=/; HttpOnly{secure_attr}; SameSite=Lax"
            ).encode("latin-1")

        async def send_wrapper(message):
            if set_cookie_value is not None and message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"set-cookie", set_cookie_value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(AnonCookieMiddleware)

# Rate limiting: per-IP defaults applied to every route. slowapi uses
# request.client.host as the key — behind Railway's edge that's the
# proxy IP, so effective per-client limiting requires a custom key_func
# reading X-Forwarded-For. Tracked as a follow-up.
#
# Defaults chosen for a low-traffic SSE-heavy service:
#   300/minute caps sustained hammering (~5 RPS average)
#   20/second tolerates short bursts (SSE reconnects, UI polling)
# Override per deploy with PULSE_RATE_LIMIT_DEFAULTS="N/unit,N/unit".
_RATE_LIMIT_DEFAULTS = os.environ.get(
    "PULSE_RATE_LIMIT_DEFAULTS", "300/minute,20/second"
).split(",")
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[l.strip() for l in _RATE_LIMIT_DEFAULTS if l.strip()],
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── Candidate engine plumbing ──
from typing import Optional as _Optional
candidate_store = CandidateStore(PULSE_DB_PATH)
candidate_engine: _Optional[CandidateEngine] = None  # initialised in startup

# ── Mount routes ──
router = create_routes(catalog, feed, simulator)
app.include_router(router)
admin_router = create_admin_routes(candidate_store, catalog, simulator)
app.include_router(admin_router)
reactions_router = create_reactions_routes(candidate_store, feed, limiter)
app.include_router(reactions_router)

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


# Liveness probe. Intentionally trivial — no I/O, no DB, no external calls —
# so it stays in single-digit ms even when the SSE pricing loop is busy.
# Use this for Railway healthchecks and uptime monitors.
@app.get("/health")
@limiter.exempt
async def health(request: Request):
    return {"ok": True}


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
# Long-lived RogueClient + SSE manager for live price updates. Distinct
# from the short-lived client used inside _load_rogue_prematch (that one
# closes at end of each run). This one stays open for the SSE stream's
# lifetime.
_sse_rogue_client: _Optional[RogueClient] = None
_sse_manager: _Optional[SSEPricingManager] = None


@app.on_event("startup")
async def generate_prematch_cards():
    """Generate pre-match cards.

    Data-source controlled by PULSE_DATA_SOURCE env var:
      - "mock" (default): hand-crafted LAL/ARS/KC demo cards from local JSON.
      - "rogue": real pre-match soccer fixtures pulled from the Rogue API.
    """
    global _rerun_task, _sse_rogue_client, _sse_manager
    # Ensure the SQLite schema is in place before any request path touches
    # it. The reactions endpoints can be hit before `_run_candidate_engine`
    # (which also calls init) runs — and in mock mode it never runs at all.
    try:
        await candidate_store.init()
    except Exception as exc:
        logger.exception("[PULSE] candidate_store.init failed at startup: %s", exc)
    if PULSE_DATA_SOURCE == "rogue":
        await _load_rogue_prematch()
        # Kick off the periodic rerun loop AFTER initial load completes.
        # The loop sleeps first, so it doesn't compete with the boot pass.
        import asyncio as _asyncio
        from app.config import PULSE_TIERED_FRESHNESS_ENABLED as _TIERED
        if _TIERED:
            # New (2026-04-24) tiered freshness + staggered publish. Each
            # tier schedules itself; cards stream into the live feed one at
            # a time via the per-card broadcaster hook.
            for _tier in _TIER_ORDER:
                _tier_tasks.append(_asyncio.create_task(_tier_loop(_tier)))
            _tier_tasks.append(_asyncio.create_task(_card_ttl_sweep_loop()))
            # PR #53: release-buffer scheduler — enforces hook-variety
            # across adjacent WS `card_added` broadcasts. Runs even when
            # the buffer is disabled (noop); cheap timer.
            global _release_task
            _release_task = _asyncio.create_task(_release_loop())
            _tier_tasks.append(_release_task)
            logger.info(
                "[PULSE] Tiered freshness active — %d tier loops + TTL sweep + release buffer",
                len(_TIER_ORDER),
            )
        else:
            _rerun_task = _asyncio.create_task(_scheduled_rerun_loop())

        # Start the SSE live-pricing manager. Separate long-lived RogueClient
        # so the SSE stream isn't killed when the rerun's transient client
        # closes. Disable via PULSE_SSE_PRICING_ENABLED=false.
        if os.getenv("PULSE_SSE_PRICING_ENABLED", "true").lower() == "true" and ROGUE_CONFIG_JWT:
            _sse_rogue_client = RogueClient(
                base_url=ROGUE_BASE_URL,
                config_jwt=ROGUE_CONFIG_JWT,
                per_second=ROGUE_RATE_LIMIT_PER_SECOND,
            )
            _sse_manager = SSEPricingManager(feed, _sse_rogue_client)
            await _sse_manager.start()
            _sse_manager.set_cards(feed.prematch_cards)
            logger.info("[PULSE] SSE live-pricing manager running")
        return
    await _load_mock_prematch()


@app.on_event("shutdown")
async def _shutdown_tasks():
    for t in _tier_tasks:
        try:
            t.cancel()
        except Exception:
            pass
    if _sse_manager is not None:
        await _sse_manager.stop()
    if _sse_rogue_client is not None:
        await _sse_rogue_client.close()


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
        staging.set_decorator(_attach_deep_link)
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
        if _sse_manager is not None:
            _sse_manager.set_cards(new_cards)
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
    if not PULSE_NEWS_INGEST_ENABLED:
        raise HTTPException(
            503,
            "rerun blocked: PULSE_NEWS_INGEST_ENABLED=false (kill switch). "
            "Set the env var to true on Railway and the next call will proceed.",
        )
    if _ondemand_rerun_inflight:
        raise HTTPException(
            409,
            f"a rerun is already in flight (started {int(time.time() - _ondemand_rerun_started_at)}s ago)",
        )
    _ondemand_rerun_inflight = True
    _ondemand_rerun_started_at = time.time()
    import asyncio as _asyncio
    from app.config import PULSE_TIERED_FRESHNESS_ENABLED as _TIERED
    if _TIERED:
        # Tiered mode: fan out one cycle per tier concurrently so the demo
        # sees cards insert one-by-one across every window. Keeps the
        # single-flight guard via `_tier_inflight` per tier.
        async def _fanout():
            global _ondemand_rerun_inflight, _ondemand_rerun_last_result
            t0 = time.time()
            try:
                results = await _asyncio.gather(
                    *[_run_tier_once(t) for t in _TIER_ORDER],
                    return_exceptions=True,
                )
                # Preserve per-tier dict so the status endpoint can surface
                # scouted / skipped_fresh / candidates / cost_estimate_usd
                # fields for each tier (volume-up PR observability).
                tier_results: dict[str, object] = {}
                for t, r in zip(_TIER_ORDER, results):
                    if isinstance(r, dict):
                        tier_results[t] = r
                    elif isinstance(r, BaseException):
                        tier_results[t] = {"ok": False, "error": str(r)}
                    else:
                        tier_results[t] = {"ok": False, "raw": str(r)}
                totals = {
                    "scouted": sum(int(v.get("scouted", 0) or 0) for v in tier_results.values() if isinstance(v, dict)),
                    "skipped_fresh": sum(int(v.get("skipped_fresh", 0) or 0) for v in tier_results.values() if isinstance(v, dict)),
                    "candidates": sum(int(v.get("candidates", 0) or 0) for v in tier_results.values() if isinstance(v, dict)),
                    "cost_estimate_usd": round(sum(float(v.get("cost_estimate_usd", 0) or 0) for v in tier_results.values() if isinstance(v, dict)), 4),
                    "daily_total_usd": round(_daily_cost_usd, 4),
                }
                _ondemand_rerun_last_result = {
                    "ok": True,
                    "tiers": tier_results,
                    "totals": totals,
                    "elapsed_s": round(time.time() - t0, 1),
                    "finished_at": time.time(),
                }
                logger.info("[PULSE] Tiered rerun fan-out complete")
            except Exception as exc:
                logger.exception("[PULSE] Tiered rerun fan-out failed: %s", exc)
                _ondemand_rerun_last_result = {
                    "ok": False, "error": str(exc),
                    "elapsed_s": round(time.time() - t0, 1),
                    "finished_at": time.time(),
                }
            finally:
                _ondemand_rerun_inflight = False
        _asyncio.create_task(_fanout())
        return {
            "ok": True,
            "status": "started",
            "mode": "tiered",
            "tiers": list(_TIER_ORDER),
            "estimated_seconds": 120,
            "poll": "/admin/rerun/status",
            "ws_event": "card_added",
        }
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
        # Daily rolling cost telemetry (UTC bucket, resets at day-boundary).
        # Surfaced here so a leak is visible without log scraping.
        "cost_telemetry": {
            "day_utc": _daily_cost_day or _today_utc(),
            "daily_total_usd": round(_daily_cost_usd, 4),
            "last_cycle_calls": dict(_cycle_call_counts),
            "last_cycle_breakdown_usd": _cycle_cost_breakdown(),
        },
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

        # Baseline pre-match cards (1X2 per fixture) — DISABLED by default
        # per principle 3 ("no-news fixtures get dropped"). Pulse is the
        # additive layer; we'd rather show 4 fixtures with stories than
        # 10 with fillers. Set PULSE_BASELINE_FALLBACK=true to revive the
        # safety net (e.g. when the news scout / Anthropic is having a bad
        # day and we still need cards on the feed).
        if os.getenv("PULSE_BASELINE_FALLBACK", "false").lower() == "true":
            cards = build_prematch_cards(games, catalog, assembler)
            # Mint bscodes before publishing so the feed decorator sees them.
            for card in cards:
                await _mint_and_stamp(card)
            for card in cards:
                target_feed.add_prematch_card(card)
            logger.info("[PULSE] Baseline pre-match cards added: %d (PULSE_BASELINE_FALLBACK=true)", len(cards))

        # Cost guard — kill switch. Flip PULSE_NEWS_INGEST_ENABLED=false on
        # Railway to stop ALL Anthropic-spending engine calls. Featured BBs
        # still load (no LLM). Useful when iterating without a demo, or
        # when the API key is bouncing on credits.
        if not PULSE_NEWS_INGEST_ENABLED:
            logger.info(
                "[PULSE] Candidate engine SKIPPED (PULSE_NEWS_INGEST_ENABLED=false). "
                "Feed will show only featured BBs."
            )
        else:
            # Real correlated BB + combo prices come from the same
            # RogueClient via POST /v1/betting/calculateBets.
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
                # Mint bscodes for featured BBs so the CTA restores the
                # full slip (piped 0VS virtual selection). Mint failures
                # leave card.bscode=None → selectionId fallback.
                for c in featured_cards:
                    await _mint_and_stamp(c)
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
        staging.set_decorator(_attach_deep_link)
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
        if _sse_manager is not None:
            _sse_manager.set_cards(new_cards)
        try:
            await feed.broadcast_feed_refresh()
        except Exception as exc:
            logger.warning("[PULSE] feed_refresh broadcast errored: %s", exc)
        elapsed = time.time() - t0
        logger.info(
            "[PULSE] Scheduled rerun complete: %d cards swapped in (%.1fs)",
            len(new_cards), elapsed,
        )


# ── Tiered freshness helpers ───────────────────────────────────────────
#
# Social-feed pivot (2026-04-24): replace the single 4h atomic cycle with
# four independent tier loops (HOT/WARM/COOL/COLD) so cards stream in over
# the hour rather than arriving in a 15-card burst once every 4h.
#
# Tier classification uses kickoff proximity. Game.start_time is a
# formatted string like "23 Apr 20:00 UTC" (no year — `_start_time` in
# catalogue_loader drops it). We reconstruct via the current year and
# bump to next year if the resulting kickoff is >6 months in the past
# (handles December→January wrap at year boundaries).

_TIER_HOT = "HOT"
_TIER_WARM = "WARM"
_TIER_COOL = "COOL"
_TIER_COLD = "COLD"
_TIER_ORDER = (_TIER_HOT, _TIER_WARM, _TIER_COOL, _TIER_COLD)


def _parse_kickoff_to_epoch(raw: str) -> "float | None":
    """Parse catalogue_loader's formatted kickoff string into a unix ts.

    Input shape: "23 Apr 20:00 UTC". Missing year — fill with the current
    UTC year, bump forward a year if the result lands >6 months in the
    past (Dec fixture viewed from Jan).
    """
    if not raw:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        txt = raw.strip()
        # Drop trailing "UTC" marker so strptime doesn't fail on tz text.
        if txt.endswith(" UTC"):
            txt = txt[:-4]
        now = datetime.now(timezone.utc)
        parsed = datetime.strptime(txt, "%d %b %H:%M").replace(
            year=now.year, tzinfo=timezone.utc,
        )
        if (now - parsed) > timedelta(days=180):
            parsed = parsed.replace(year=now.year + 1)
        return parsed.timestamp()
    except Exception:
        return None


def _classify_tier(game: Game, *, now: float) -> str:
    """Return the tier bucket for a fixture based on seconds-to-kickoff."""
    ko = _parse_kickoff_to_epoch(game.start_time or "")
    if ko is None:
        # Unknown kickoff — treat as COOL so we don't starve it but don't
        # waste HOT budget either. Can be tuned.
        return _TIER_COOL
    seconds_to_kickoff = ko - now
    if seconds_to_kickoff < 6 * 3600:
        return _TIER_HOT
    if seconds_to_kickoff < 24 * 3600:
        return _TIER_WARM
    if seconds_to_kickoff < 72 * 3600:
        return _TIER_COOL
    return _TIER_COLD


_TIER_CONFIG = {
    # (cadence_seconds, max_fixtures) pulled from env at read time so the
    # values reflect any runtime override. Defaults here mirror
    # app.config (volume-up PR #53 defaults: HOT 30min/10 fixtures,
    # WARM 1h/10 fixtures). Change both sites together.
    _TIER_HOT: lambda: (
        int(os.getenv("PULSE_TIER_HOT_MIN_SECONDS", "1800")),
        int(os.getenv("PULSE_TIER_HOT_MAX_FIXTURES", "10")),
    ),
    _TIER_WARM: lambda: (
        int(os.getenv("PULSE_TIER_WARM_MIN_SECONDS", "3600")),
        int(os.getenv("PULSE_TIER_WARM_MAX_FIXTURES", "10")),
    ),
    _TIER_COOL: lambda: (
        int(os.getenv("PULSE_TIER_COOL_MIN_SECONDS", "21600")),
        int(os.getenv("PULSE_TIER_COOL_MAX_FIXTURES", "6")),
    ),
    _TIER_COLD: lambda: (
        int(os.getenv("PULSE_TIER_COLD_MIN_SECONDS", "43200")),
        int(os.getenv("PULSE_TIER_COLD_MAX_FIXTURES", "4")),
    ),
}

# ── Hook-diversity release buffer ──────────────────────────────────────
# PR #53: instead of broadcasting each new card over WS immediately, push
# it into a buffer. A single release scheduler wakes every ~20s, picks
# the next-best card whose hook_type differs from the last 2 published,
# and broadcasts. If none match → release the oldest anyway (no
# starvation). Makes the stream order stay varied, not just the snapshot
# (per project_pulse_social_feed.md rule 7). Disable by setting
# PULSE_PUBLISH_BUFFER_SECONDS=0 — broadcasts then fire immediately.
_publish_buffer: list[tuple[float, "Card"]] = []  # (pushed_at_ts, card)
_publish_buffer_lock = None  # asyncio.Lock, lazy init
_last_broadcast_hooks: list[str] = []  # most-recent-last, cap at 2
_release_task = None


def _buffer_enabled() -> bool:
    try:
        from app.config import PULSE_PUBLISH_BUFFER_SECONDS as _BUF
        return int(_BUF) > 0
    except Exception:
        return False


async def _enqueue_release(card: "Card") -> None:
    """Push a card into the release buffer (PR #53).

    If the buffer is disabled, broadcast immediately (pre-PR #53 shape).
    If enabled, the release scheduler picks up the card within a few
    seconds and broadcasts when the hook-diversity rule allows.
    """
    import time as _t
    import asyncio as _a
    if not _buffer_enabled():
        try:
            await feed.broadcast_card_added(card)
        except Exception as exc:
            logger.warning("[PULSE] broadcast_card_added errored: %s", exc)
        return
    global _publish_buffer_lock
    if _publish_buffer_lock is None:
        _publish_buffer_lock = _a.Lock()
    async with _publish_buffer_lock:
        _publish_buffer.append((_t.time(), card))


async def _release_loop() -> None:
    """Single scheduler that drains the buffer in hook-diverse order.

    Wakes every 20s by default. For each tick:
      1. Drop cards whose hook_type hasn't been broadcast in the last 2.
         If none, take the OLDEST card (no starvation; age beats variety).
      2. Broadcast, update last_broadcast_hooks (cap 2).
      3. If buffer still holds cards whose wait exceeds
         PULSE_PUBLISH_BUFFER_SECONDS, release them too (up to 3 per tick
         to avoid a thundering-herd if the engine dumped 20 cards in).
    """
    import asyncio as _a
    import time as _t
    global _publish_buffer_lock
    if _publish_buffer_lock is None:
        _publish_buffer_lock = _a.Lock()
    tick_s = 20
    logger.info("[PULSE] release-buffer loop active (tick=%ds)", tick_s)
    while True:
        try:
            await _a.sleep(tick_s)
        except _a.CancelledError:
            return
        try:
            from app.config import PULSE_PUBLISH_BUFFER_SECONDS as _BUF_S
            buf_s = int(_BUF_S)
        except Exception:
            buf_s = 60
        if buf_s <= 0:
            continue  # live-disabled; drain nothing, broadcasts went direct
        now = _t.time()
        to_release: list["Card"] = []
        async with _publish_buffer_lock:
            if not _publish_buffer:
                continue
            # Pick up to 3 cards per tick.
            for _ in range(3):
                if not _publish_buffer:
                    break
                idx = None
                # Prefer a card whose hook differs from the last 2 broadcast.
                for i, (_ts, c) in enumerate(_publish_buffer):
                    hook = getattr(c, "hook_type", None) or ""
                    if hook not in _last_broadcast_hooks:
                        idx = i
                        break
                # If every buffered card's hook is in the recent window,
                # fall back to whichever has been waiting longest (the
                # head of the buffer, since we append in arrival order).
                # Also: any card older than PULSE_PUBLISH_BUFFER_SECONDS
                # has to go out regardless — no starvation.
                if idx is None:
                    # Forced release only if a card has aged past the
                    # buffer ceiling. Otherwise wait another tick.
                    aged = [
                        (i, c) for i, (ts, c) in enumerate(_publish_buffer)
                        if (now - ts) >= buf_s
                    ]
                    if not aged:
                        break
                    idx = aged[0][0]
                _ts, c = _publish_buffer.pop(idx)
                to_release.append(c)
                hook = getattr(c, "hook_type", None) or ""
                _last_broadcast_hooks.append(hook)
                if len(_last_broadcast_hooks) > 2:
                    del _last_broadcast_hooks[0]
        # Broadcast outside the lock so a slow WS client can't block
        # further enqueues.
        for c in to_release:
            try:
                await feed.broadcast_card_added(c)
                logger.info(
                    "[PULSE] release-buffer broadcast card=%s hook=%s (recent=%s)",
                    c.id, getattr(c, "hook_type", None),
                    list(_last_broadcast_hooks),
                )
            except Exception as exc:
                logger.warning("[PULSE] release-buffer broadcast errored: %s", exc)


# Per-tier reentry guard so a long-running cycle can't overlap itself.
_tier_inflight: dict[str, bool] = {t: False for t in _TIER_ORDER}
_tier_tasks: list = []

# Process-level cost-telemetry counters. Incremented during a cycle,
# read at end-of-cycle for the `[cost] cycle total: ...` log line and
# the daily rolling total in `/admin/rerun/status`. Reset at the top of
# each tier cycle (so per-cycle log is honest) and aggregated daily.
_cycle_call_counts: dict[str, int] = {
    "scout_haiku_websearch": 0,
    "storyline_sonnet_websearch": 0,  # storyline detector LLM call
    "standings_haiku_websearch": 0,
    "rewrite_sonnet": 0,
}
_daily_cost_usd: float = 0.0
_daily_cost_day: str = ""  # 'YYYY-MM-DD' UTC; resets the bucket on rollover


def _today_utc() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d", _t.gmtime())


def _reset_cycle_counters() -> None:
    for k in _cycle_call_counts:
        _cycle_call_counts[k] = 0


def _bump_cycle_counter(name: str, by: int = 1) -> None:
    if name in _cycle_call_counts:
        _cycle_call_counts[name] += by


def _accumulate_daily_cost(usd: float) -> None:
    global _daily_cost_usd, _daily_cost_day
    today = _today_utc()
    if today != _daily_cost_day:
        _daily_cost_day = today
        _daily_cost_usd = 0.0
    _daily_cost_usd += float(usd or 0.0)


def _cycle_cost_breakdown() -> dict[str, float]:
    """Compute per-bucket USD cost from current cycle counters.

    Uses env-knobbed per-call estimates from app.config — directional,
    not billable. Real telemetry lives in Anthropic's usage API.
    """
    try:
        from app.config import (
            PULSE_COST_HAIKU_PER_CALL,
            PULSE_COST_HAIKU_WEBSEARCH_PER_CALL,
            PULSE_COST_SONNET_PER_CALL,
        )
    except Exception:
        PULSE_COST_HAIKU_PER_CALL = 0.01
        PULSE_COST_HAIKU_WEBSEARCH_PER_CALL = 0.025
        PULSE_COST_SONNET_PER_CALL = 0.05
    scout_usd = (
        _cycle_call_counts.get("scout_haiku_websearch", 0)
        * float(PULSE_COST_HAIKU_WEBSEARCH_PER_CALL)
    )
    storyline_usd = (
        _cycle_call_counts.get("storyline_sonnet_websearch", 0)
        * float(PULSE_COST_SONNET_PER_CALL)
    )
    verify_usd = (
        _cycle_call_counts.get("standings_haiku_websearch", 0)
        * float(PULSE_COST_HAIKU_WEBSEARCH_PER_CALL)
    )
    rewrite_usd = (
        _cycle_call_counts.get("rewrite_sonnet", 0)
        * float(PULSE_COST_SONNET_PER_CALL)
    )
    total = scout_usd + storyline_usd + verify_usd + rewrite_usd
    return {
        "scout_usd": round(scout_usd, 4),
        "storylines_usd": round(storyline_usd, 4),
        "verify_usd": round(verify_usd, 4),
        "rewrite_usd": round(rewrite_usd, 4),
        "total_usd": round(total, 4),
    }

# Cross-tier mutex — the candidate engine sets a module-global
# `candidate_engine` singleton during `_run_candidate_engine`, so two
# tier cycles running concurrently would race it. Serialise the work
# (tier loops each publish quickly; minutes-apart cadence so this isn't
# a throughput bottleneck).
_engine_mutex = None  # initialised lazily (anyio/asyncio loop must exist)


async def _run_tier_once(tier: str) -> dict:
    """Execute one cycle of a tier: filter fixtures, scout, publish per-card.

    Called by both the periodic tier loop and the `/admin/rerun` fan-out.
    Safe against self-overlap: guarded by `_tier_inflight[tier]`.
    """
    import time as _time
    import asyncio as _asyncio
    global _engine_mutex
    if _engine_mutex is None:
        _engine_mutex = _asyncio.Lock()
    if _tier_inflight.get(tier):
        logger.info("[tier:%s] skip — previous cycle still running", tier)
        return {"skipped": "inflight"}
    _tier_inflight[tier] = True
    t0 = _time.time()
    try:
        _, max_fixtures = _TIER_CONFIG[tier]()
        games = dict(simulator._games or {})
        if not games:
            logger.info("[tier:%s] no games loaded yet — skipping", tier)
            return {"skipped": "no_games"}
        now = _time.time()
        tier_games = {
            gid: g for gid, g in games.items()
            if _classify_tier(g, now=now) == tier
        }
        if not tier_games:
            logger.info("[tier:%s] no fixtures in window", tier)
            return {"skipped": "no_fixtures"}
        # Respect per-tier cap — sort by earliest kickoff so we always
        # pick the most imminent within-window fixtures first.
        tier_games = dict(
            sorted(
                tier_games.items(),
                key=lambda kv: _parse_kickoff_to_epoch(kv[1].start_time or "") or 9e18,
            )[:max_fixtures]
        )
        fixture_ids = list(tier_games.keys())
        logger.info(
            "[tier:%s] cycle start — %d fixtures (cap=%d)",
            tier, len(fixture_ids), max_fixtures,
        )
        # Reset cycle cost-telemetry counters BEFORE any LLM calls fire.
        # End-of-cycle log + daily total read these.
        _reset_cycle_counters()
        # Reset storyline / standings counters (process-level in
        # storyline_detector). These survive across cycles otherwise.
        try:
            from app.engine.storyline_detector import (
                reset_standings_cache_counters,
                reset_storyline_cooldown_counters,
            )
            reset_standings_cache_counters()
            reset_storyline_cooldown_counters()
        except Exception as exc:
            logger.warning("[tier:%s] counter reset failed: %s", tier, exc)
        # Boot-freshness skip (2026-04-24 volume-up PR). For each fixture
        # in this tier, check the DB freshness via the centralised
        # `is_fixture_news_fresh` helper (which reads from
        # ingest_cache.ingested_at — the timestamp that's now bumped on
        # EVERY scout pass including cache hits). The TTL is the tier's
        # own cadence + slack, where slack covers boundary jitter (a
        # fixture scouted T seconds ago when cadence is also T seconds
        # was previously never "fresh" because the comparison sat on
        # the boundary — that bug had `skipped_fresh=0` in every cycle
        # log, see fix/cost-leak-freshness-cooldowns PR).
        from app.config import PULSE_BOOT_FRESHNESS_SKIP_ENABLED as _FRESH_SKIP
        # Read slack from env at runtime so a Railway env flip takes
        # effect on the next cycle without redeploy.
        try:
            _FRESH_SLACK = int(os.getenv("PULSE_TIER_FRESHNESS_SLACK_SECONDS", "300"))
        except ValueError:
            _FRESH_SLACK = 300
        tier_cadence_s, _ = _TIER_CONFIG[tier]()
        freshness_ttl = float(tier_cadence_s) + float(_FRESH_SLACK)
        skipped_fresh_ids: list[str] = []
        if _FRESH_SKIP:
            still_scout: dict[str, Game] = {}
            for gid, g in tier_games.items():
                try:
                    is_fresh, age_s = await candidate_store.is_fixture_news_fresh(
                        gid, freshness_ttl,
                    )
                except Exception as exc:
                    logger.warning(
                        "[tier:%s] freshness probe failed for %s: %s",
                        tier, gid, exc,
                    )
                    is_fresh, age_s = False, None
                if is_fresh and age_s is not None:
                    logger.info(
                        "[tier:%s] fixture %s fresh (%.0fs old) — skipped",
                        tier, gid, age_s,
                    )
                    skipped_fresh_ids.append(gid)
                else:
                    still_scout[gid] = g
            scouted_games = still_scout
        else:
            scouted_games = dict(tier_games)
        scouted_ids = list(scouted_games.keys())
        logger.info(
            "[tier:%s] freshness filter — %d to scout, %d skipped_fresh "
            "(ttl=%.0fs = cadence %.0fs + slack %ds)",
            tier, len(scouted_ids), len(skipped_fresh_ids),
            freshness_ttl, float(tier_cadence_s), int(_FRESH_SLACK),
        )
        # Expire prior published candidates for ONLY the fixtures we're
        # actually scouting this tick. Skipped-fresh fixtures keep their
        # existing cards in place (same news → same cards, no churn).
        try:
            n_expired = await candidate_store.expire_published_for_fixtures(scouted_ids)
            logger.info("[tier:%s] expired %d prior published candidates", tier, n_expired)
        except Exception as exc:
            logger.warning("[tier:%s] expire_for_fixtures failed: %s", tier, exc)
        # NOTE: we intentionally do NOT pre-remove existing cards for this
        # tier's fixtures. Pre-removing creates a visible gap (old cards
        # vanish ~30-60s before the fresh scout's cards land — the feed
        # looks empty mid-cycle). Instead we snapshot pre-cycle card ids
        # per fixture, let the engine publish fresh cards on top, then
        # sweep any pre-cycle ids that weren't re-emitted (deferred-
        # replace, zero-gap).
        fixture_set = set(scouted_ids)
        pre_cycle_ids_by_game: dict[str, list[str]] = {}
        for c in list(feed.prematch_cards):
            gid = getattr(getattr(c, "game", None), "id", None)
            if gid in fixture_set:
                pre_cycle_ids_by_game.setdefault(gid, []).append(c.id)
        newly_emitted_ids: set[str] = set()
        # Spin a short-lived RogueClient for the cycle.
        client = None
        if ROGUE_CONFIG_JWT:
            client = RogueClient(
                base_url=ROGUE_BASE_URL,
                config_jwt=ROGUE_CONFIG_JWT,
                per_second=ROGUE_RATE_LIMIT_PER_SECOND,
            )
        try:
            # Per-card broadcast hook. Only fires when staggered publish
            # is on (kill-switch PULSE_STAGGERED_PUBLISH_ENABLED). Uses
            # the release-buffer enqueue so the WS broadcast order stays
            # hook-diverse (PR #53). If PULSE_PUBLISH_BUFFER_SECONDS=0,
            # the enqueue path broadcasts immediately.
            from app.config import PULSE_STAGGERED_PUBLISH_ENABLED
            async def _on_card(card: Card) -> None:
                newly_emitted_ids.add(card.id)
                if not PULSE_STAGGERED_PUBLISH_ENABLED:
                    return
                try:
                    await _enqueue_release(card)
                except Exception as exc:
                    logger.warning("[tier:%s] enqueue_release errored: %s", tier, exc)
            if not PULSE_NEWS_INGEST_ENABLED:
                logger.info("[tier:%s] skipped — PULSE_NEWS_INGEST_ENABLED=false", tier)
            elif not scouted_games:
                logger.info(
                    "[tier:%s] all %d fixtures skipped_fresh — engine bypassed",
                    tier, len(skipped_fresh_ids),
                )
            else:
                async with _engine_mutex:
                    await _run_candidate_engine(
                        scouted_games,
                        rogue_client=client,
                        target_feed=feed,
                        max_fixtures=max_fixtures,
                        per_card_publish_hook=_on_card,
                        tier_label=tier,
                    )
            if _sse_manager is not None:
                _sse_manager.set_cards(feed.prematch_cards)
        finally:
            if client is not None:
                await client.close()
        # Deferred sweep — now that the engine finished and any new cards
        # have been added, drop any pre-cycle cards for these fixtures
        # that the engine did NOT re-emit (the fixture no longer yielded
        # a story, or the story deduped). Zero-gap: for replaced cards
        # the new one is already visible before the old goes.
        swept = 0
        for gid, prev_ids in pre_cycle_ids_by_game.items():
            for prev_id in prev_ids:
                if prev_id in newly_emitted_ids:
                    continue  # same id re-emitted → no-op
                removed = feed.remove_prematch_card(prev_id)
                if removed is None:
                    continue
                swept += 1
                try:
                    await feed.broadcast_card_removed(prev_id)
                except Exception as exc:
                    logger.warning("[tier:%s] sweep broadcast_card_removed errored: %s", tier, exc)
        if swept:
            logger.info("[tier:%s] swept %d replaced/orphaned cards", tier, swept)
        elapsed = _time.time() - t0
        # Honest per-cycle cost: tally every Haiku/Sonnet call that
        # actually fired during the cycle (scout, storylines, standings
        # verify, rewriter Sonnet) using env-knobbed per-call estimates.
        # Replaces the old per-scouted-fixture * $0.01 approximation,
        # which undercounted by ignoring storylines + verify + rewriter.
        cost = _cycle_cost_breakdown()
        est_cost = cost["total_usd"]
        _accumulate_daily_cost(est_cost)
        scouted_count = len(scouted_ids)
        skipped_count = len(skipped_fresh_ids)
        candidates_emitted = len(newly_emitted_ids)
        # Cooldown / cache hit-rates from the storyline detector. Read
        # post-cycle so the values reflect what just happened.
        try:
            from app.engine.storyline_detector import (
                get_standings_cache_counters,
                get_storyline_cooldown_counters,
            )
            std_hits, std_misses = get_standings_cache_counters()
            sl_hits, sl_misses = get_storyline_cooldown_counters()
        except Exception:
            std_hits = std_misses = sl_hits = sl_misses = 0
        logger.info(
            "[tier:%s] cycle: scouted=%d skipped_fresh=%d candidates=%d cost_estimate=$%.4f",
            tier, scouted_count, skipped_count, candidates_emitted, est_cost,
        )
        logger.info(
            "[cost] cycle total: scout=$%.4f storylines=$%.4f verify=$%.4f "
            "rewrite=$%.4f total=$%.4f calls=(scout=%d storyline=%d verify=%d rewrite=%d) "
            "standings_cache=(hit=%d miss=%d) storyline_cooldown=(hit=%d miss=%d) "
            "daily_total=$%.4f",
            cost["scout_usd"], cost["storylines_usd"], cost["verify_usd"],
            cost["rewrite_usd"], cost["total_usd"],
            _cycle_call_counts.get("scout_haiku_websearch", 0),
            _cycle_call_counts.get("storyline_sonnet_websearch", 0),
            _cycle_call_counts.get("standings_haiku_websearch", 0),
            _cycle_call_counts.get("rewrite_sonnet", 0),
            std_hits, std_misses, sl_hits, sl_misses,
            _daily_cost_usd,
        )
        logger.info("[tier:%s] cycle finish in %.1fs", tier, elapsed)
        return {
            "ok": True,
            "fixtures": len(fixture_ids),
            "scouted": scouted_count,
            "skipped_fresh": skipped_count,
            "candidates": candidates_emitted,
            "cost_estimate_usd": est_cost,
            "cost_breakdown_usd": cost,
            "calls": dict(_cycle_call_counts),
            "standings_cache": {"hits": std_hits, "misses": std_misses},
            "storyline_cooldown": {"hits": sl_hits, "misses": sl_misses},
            "elapsed_s": round(elapsed, 1),
        }
    except Exception as exc:
        logger.exception("[tier:%s] cycle errored: %s", tier, exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _tier_inflight[tier] = False


async def _tier_loop(tier: str) -> None:
    """Periodic tier loop — sleeps cadence, runs one cycle, repeats."""
    import asyncio as _asyncio
    cadence_s, _ = _TIER_CONFIG[tier]()
    # Stagger first-run offsets so tiers don't all fire at once on boot.
    _offset_map = {_TIER_HOT: 90, _TIER_WARM: 180, _TIER_COOL: 270, _TIER_COLD: 360}
    await _asyncio.sleep(_offset_map.get(tier, 60))
    logger.info("[tier:%s] loop active — cadence %ds", tier, cadence_s)
    while True:
        try:
            await _run_tier_once(tier)
        except _asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception("[tier:%s] unexpected loop error: %s", tier, exc)
        # Re-read cadence each cycle so env overrides without redeploy
        # take effect on the next iteration.
        cadence_s, _ = _TIER_CONFIG[tier]()
        try:
            await _asyncio.sleep(max(60, cadence_s))
        except _asyncio.CancelledError:
            return


async def _card_ttl_sweep_loop() -> None:
    """Periodically drop expired cards + broadcast removals."""
    import asyncio as _asyncio
    from app.config import PULSE_CARD_TTL_SECONDS, PULSE_CARD_TTL_SWEEP_SECONDS
    logger.info(
        "[PULSE] TTL sweep active — ttl=%ds, every %ds",
        PULSE_CARD_TTL_SECONDS, PULSE_CARD_TTL_SWEEP_SECONDS,
    )
    while True:
        try:
            await _asyncio.sleep(max(10, PULSE_CARD_TTL_SWEEP_SECONDS))
        except _asyncio.CancelledError:
            return
        try:
            dropped = feed.expire_stale_prematch(PULSE_CARD_TTL_SECONDS)
            if dropped:
                logger.info("[PULSE] TTL swept %d cards off the feed", len(dropped))
                for c in dropped:
                    try:
                        await feed.broadcast_card_removed(c.id)
                    except Exception as exc:
                        logger.warning(
                            "[PULSE] broadcast_card_removed errored: %s", exc,
                        )
        except Exception as exc:
            logger.exception("[PULSE] TTL sweep errored: %s", exc)


def _label_contains_excluded_player(label: str, excluded: set) -> bool:
    """True if the goalscorer selection label names a player in `excluded`.

    `excluded` is a set of normalized (lowercase, de-accented) player names
    provided by the publisher. Uses the same tokenization as the player-match
    path so "Lejeune" in the exclusion set catches "Jaime Lejeune" selections.
    """
    if not excluded:
        return False
    from app.engine.candidate_builder import _name_tokens
    sel_tokens = set(_name_tokens(label or ""))
    if not sel_tokens:
        return False
    for excl_name in excluded:
        excl_tokens = set(_name_tokens(excl_name))
        if not excl_tokens:
            continue
        # Require the excluded name's LAST token (usually surname) to appear
        # in the selection's tokens — avoids "Jaime" matching "Jaime Lejeune"
        # when the excluded player is "Jaime Rodriguez".
        excl_last = list(_name_tokens(excl_name))[-1] if _name_tokens(excl_name) else None
        if excl_last and excl_last in sel_tokens:
            return True
    return False


async def _run_candidate_engine(
    games_by_id: dict[str, Game],
    *,
    rogue_client: _Optional[RogueClient] = None,
    target_feed: _Optional[FeedManager] = None,
    max_fixtures: _Optional[int] = None,
    per_card_publish_hook=None,
    tier_label: str = "",
):
    """Run the news-driven engine across the live catalogue.

    Uses MockNewsIngester by default (no API key). When ANTHROPIC_API_KEY is
    set, we can swap in the real NewsIngester — imported lazily so missing
    SDK deps don't break the mock path. When a Rogue client is passed in,
    the ComboBuilder uses it for Bet Builder validation AND for real
    correlated pricing via POST /v1/betting/calculateBets.

    target_feed: which FeedManager to publish into. Defaults to live `feed`;
    scheduled rerun loop passes a staging FeedManager.

    max_fixtures: override PULSE_NEWS_MAX_FIXTURES. Tier loops pass a
    tier-specific cap so HOT fixtures get more frequent, tighter scans
    and COLD fixtures don't consume the whole budget.

    per_card_publish_hook: async callable invoked with each published Card
    immediately after insertion into target_feed. Staggered-publish mode
    uses this to broadcast `card_added` per card. None = silent batch.
    """
    if target_feed is None:
        target_feed = feed
    _tier_prefix = f"[tier:{tier_label}] " if tier_label else ""
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
            # U3: memoise rewrites by input-hash so unchanged candidates on
            # reruns don't re-pay Sonnet. Kill-switch + TTL env-controlled.
            _cache_enabled = os.getenv("PULSE_REWRITE_CACHE_ENABLED", "true").lower() == "true"
            try:
                _cache_ttl = float(os.getenv("PULSE_REWRITE_CACHE_TTL_SECONDS", "86400"))
            except ValueError:
                _cache_ttl = 86400.0
            rewriter = NarrativeRewriter(
                anth,
                model=os.getenv("PULSE_REWRITER_MODEL", "claude-sonnet-4-6"),
                store=candidate_store,
                cache_enabled=_cache_enabled,
                cache_ttl_seconds=_cache_ttl,
            )
            rewriter.reset_cache_counters()
            logger.info(
                "[PULSE] Candidate engine: scout=%s, rewriter=%s, rewrite_cache=%s (ttl=%.0fs)",
                PULSE_NEWS_MODEL,
                os.getenv("PULSE_REWRITER_MODEL", "claude-sonnet-4-6"),
                "on" if _cache_enabled else "off",
                _cache_ttl,
            )
        except Exception as exc:
            logger.warning("[PULSE] LLM ingester unavailable (%s) — using mock", exc)
            ingester = MockNewsIngester(candidate_store)
    else:
        ingester = MockNewsIngester(candidate_store)
        logger.info("[PULSE] Candidate engine using mock news (no ANTHROPIC_API_KEY)")

    resolver = NewsEntityResolver(games_by_id)
    # Pass games to CandidateBuilder so INJURY routing can look up the
    # affected side and do position-aware market selection (2026-04-23).
    builder = CandidateBuilder(catalog, games_by_id)
    scorer = NewsScorer()
    # Merge env-var override onto the module default HOOK_BET_TYPE_PREFERENCE.
    # Keys come in as strings (hook_type enum values); convert to HookType.
    from app.engine.news_scorer import HOOK_BET_TYPE_PREFERENCE
    from app.models.news import HookType as _HookType
    _merged_pref = dict(HOOK_BET_TYPE_PREFERENCE)
    for k_str, v in PULSE_HOOK_BET_TYPE_PREFERENCE_JSON.items():
        try:
            _merged_pref[_HookType(k_str)] = v
        except ValueError:
            logger.warning("[PULSE] ignoring unknown hook_type in override: %s", k_str)
    if PULSE_HOOK_BET_TYPE_PREFERENCE_JSON:
        logger.info(
            "[PULSE] Hook-bet-type preference override applied: %s",
            PULSE_HOOK_BET_TYPE_PREFERENCE_JSON,
        )
    from app.config import PULSE_NEWS_CANDIDATES_PER_FIXTURE_MAX
    policy = PolicyLayer(
        publish_threshold=PULSE_PUBLISH_THRESHOLD,
        per_fixture_cap=PULSE_NEWS_CANDIDATES_PER_FIXTURE_MAX,
        hook_bet_type_preference=_merged_pref,
    )
    # Bet Builder generator — only active when we have a Rogue client.
    # The same client provides real correlated BB pricing via the Betting
    # API (calculate_bets). Mock mode (no Rogue client) skips BBs entirely.
    combo_builder = ComboBuilder(catalog, rogue_client) if rogue_client is not None else None

    candidate_engine = CandidateEngine(
        ingester=ingester, resolver=resolver, builder=builder,
        scorer=scorer, policy=policy, store=candidate_store,
        combo_builder=combo_builder,
    )

    _cap = max_fixtures if (max_fixtures is not None) else PULSE_NEWS_MAX_FIXTURES
    counts = await candidate_engine.run_once(
        games_by_id, max_fixtures=_cap,
    )
    logger.info("%s[PULSE] Candidate engine counts: %s", _tier_prefix, counts)

    # ── Cross-event storyline combos (spike, replaces Stage 3d) ──
    # Runs ONCE per cycle after the per-fixture news engine. Asks the LLM to
    # detect a storyline pattern across the upcoming matchweek (Golden Boot
    # race in v1), binds named players to real fixtures + goalscorer legs,
    # emits BetType.COMBO candidates with populated selection_ids. Pricing
    # happens in the same post-engine calculate_bets sweep below.
    if (
        rogue_client is not None
        and ANTHROPIC_API_KEY
        and os.getenv("PULSE_STORYLINE_COMBOS_ENABLED", "true").lower() == "true"
    ):
        try:
            from anthropic import AsyncAnthropic as _AsyncAnth
            from app.engine.storyline_detector import StorylineDetector
            from app.engine.cross_event_builder import CrossEventBuilder
            from app.engine.combined_narrative_author import CombinedNarrativeAuthor
            from app.models.news import (
                CandidateStatus as _CStatus,
                StorylineType as _SType,
            )
            _anth = _AsyncAnth(api_key=ANTHROPIC_API_KEY)
            detector = StorylineDetector(
                _anth,
                model=os.getenv("PULSE_STORYLINE_MODEL", "claude-sonnet-4-6"),
                min_participants=PULSE_STORYLINE_MIN_PARTICIPANTS,
            )
            xbuilder = CrossEventBuilder(catalog)
            author = CombinedNarrativeAuthor(_anth, model=os.getenv("PULSE_STORYLINE_MODEL", "claude-sonnet-4-6"))

            try:
                story_cap = int(os.getenv("PULSE_STORYLINE_COMBOS_MAX", "5"))
            except ValueError:
                story_cap = 5

            # Enabled types — each with its own kill switch so ops can
            # disable a misbehaving detector without a redeploy. Insertion
            # order is the newsworthiness tiebreaker if all three have
            # equal participant counts.
            enabled_types: list = []
            if PULSE_STORYLINE_RELEGATION_ENABLED:
                enabled_types.append(_SType.RELEGATION)
            if PULSE_STORYLINE_EUROPE_CHASE_ENABLED:
                enabled_types.append(_SType.EUROPE_CHASE)
            if PULSE_STORYLINE_GOLDEN_BOOT_ENABLED:
                enabled_types.append(_SType.GOLDEN_BOOT)

            # Detect across all enabled types FIRST, score + select AFTER.
            # This way we don't burn the cap on the first type detected
            # and starve the others. Keeps at-most-one of each type when
            # possible so the feed doesn't get three Golden Boot cards.
            detected: list[tuple[object, int]] = []  # (story, score)
            for st in enabled_types:
                try:
                    stories = await detector.detect(st, games_by_id)
                except Exception as exc:
                    logger.warning(
                        "[PULSE] StorylineDetector %s errored: %s",
                        st.value, exc,
                    )
                    stories = []
                for story in stories:
                    # Score = participant count (more participants = more
                    # newsworthy; 2 is the floor, 5 is the ceiling). We
                    # don't have a real recency signal yet — standings
                    # lookups are always today — so this keeps it honest.
                    score = len(story.participants)
                    detected.append((story, score))
                    logger.info(
                        "[PULSE] Storyline detected: type=%s score=%d headline=%s",
                        story.storyline_type.value, score,
                        (story.headline_hint or "")[:80],
                    )

            # Sort by score desc. Then greedy-pick up to `story_cap`,
            # preferring diversity — take one of each type before a second
            # of the same type.
            detected.sort(key=lambda ss: -ss[1])
            picked_stories: list = []
            used_types: set = set()
            for story, _ in detected:
                if len(picked_stories) >= story_cap:
                    break
                if story.storyline_type in used_types:
                    continue
                picked_stories.append(story)
                used_types.add(story.storyline_type)
            # Second pass — if we have room and more stories detected,
            # take the remaining highest-scored regardless of type. This
            # only matters if future detectors return >1 storyline each;
            # today each returns 0 or 1 so pass 2 is a no-op.
            if len(picked_stories) < story_cap:
                for story, _ in detected:
                    if len(picked_stories) >= story_cap:
                        break
                    if story in picked_stories:
                        continue
                    picked_stories.append(story)

            storyline_candidates: list = []
            for story in picked_stories:
                cand = xbuilder.build(story, games_by_id)
                if cand is None:
                    continue
                # Storyline combos are hand-curated patterns — they skip
                # the news scorer + policy layer. Mark them published so
                # the render loop picks them up.
                cand.score = 0.85
                cand.reason = f"cross-event storyline: {story.storyline_type.value}"
                cand.threshold_passed = True
                cand.status = _CStatus.PUBLISHED
                # Stash the storyline in-memory via the candidate's
                # narrative field for the author step below; author will
                # overwrite with fresh headline + angle.
                cand._storyline = story  # type: ignore[attr-defined] — transient
                storyline_candidates.append(cand)

            # Author synthesised copy for each storyline combo BEFORE save,
            # so the headline on the CandidateCard is the storyline headline
            # (not the detector's hint). Needs legs resolved from catalog.
            from app.models.schemas import CardLeg as _CL
            # Collect (candidate, storyline, final_title) triples so we can
            # persist the storyline with the authored title — not the raw
            # detector hint — alongside the candidate row it backs.
            to_persist: list[tuple] = []
            for cand in storyline_candidates:
                story = getattr(cand, "_storyline", None)
                if story is None:
                    continue
                legs_for_copy: list = []
                for mid, sid in zip(cand.market_ids, cand.selection_ids):
                    m = catalog.get(mid)
                    if m is None:
                        continue
                    sel = next((s for s in m.selections if s.selection_id == sid), None)
                    if sel is None:
                        continue
                    try:
                        o = float(sel.odds)
                    except Exception:
                        o = 0.0
                    legs_for_copy.append(_CL(
                        label=sel.label, market_label=m.label, odds=o, selection_id=sid,
                    ))
                written = await author.author(
                    storyline=story, legs=legs_for_copy,
                    total_odds=cand.total_odds,
                )
                final_title = story.headline_hint or ""
                if written and written.get("headline"):
                    # Store the synthesised headline + angle in `narrative`
                    # joined by a separator the publisher will split on.
                    cand.narrative = f"{written['headline']}\n{written.get('angle', '')}"
                    final_title = written["headline"]
                    logger.info(
                        "[PULSE] Storyline copy: %s | %s",
                        written["headline"], written.get("angle", "")[:80],
                    )
                to_persist.append((cand, story, final_title))
                # Strip the transient attribute before save
                try:
                    delattr(cand, "_storyline")
                except Exception:
                    pass

            if storyline_candidates:
                # Persist StorylineItems FIRST so the candidate rows' FK
                # (storyline_id → storyline_items.id) resolves on save.
                for _cand, _story, _title in to_persist:
                    try:
                        await candidate_store.store_storyline(
                            _story, title=_title, status="active",
                        )
                    except Exception as exc:
                        logger.warning(
                            "[PULSE] store_storyline failed for %s: %s",
                            _story.id, exc,
                        )
                await candidate_store.save_candidates(storyline_candidates)
                logger.info(
                    "[PULSE] Cross-event storyline combos emitted: %d "
                    "(storylines persisted: %d)",
                    len(storyline_candidates), len(to_persist),
                )
            else:
                logger.info("[PULSE] Cross-event storylines: 0 combos built this cycle")
        except Exception as exc:
            logger.exception("[PULSE] Cross-event storyline step failed: %s", exc)

    # Cross-event combo pricing — when (in a future stage) the engine starts
    # emitting BetType.COMBO candidates, fetch the operator-boosted price via
    # Rogue's calculate_bets endpoint. The storyline combos above produce
    # exactly these; this sweep stamps real prices on them. Combo math =
    # `Bets[Type=='Combo'].TrueOdds`, which already includes the operator's
    # `ComboBonus.Percent` boost.
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
    bscode_updates: list = []  # candidates that got a fresh bscode this cycle
    for cand in published:
        game = games_by_id.get(cand.game_id)
        # Cross-event combos use game_id as a "primary fixture" pointer only;
        # if it happens to be missing from the current catalogue we still
        # want to render the card from its legs. For singles / BBs the game
        # is required.
        is_cross_event_combo = (cand.bet_type == _BetType.COMBO and len(cand.selection_ids) >= 2)
        if game is None and not is_cross_event_combo:
            continue
        market_id = cand.market_ids[0] if cand.market_ids else None
        market = catalog.get(market_id) if market_id else None
        if market is None and not is_cross_event_combo:
            continue
        news = await candidate_store.get_news_item(cand.news_item_id) if cand.news_item_id else None

        # Build the exclusion set for Goalscorer trims (2026-04-23).
        # ANY news item for this fixture that flags a player as out /
        # suspended contributes to the exclusion list. We gather across
        # fixture-scoped news (not just the current candidate's) because
        # card 9 on 2026-04-23 ("Lejeune watching from the stands" +
        # Lejeune @ 5.20 goalscorer) fired when the goalscorer story
        # and the injury story had different news_item_ids.
        excluded_player_names: set = set()
        is_goalscorer_market = (market.market_type == "goalscorer")
        has_goalscorer_leg = any(
            (catalog.get(mid) and catalog.get(mid).market_type == "goalscorer")
            for mid in cand.market_ids
        )
        if is_goalscorer_market or has_goalscorer_leg:
            from app.engine.candidate_builder import _normalize_name as _norm_name
            fixture_news: list = []
            if news is not None:
                fixture_news.append(news)
            # Pull every other news item resolved to this fixture. Small N
            # per fixture (per-fixture cap means ~3 candidates).
            for other in published:
                if other.game_id != cand.game_id or other.news_item_id == cand.news_item_id:
                    continue
                if other.news_item_id:
                    other_news = await candidate_store.get_news_item(other.news_item_id)
                    if other_news is not None:
                        fixture_news.append(other_news)
            for n in fixture_news:
                for d in (n.injury_details or []):
                    if not isinstance(d, dict):
                        continue
                    if not d.get("is_out_confirmed"):
                        continue
                    pname = _norm_name(str(d.get("player_name") or ""))
                    if pname:
                        excluded_player_names.add(pname)

        # Render-time goalscorer trim. Catalogue keeps every player so the
        # engine can match by name; the publisher narrows to either the
        # matched player (when CandidateBuilder hinted via selection_ids)
        # or the top-N favourites. Skip for BB legs — those resolve via
        # selection_ids per leg below.
        if cand.bet_type == _BetType.SINGLE and market is not None and market.market_type == "goalscorer":
            wanted_sid = cand.selection_ids[0] if cand.selection_ids else None
            if wanted_sid:
                matched = next((s for s in market.selections if s.selection_id == wanted_sid), None)
                if matched is not None:
                    market = market.model_copy(update={"selections": [matched]})
            else:
                from app.engine.candidate_builder import _normalize_name as _norm_name
                # Filter out any selection whose label matches an excluded
                # (out / suspended) player before taking the top-N.
                filtered = [
                    s for s in market.selections
                    if not _label_contains_excluded_player(s.label, excluded_player_names)
                ]
                market = market.model_copy(update={"selections": list(filtered[:GOALSCORER_DEFAULT_TOP_N])})

        # Resolve leg markets for Bet Builder AND cross-event combo candidates.
        # Both carry `selection_ids` + `market_ids` per leg; only difference is
        # how the frontend badges them (bet_type = "bet_builder" vs "combo").
        is_bb = cand.bet_type == _BetType.BET_BUILDER and len(cand.selection_ids) >= 2
        is_combo = cand.bet_type == _BetType.COMBO and len(cand.selection_ids) >= 2
        legs: list[_CardLeg] = []
        total_odds: "float | None" = None
        bb_excluded = False
        # Cap how many Goalscorer-leg substitutions we'll try per BB when
        # the originally-picked player is on the out/suspended list. 3 is
        # enough to work down to the 3rd-shortest-odds alternative — if
        # we can't find a non-excluded player in the top 3, the market
        # itself is probably a bad fit. PR #33 feedback point 3 (2026-04-23).
        _MAX_SCORER_SUBS = 3
        if is_bb or is_combo:
            # Mutable copy so we can rewrite the selection id when we swap
            # a Goalscorer leg's player (BB path only). Cross-event combos
            # iterate without substitution.
            cand_selection_ids = list(cand.selection_ids)
            cand_market_ids = list(cand.market_ids)
            for leg_idx, (mid, sid) in enumerate(zip(cand_market_ids, cand_selection_ids)):
                leg_market = catalog.get(mid)
                if leg_market is None:
                    continue
                leg_sel = next((s for s in leg_market.selections if s.selection_id == sid), None)
                if leg_sel is None:
                    continue
                # Exclude-player retry for Goalscorer legs: if the scorer
                # this BB was built with turns out to be on the out/
                # suspended list (catches the PR #20 carry-through where a
                # Lejeune-injury news item and a Lejeune-scorer BB shipped
                # together), walk down the Goalscorer market's selection
                # list (price-ordered at ingest time) and pick the first
                # player who isn't on the exclusion list. Cap at
                # _MAX_SCORER_SUBS substitutions before dropping the BB.
                if (
                    is_bb
                    and leg_market.market_type == "goalscorer"
                    and _label_contains_excluded_player(leg_sel.label, excluded_player_names)
                ):
                    substitute = None
                    # Build the ordered candidate pool of same-market
                    # selections we haven't already rejected. Skip the
                    # originally-picked sid and anything else already
                    # used elsewhere in this BB (avoid duplicate legs).
                    already_used = set(cand_selection_ids)
                    tried = 0
                    for alt in leg_market.selections:
                        if alt.selection_id == sid:
                            continue
                        if not alt.selection_id:
                            continue
                        if alt.selection_id in already_used:
                            continue
                        if tried >= _MAX_SCORER_SUBS:
                            break
                        tried += 1
                        if _label_contains_excluded_player(alt.label, excluded_player_names):
                            continue
                        substitute = alt
                        break
                    if substitute is None:
                        logger.info(
                            "[PULSE] BB rejected — goalscorer leg player %r is on "
                            "the out/suspended list for game %s, no substitute in "
                            "top %d non-excluded selections",
                            leg_sel.label, cand.game_id, _MAX_SCORER_SUBS,
                        )
                        bb_excluded = True
                        break
                    logger.info(
                        "[PULSE] BB goalscorer leg swapped — %r (excluded) -> %r "
                        "(game %s)",
                        leg_sel.label, substitute.label, cand.game_id,
                    )
                    leg_sel = substitute
                    sid = substitute.selection_id
                    cand_selection_ids[leg_idx] = sid
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
            # Persist the (possibly substituted) selection_ids back onto
            # the candidate so downstream pricing + the virtual_selection
            # id (re-quoted via calculate_bets when available) match the
            # legs we actually rendered.
            if not bb_excluded:
                cand.selection_ids = cand_selection_ids
            if bb_excluded:
                # BB carried an out-player goalscorer leg — drop the card
                # entirely. Better to publish nothing than a self-contradicting
                # pick. Persist the rejection for the admin table.
                cand.status = _CandidateStatus.REJECTED
                cand.threshold_passed = False
                cand.reason = (cand.reason + " | gate: goalscorer leg is excluded player").strip(" |")
                gated_updates.append(cand)
                gate_rejected += 1
                gate_reject_reasons["excluded_goalscorer_player"] = (
                    gate_reject_reasons.get("excluded_goalscorer_player", 0) + 1
                )
                continue
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
        # Cross-event storyline combos skip the rewriter entirely — their
        # narrative was authored fresh by CombinedNarrativeAuthor upstream
        # and lives in cand.narrative as "headline\nangle".
        if is_cross_event_combo:
            parts = (cand.narrative or "").split("\n", 1)
            final_headline = parts[0].strip() if parts else (cand.narrative or "")
            final_angle = parts[1].strip() if len(parts) > 1 else ""
        else:
            final_headline = news.headline if news else (cand.narrative or "")
            final_angle = news.summary if news else (cand.narrative or "")
        if rewriter is not None and news is not None and not is_cross_event_combo:
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
        # them for tuning. Cross-event combos skip the gate: the
        # fixture-attribution check assumes one fixture per card, and the
        # storyline author's output has already been prompt-constrained
        # (no "back the X" / "lock" etc).
        if is_cross_event_combo:
            passes, gate_reason = True, None
        else:
            # For the self-consistency gate we need the primary selection
            # (the one whose outcome_type the card ultimately backs).
            # Singles: first selection on the trimmed market. BBs: first
            # leg whose selection resolves on the primary market (legs[0]
            # is market_result-ish when present because of theme ordering).
            primary_selection = None
            if not is_bb and market.selections:
                primary_selection = market.selections[0]
            elif is_bb and legs:
                first_leg = legs[0]
                leg_market = catalog.get(cand.market_ids[0]) if cand.market_ids else None
                if leg_market is not None:
                    primary_selection = next(
                        (s for s in leg_market.selections if s.selection_id == first_leg.selection_id),
                        None,
                    )
            passes, gate_reason = _apply_gates(
                cand,
                headline=final_headline or "",
                angle=final_angle or "",
                game=game,
                legs=legs if is_bb else None,
                total_odds=total_odds if is_bb else None,
                primary_selection=primary_selection,
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
        if is_cross_event_combo:
            badge = BadgeType.TRENDING
        # Ensure we have representative game + market args for the assembler.
        # For cross-event combos, both are nominal (renderer uses `legs`), so
        # fall through to the first leg's fixture/market if the candidate's
        # own game_id / market_id didn't resolve.
        assembler_game = game
        assembler_market = market
        if is_cross_event_combo and legs:
            first_leg_sid = legs[0].selection_id
            for mid in cand.market_ids:
                m_for_leg = catalog.get(mid)
                if m_for_leg is None:
                    continue
                if any(s.selection_id == first_leg_sid for s in m_for_leg.selections):
                    assembler_market = assembler_market or m_for_leg
                    assembler_game = assembler_game or games_by_id.get(m_for_leg.game_id)
                    break
        if assembler_game is None or assembler_market is None:
            # Shouldn't happen, but don't crash the whole publish loop if it does.
            logger.warning("[PULSE] publish: could not resolve game/market for cand=%s, skipping", cand.id)
            continue
        card = assembler.assemble_prematch(
            game=assembler_game, market=assembler_market, narrative=final_angle or "",
            badge=badge, relevance=cand.score, stats=[], tweets=[],
        )
        card.hook_type = cand.hook_type.value
        card.headline = final_headline
        card.source_name = news.source_name if news else None
        ingested = news.ingested_at if news else cand.created_at
        if ingested:
            card.ago_minutes = max(0, int((_time.time() - ingested) / 60))
        if is_combo:
            card.legs = legs
            # Combo total from calculate_bets (Bets[Type='Combo'].TrueOdds).
            # Naive products are fine to show for cross-event combos (no
            # correlation to worry about) but keep gated behind price_source.
            if cand.price_source == "rogue_calculate_bets":
                card.total_odds = total_odds
            else:
                card.total_odds = None
            card.bet_type = "combo"
            card.virtual_selection = None
            # Storyline marker drives the "Weekend Storyline" badge swap
            # on the frontend. Only set on cross-event combos produced
            # by CrossEventBuilder.
            card.storyline_id = cand.storyline_id
        elif is_bb:
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
            card.virtual_selection = cand.virtual_selection
        # Stage 5b — reuse a previously-minted bscode if the candidate
        # already carries one (we persist it to the store so reruns don't
        # re-mint), otherwise mint fresh. Both code paths end with
        # card.bscode set (or None → selectionId fallback).
        if cand.bscode:
            card.bscode = cand.bscode
            card.deep_link = None  # force rebuild to the bscode URL
            _attach_deep_link(card)
        else:
            await _mint_and_stamp(card)
            if card.bscode:
                cand.bscode = card.bscode
                bscode_updates.append(cand)
        target_feed.add_prematch_card(card)
        # Staggered publish hook — fires on every successful insert. Tier
        # loops wire this to `feed.broadcast_card_added` so the frontend
        # drops each new card into the feed one-by-one rather than waiting
        # for a 4h atomic swap. Errors never block the publish.
        if per_card_publish_hook is not None:
            try:
                await per_card_publish_hook(card)
            except Exception as exc:
                logger.warning(
                    "%s[PULSE] per-card publish hook errored (ignored): %s",
                    _tier_prefix, exc,
                )

    if rewriter is not None:
        logger.info("[PULSE] NarrativeRewriter: %d hits, %d misses (fell back to scout copy)",
                    rewrite_hit, rewrite_miss)
        # U3 cache summary — separate from the rewrite_hit/miss counters
        # above (which track whether a Sonnet response was successfully
        # applied). cache_hits here = rewrites served from SQLite without
        # calling Sonnet at all.
        # cache_misses here = real Sonnet rewrite calls; feed it into the
        # cycle cost telemetry.
        try:
            _bump_cycle_counter("rewrite_sonnet", int(rewriter.cache_misses))
        except Exception:
            pass
        logger.info(
            "[PULSE] rewrite cache: %d hits, %d misses this cycle",
            rewriter.cache_hits, rewriter.cache_misses,
        )
    if gate_rejected:
        # Persist the REJECTED status change so the admin table reflects
        # what got blocked and why.
        await candidate_store.save_candidates(gated_updates)
        logger.info("[PULSE] Quality gates rejected %d candidates — reasons: %s",
                    gate_rejected, gate_reject_reasons)
    if bscode_updates:
        # Persist freshly-minted bscodes so the next rerun reads them from
        # the store instead of re-minting (mint is idempotent per
        # selection-set, but avoiding the POST is cheaper and politer to
        # Kmianko). INSERT OR REPLACE upserts — safe to rewrite same rows.
        try:
            await candidate_store.save_candidates(bscode_updates)
            logger.info("[PULSE] Persisted %d freshly-minted bscodes", len(bscode_updates))
        except Exception as exc:
            # Persistence is best-effort — a failed save just means we
            # re-mint next cycle.
            logger.warning("[PULSE] Failed to persist bscodes: %s", exc)


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
