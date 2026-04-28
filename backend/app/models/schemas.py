from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
import uuid
import time


class Sport(str, Enum):
    NBA = "nba"
    NFL = "nfl"
    EPL = "epl"
    MLB = "mlb"
    SOCCER = "soccer"  # generic soccer fixtures sourced from Rogue across international leagues


class GameStatus(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"


class MarketStatus(str, Enum):
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class CardType(str, Enum):
    PRE_MATCH = "pre_match"
    LIVE_EVENT = "live_event"
    SOCIAL_SIGNAL = "social_signal"


class BadgeType(str, Enum):
    TRENDING = "trending"
    MILESTONE = "milestone"
    HOT = "hot"
    STAT = "stat"
    NEWS = "news"


# ── Core Entities ──

class Team(BaseModel):
    id: str
    name: str
    short_name: str
    color: str
    sport: Sport


class Player(BaseModel):
    id: str
    name: str
    team_id: str
    position: str
    season_stats: dict = {}
    career_stats: dict = {}


class Game(BaseModel):
    id: str
    sport: Sport
    home_team: Team
    away_team: Team
    status: GameStatus = GameStatus.SCHEDULED
    home_score: int = 0
    away_score: int = 0
    clock: str = ""  # e.g., "Q3 8:22" or "62'"
    period: str = ""  # e.g., "Q3" or "2H"
    broadcast: str = ""
    start_time: str = ""  # e.g., "7:30 PM ET"
    # Whether the operator has enabled Bet Builder on this fixture. Sourced
    # from `Settings.IsBetBuilderEnabled` on the Rogue Event payload at
    # catalogue-load time. Defaults to False so absence is conservative —
    # the HOT-tier classifier's BB filter rejects rather than passes when
    # the flag is missing. Toggle off via `PULSE_HOT_REQUIRE_BB_ENABLED=false`
    # if Rogue mis-reports.
    is_bet_builder_enabled: bool = False


# ── Market ──

class MarketSelection(BaseModel):
    label: str  # e.g., "Over 27.5", "Arsenal"
    odds: str  # e.g., "-115", "+260"
    previous_odds: Optional[str] = None
    # Rogue selection ID — required for Bet Builder validation via
    # /v1/sportsdata/betbuilder/match. Optional because mock data and other
    # sources won't have one.
    selection_id: Optional[str] = None
    # Outcome classification from Rogue — "Home" / "Away" / "Draw" / "Over" /
    # "Under" / "Yes" / "No" etc. Used by the ComboBuilder to pick the right
    # leg per hook theme without parsing labels.
    outcome_type: Optional[str] = None


class Market(BaseModel):
    id: str
    game_id: str
    market_type: str  # "spread", "over_under", "moneyline", "player_prop", "match_result"
    label: str  # e.g., "LeBron Points O/U 27.5"
    player_id: Optional[str] = None
    team_id: Optional[str] = None
    stat_type: Optional[str] = None  # "points", "touchdowns", "goals"
    line: Optional[float] = None
    selections: list[MarketSelection] = []
    status: MarketStatus = MarketStatus.OPEN


# ── Context / Evidence ──

class Tweet(BaseModel):
    id: str
    author_name: str
    author_handle: str
    author_avatar: str = ""  # single letter or emoji
    body: str
    time_ago: str = "2h ago"
    player_ids: list[str] = []
    team_ids: list[str] = []
    game_id: Optional[str] = None


class NewsItem(BaseModel):
    source: str
    title: str
    time_ago: str
    icon: str = ""
    player_ids: list[str] = []
    team_ids: list[str] = []


class StatDisplay(BaseModel):
    label: str
    value: str
    color: Optional[str] = None  # "green", "orange", "red", "accent"


class ProgressDisplay(BaseModel):
    label: str
    current: float
    target: float
    fill_color: str = "green"  # CSS color class


# ── Events ──

class EventType(str, Enum):
    SCORE_CHANGE = "score_change"
    STAT_UPDATE = "stat_update"
    THRESHOLD_APPROACH = "threshold_approach"
    MOMENTUM_SHIFT = "momentum_shift"
    MILESTONE = "milestone"
    INJURY = "injury"


class GameEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    game_id: str
    event_type: EventType
    player_id: Optional[str] = None
    team_id: Optional[str] = None
    description: str = ""
    data: dict = {}  # flexible payload
    timestamp: float = Field(default_factory=time.time)


# ── Feed Card (the output) ──

class CardLeg(BaseModel):
    """One leg of a multi-selection card (Bet Builder or combo).

    For singles, we leave `legs` empty on the Card and let the frontend render
    from `market.selections` as before. Legs carry enough data for the design
    pack's stacked-pick block: a label, optional sub-line, and decimal odds.
    """
    label: str
    sub: Optional[str] = None
    odds: float = 0.0
    market_label: Optional[str] = None       # "FT 1X2", "Total Goals O/U"
    selection_id: Optional[str] = None       # Rogue selection id (for deep-link)


class Card(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    card_type: CardType
    game: Game
    badge: Optional[BadgeType] = None
    event_trigger: Optional[dict] = None  # {icon, what, when}
    narrative_hook: str = ""
    stats: list[StatDisplay] = []
    progress: Optional[ProgressDisplay] = None
    tweets: list[Tweet] = []
    news: list[NewsItem] = []
    market: Optional[Market] = None
    relevance_score: float = 0.0
    created_at: float = Field(default_factory=time.time)
    ttl_seconds: int = 600  # 10 min default, shorter for live
    # Stamp set when the card is first published to the live feed. Used by
    # the tiered-freshness TTL sweep (PULSE_CARD_TTL_SECONDS) and the
    # frontend "NEW" marker / relative-time label. `created_at` isn't
    # enough because cached scout cycles re-use Card objects minted in a
    # prior cycle — published_at is cycle-scoped.
    published_at: Optional[float] = None

    # Stage 2 design handoff — populated for news-driven cards so the Hero
    # variant can render source / recency / hook styling without joining back
    # to the candidate store.
    hook_type: Optional[str] = None          # "injury", "team_news", "tactical", ...
    headline: Optional[str] = None           # short news headline
    source_name: Optional[str] = None        # "BBC Sport", "Sky Sports"
    source_handle: Optional[str] = None      # "@SkySportsNews"
    ago_minutes: Optional[int] = None        # minutes since news was published/ingested

    # Stage 3 — multi-leg cards (Bet Builder / combo). When legs is non-empty,
    # the frontend stacks them in the Pulse Pick block and shows total_odds
    # instead of the single selection's odds.
    legs: list[CardLeg] = []
    total_odds: Optional[float] = None
    bet_type: str = "single"                 # "single" | "combo" | "bet_builder"
    # SSE live-pricing — Rogue VirtualSelection id for BBs (re-fed into
    # /v1/betting/calculateBets when a leg ticks). None for singles/combos.
    virtual_selection: Optional[str] = None
    # Cross-event storyline marker. Set when this card was produced from
    # a persisted StorylineItem (Golden Boot race, etc.). Frontend swaps
    # the "Bet Builder" / "Combo" label for "Weekend Storyline" when this
    # is non-null.
    storyline_id: Optional[str] = None
    # Suspension state — set true when the SSE feed reports the event or any
    # leg market suspends. Frontend grays the CTA.
    suspended: bool = False
    # Stage 5 deep-link — operator bet-slip URL with this card's selection(s)
    # pre-loaded. Built server-side from env templates so the frontend stays
    # operator-agnostic (client just `window.open`s whatever lands in this
    # field). None => CTA renders dead (e.g. cross-event combo on an operator
    # whose slip URL can't encode multi-event selections).
    deep_link: Optional[str] = None
    # Stage 5b — server-minted bscode. 6-char code from kmianko's
    # share-betslip endpoint; when present, `deep_link` is the bscode
    # variant which restores the full slip (single / BB / combo) verbatim.
    # None when the minter is disabled, selection_ids is empty, or the
    # mint call failed — in that case `deep_link` falls back to the PR #36
    # selectionId URL. Exposed so admin tooling can inspect.
    bscode: Optional[str] = None
