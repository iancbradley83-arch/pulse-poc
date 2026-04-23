"""News-driven recommendation engine schemas.

Two core persisted objects:

  - NewsItem: a single real-world signal that could make a market interesting.
    Triggered by the ingester (LLM + web search per fixture in Stage 2).

  - CandidateCard: a proposed feed card built from a news item + matched
    market(s). Scored and persisted whether or not it passes the publish
    threshold — the admin /candidates table reads from this.

Lightweight Pydantic models; persistence is a thin SQLite layer in
`app/services/candidate_store.py`. Wire-format backwards compatibility is not
a concern yet (POC, no external consumers).
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class HookType(str, Enum):
    """What real-world signal triggered this news item.

    Sourced from ingester; the same enum is used on CandidateCard so we can
    filter the admin table by hook type without joining.
    """
    INJURY = "injury"
    TEAM_NEWS = "team_news"        # starting XI, formation, suspensions, pressers
    TRANSFER = "transfer"
    MANAGER_QUOTE = "manager_quote"
    TACTICAL = "tactical"
    PREVIEW = "preview"            # pre-match preview article
    ARTICLE = "article"            # generic editorial / news
    PRICE_MOVE = "price_move"      # market-internal — Stage 6+
    LIVE_MOMENT = "live_moment"    # Stage 7+
    FEATURED = "featured"          # operator-curated (Rogue /v1/featured/betbuilder)
    OTHER = "other"


class BetType(str, Enum):
    SINGLE = "single"
    COMBO = "combo"
    BET_BUILDER = "bet_builder"


class CandidateStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    PUBLISHED = "published"
    REJECTED = "rejected"
    EXPIRED = "expired"


class NewsItem(BaseModel):
    id: str = Field(default_factory=lambda: f"news_{uuid.uuid4().hex[:12]}")
    source: str = "llm_web_search"
    source_url: str = ""
    source_name: str = ""
    headline: str = ""
    summary: str = ""
    hook_type: HookType = HookType.OTHER
    published_at: str = ""              # ISO 8601 from the news source (if known)
    ingested_at: float = Field(default_factory=time.time)

    # Raw strings from the ingester (team / player / coach names). Entity
    # resolver turns these into Rogue IDs; kept verbatim for debugging.
    mentions: list[str] = Field(default_factory=list)

    # Populated by EntityResolver. Empty list = "we couldn't map this to
    # anything in the current Rogue catalogue" — gets filtered out before
    # it becomes a candidate.
    fixture_ids: list[str] = Field(default_factory=list)
    team_ids: list[str] = Field(default_factory=list)

    # Structured position data for INJURY / TEAM_NEWS items. Each entry:
    #   {"player_name": str, "team": str,
    #    "position_guess": "striker"|"winger"|"attacking_mid"|
    #                      "defensive_mid"|"centre_back"|"fullback"|
    #                      "goalkeeper"|"unknown",
    #    "is_out_confirmed": bool}
    # (Legacy: the enum also historically included "midfielder"; the
    # news_ingester parse layer folds that value to "unknown" so old
    # cached rows still deserialize cleanly.)
    # Populated by the scout (news_ingester) when the story names a player
    # who is out / suspended / doubtful. Consumed by:
    #   - candidate_builder INJURY routing (position → market selection)
    #   - publisher Goalscorer trim (exclude out players)
    # Optional — missing/empty list means we fall back to the old behavior.
    injury_details: list[dict] = Field(default_factory=list)


class CandidateCard(BaseModel):
    id: str = Field(default_factory=lambda: f"cand_{uuid.uuid4().hex[:12]}")
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0              # 0 = no expiry; set for match-imminent hooks

    news_item_id: Optional[str] = None   # nullable only for hooks without a news source (e.g. scheduled themes)
    hook_type: HookType = HookType.OTHER
    bet_type: BetType = BetType.SINGLE

    game_id: str = ""                    # Rogue event ID, primary fixture
    market_ids: list[str] = Field(default_factory=list)      # for singles, one entry
    selection_ids: list[str] = Field(default_factory=list)   # populated for combos / BBs

    score: float = 0.0                   # 0..1, from RelevanceScorer
    threshold_passed: bool = False
    reason: str = ""                     # human-readable "why it scored what it did"
    status: CandidateStatus = CandidateStatus.DRAFT

    narrative: str = ""                  # card copy — "Saka returns to full training..."
    supporting_stats_json: str = ""      # Stage 4 fills this (JSON-encoded list[StatDisplay])

    # Multi-leg pricing — populated by ComboBuilder when we successfully fetch a
    # real correlated BB price (or operator-boosted combo price). None means
    # "compute naively downstream".
    total_odds: Optional[float] = None
    # Where `total_odds` came from. One of:
    #   "rogue_calculate_bets" — real correlated/boosted price via Rogue API
    #   "naive"                — pure product, no bonus / no real quote
    #   None                   — total_odds not set
    # Legacy: "kmianko_bb" / "kmianko_combo" from the deprecated bet-slip
    # path. Read paths still tolerate them for back-compat.
    price_source: Optional[str] = None
    # Rogue VirtualSelection id (returned by /v1/sportsdata/betbuilder/match)
    # used for re-pricing the BB via /v1/betting/calculateBets. Persisted so
    # the SSEPricingManager can re-quote on leg ticks without rebuilding the
    # piped id from leg ids each time.
    virtual_selection: Optional[str] = None
