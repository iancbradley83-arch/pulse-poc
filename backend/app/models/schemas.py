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


# ── Market ──

class MarketSelection(BaseModel):
    label: str  # e.g., "Over 27.5", "Arsenal"
    odds: str  # e.g., "-115", "+260"
    previous_odds: Optional[str] = None


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
