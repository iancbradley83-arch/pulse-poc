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
