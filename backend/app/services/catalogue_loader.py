"""Catalogue loader — pulls real pre-match soccer fixtures from Rogue at boot.

Scope (Stage 1):
  - Soccer only.
  - Pre-match fixtures (no live events yet).
  - International top leagues only (EPL, La Liga, Bundesliga, Serie A, Ligue 1,
    UCL, Europa) — identified by league-name match since Rogue's LeagueIds are
    per-operator.
  - Deep-scan each fixture with `includeMarkets=default` to get main markets.
  - Map to internal Game + Market objects.

Kept narrow on purpose — additional market coverage, bet-builder flags, and
context enrichment land in later stages.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.models.schemas import (
    Game,
    GameStatus,
    Market,
    MarketSelection,
    MarketStatus,
    Sport,
    Team,
)
from app.services.rogue_client import RogueClient

logger = logging.getLogger(__name__)


INTERNATIONAL_LEAGUE_PATTERNS = [
    # EPL
    "premier league",
    "english premier",
    # La Liga
    "laliga", "la liga",
    # Bundesliga
    "bundesliga",
    # Serie A
    "serie a",
    # Ligue 1
    "ligue 1",
    # UEFA club competitions
    "champions league",
    "europa league",
    "europa conference",
]

# Hardcoded colors for big clubs so cards don't all render neutral gray.
# Fallback colors below for everything else.
CLUB_COLORS: dict[str, str] = {
    "arsenal": "#EF0107",
    "chelsea": "#034694",
    "liverpool": "#C8102E",
    "manchester city": "#6CABDD",
    "manchester united": "#DA291C",
    "tottenham": "#132257",
    "real madrid": "#FEBE10",
    "barcelona": "#A50044",
    "atletico madrid": "#CB3524",
    "bayern munich": "#DC052D",
    "borussia dortmund": "#FDE100",
    "juventus": "#000000",
    "inter milan": "#0068A8",
    "ac milan": "#FB090B",
    "paris saint-germain": "#004170",
    "psg": "#004170",
}

DEFAULT_HOME_COLOR = "#1a365d"
DEFAULT_AWAY_COLOR = "#2d3748"


def _league_matches(name: str) -> bool:
    n = (name or "").lower()
    return any(p in n for p in INTERNATIONAL_LEAGUE_PATTERNS)


def _short_name(name: str) -> str:
    """Generate a 3-letter short code for a team. Best-effort, not canonical."""
    if not name:
        return "???"
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if len(words) == 1:
        return words[0][:3].upper()
    # Take first letter of up to three words
    return "".join(w[0] for w in words[:3]).upper()


def _color_for(name: str, fallback: str) -> str:
    key = (name or "").lower().strip()
    if key in CLUB_COLORS:
        return CLUB_COLORS[key]
    # Crude alias match — "Manchester City FC" → "manchester city"
    for alias, color in CLUB_COLORS.items():
        if alias in key:
            return color
    return fallback


def _participants(event: dict[str, Any]) -> tuple[Optional[dict], Optional[dict]]:
    parts = event.get("Participants") or []
    home = next((p for p in parts if str(p.get("VenueRole") or "").lower() == "home"), None)
    away = next((p for p in parts if str(p.get("VenueRole") or "").lower() == "away"), None)
    if home is None and away is None and len(parts) >= 2:
        home, away = parts[0], parts[1]
    elif home is None and len(parts) >= 1:
        home = parts[0]
    elif away is None and len(parts) >= 2:
        away = parts[1]
    return home, away


def _start_time(event: dict[str, Any]) -> str:
    raw = event.get("StartEventDate")
    if not raw:
        return ""
    try:
        # Rogue dates are ISO-8601 with timezone.
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")
    except Exception:
        return str(raw)


def _participant_name(part: Optional[dict]) -> str:
    if not part:
        return ""
    name = part.get("Name")
    if isinstance(name, dict):
        return str(name.get("EN") or next(iter(name.values()), ""))
    return str(name or "")


def _display_odds(selection: dict[str, Any]) -> str:
    """Extract a display string from a Rogue selection.

    DisplayOdds may be:
      - a dict like {"Decimal": "1.95", "American": "-105", ...}
      - a plain string
      - missing (fall back to TrueOdds which is decimal)
    """
    disp = selection.get("DisplayOdds")
    if isinstance(disp, dict):
        for fmt in ("Decimal", "American", "Fractional"):
            val = disp.get(fmt)
            if val:
                return str(val)
    if isinstance(disp, str) and disp:
        return disp
    true_odds = selection.get("TrueOdds")
    if true_odds is not None:
        try:
            return f"{float(true_odds):.2f}"
        except Exception:
            return str(true_odds)
    return ""


# Map Rogue market names to our internal market_type taxonomy.
#
# We whitelist by exact (case-insensitive) name rather than regex because
# Rogue has many variants (`1st Half 1X2`, `FT 1X2 - Super Odds`,
# `First To Score 1X2`, `Total Corners O/U`, etc.) that we do NOT want to
# surface as the primary pre-match market. Only the full-time standard
# market of each type is accepted.
MATCH_RESULT_NAMES = {
    "ft 1x2",
    "ft match result",
    "match result",
    "full time result",
}

TOTAL_GOALS_NAMES = {
    "total goals o/u",
    "ft total goals",
    "total goals",
    "total goals over/under",
}

BTTS_NAMES = {
    "both teams to score",
    "btts",
}

DOUBLE_CHANCE_NAMES = {
    "double chance",
    "ft double chance",
}


def _classify_market(name: str, raw_type: str) -> str:
    key = (name or "").strip().lower()
    if key in MATCH_RESULT_NAMES:
        return "match_result"
    if key in TOTAL_GOALS_NAMES:
        return "over_under"
    if key in BTTS_NAMES:
        return "btts"
    if key in DOUBLE_CHANCE_NAMES:
        return "double_chance"
    return "other"


# Markets we surface on pre-match cards for Stage 1. Asian Handicap /
# correct score / player props require richer card shapes — deferred.
ALLOWED_MARKET_TYPES = {
    "match_result",
    "over_under",
    "btts",
    "double_chance",
}


def _selection_name(sel: dict[str, Any]) -> str:
    name = sel.get("Name")
    if isinstance(name, dict):
        name = name.get("EN") or next(iter(name.values()), "")
    return str(name or "").strip()


def _active_selections(market: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (market.get("Selections") or []) if not s.get("IsDisabled")]


_MATCH_RESULT_ORDER = {"home": 0, "draw": 1, "x": 1, "tie": 1, "away": 2}
_BTTS_ORDER = {"yes": 0, "no": 1}
_OU_ORDER = {"over": 0, "under": 1}


def _sort_selections(raw: list[dict[str, Any]], market_type: str) -> list[dict[str, Any]]:
    def key(sel: dict[str, Any]) -> int:
        side = (sel.get("OutcomeType") or "").strip().lower()
        name = _selection_name(sel).lower()
        if market_type == "match_result":
            # Try OutcomeType first, then fall back to selection name
            # (Rogue's draw selection has Name="X" but no consistent OutcomeType).
            if side in _MATCH_RESULT_ORDER:
                return _MATCH_RESULT_ORDER[side]
            if name in _MATCH_RESULT_ORDER:
                return _MATCH_RESULT_ORDER[name]
            return 99
        if market_type == "btts":
            return _BTTS_ORDER.get(name, 99)
        if market_type == "over_under":
            return _OU_ORDER.get(side, 99)
        return 99
    return sorted(raw, key=key)


def _selections(market: dict[str, Any], market_type: str) -> list[MarketSelection]:
    raw = _active_selections(market)
    if market_type == "over_under":
        raw = _pick_main_total_line(raw)
    raw = _sort_selections(raw, market_type)
    out: list[MarketSelection] = []
    for sel in raw:
        label = _selection_name(sel)
        if not label:
            continue
        # For totals, make the label explicit ("Over 2.5" / "Under 2.5").
        if market_type == "over_under" and sel.get("Points") is not None:
            label = f"{label} {sel['Points']}"
        out.append(MarketSelection(
            label=label,
            odds=_display_odds(sel),
            selection_id=str(sel.get("_id") or "") or None,
            outcome_type=str(sel.get("OutcomeType") or "") or None,
        ))
    return out


def _pick_main_total_line(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a multi-line Total Goals O/U market to a single line pair.

    Strategy: pick the (Over, Under) pair whose odds are closest to a true
    coin flip (difference between decimal odds minimised). That's almost
    always the 2.5-ish line for soccer.
    """
    by_points: dict[Any, dict[str, dict[str, Any]]] = {}
    for sel in selections:
        points = sel.get("Points")
        if points is None:
            continue
        side = (sel.get("OutcomeType") or "").strip().lower()
        if side not in ("over", "under"):
            continue
        by_points.setdefault(points, {})[side] = sel

    best: list[dict[str, Any]] | None = None
    best_gap = float("inf")
    for points, pair in by_points.items():
        if "over" not in pair or "under" not in pair:
            continue
        try:
            o = float(pair["over"].get("TrueOdds") or 0)
            u = float(pair["under"].get("TrueOdds") or 0)
        except Exception:
            continue
        gap = abs(o - u)
        if gap < best_gap:
            best_gap = gap
            best = [pair["over"], pair["under"]]
    return best or selections[:2]


def _map_event_to_game(event: dict[str, Any]) -> Optional[Game]:
    event_id = event.get("_id")
    home, away = _participants(event)
    home_name = _participant_name(home)
    away_name = _participant_name(away)
    if not event_id or not home_name or not away_name:
        return None

    home_team = Team(
        id=f"rogue_{home.get('_id') or home_name.lower().replace(' ', '_')}",
        name=home_name,
        short_name=_short_name(home_name),
        color=_color_for(home_name, DEFAULT_HOME_COLOR),
        sport=Sport.SOCCER,
    )
    away_team = Team(
        id=f"rogue_{away.get('_id') or away_name.lower().replace(' ', '_')}",
        name=away_name,
        short_name=_short_name(away_name),
        color=_color_for(away_name, DEFAULT_AWAY_COLOR),
        sport=Sport.SOCCER,
    )

    return Game(
        id=str(event_id),
        sport=Sport.SOCCER,
        home_team=home_team,
        away_team=away_team,
        status=GameStatus.SCHEDULED,
        home_score=0,
        away_score=0,
        clock="",
        period="",
        broadcast=str(event.get("LeagueName") or ""),
        start_time=_start_time(event),
    )


def _map_event_to_markets(event: dict[str, Any]) -> list[Market]:
    event_id = str(event.get("_id") or "")
    if not event_id:
        return []
    raw_markets = event.get("Markets") or []
    out: list[Market] = []
    for m in raw_markets:
        if m.get("IsSuspended"):
            status = MarketStatus.SUSPENDED
        else:
            status = MarketStatus.OPEN
        name_raw = m.get("Name")
        if isinstance(name_raw, dict):
            name = str(name_raw.get("EN") or next(iter(name_raw.values()), ""))
        else:
            name = str(name_raw or "")
        raw_type = str(m.get("MarketType") or "")
        market_type = _classify_market(name, raw_type)
        if market_type not in ALLOWED_MARKET_TYPES:
            continue
        selections = _selections(m, market_type)
        if not selections:
            continue
        out.append(
            Market(
                id=str(m.get("_id") or f"{event_id}:{name}"),
                game_id=event_id,
                market_type=market_type,
                label=name or raw_type or market_type.replace("_", " ").title(),
                line=_extract_line(selections, market_type),
                selections=selections,
                status=status,
            )
        )
    return out


def _extract_line(selections: list[MarketSelection], market_type: str) -> Optional[float]:
    if market_type != "over_under":
        return None
    for s in selections:
        match = re.search(r"[-+]?\d+(?:\.\d+)?", s.label)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                continue
    return None


async def fetch_soccer_snapshot(
    client: RogueClient,
    *,
    sport_id: str,
    days_ahead: int,
    max_events: int,
) -> tuple[list[Game], list[Market], list[dict[str, Any]]]:
    """Fetch top-league international soccer fixtures and map to internal types.

    Returns (games, markets, raw_events) so callers can log / debug.
    """
    # Rogue accepts "YYYY-MM-DDTHH:MM:SSZ" — microseconds or +00:00 suffix are rejected.
    _fmt = "%Y-%m-%dT%H:%M:%SZ"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    from_date = now_utc.strftime(_fmt)
    to_date = (now_utc + timedelta(days=days_ahead)).strftime(_fmt)

    logger.info("Rogue: fetching soccer fixtures %s → %s", from_date, to_date)
    candidates = await client.get_all_events(
        max_events=max_events * 4,  # over-fetch; many won't match the league filter
        sport_ids=sport_id,
        event_type="Fixture",
        is_live=False,
        is_top_league=True,
        from_date=from_date,
        to_date=to_date,
        include_markets="none",
    )
    logger.info("Rogue: received %d top-league soccer candidates", len(candidates))

    filtered = [e for e in candidates if _league_matches(str(e.get("LeagueName") or ""))]
    filtered.sort(key=lambda e: str(e.get("StartEventDate") or ""))
    filtered = filtered[:max_events]
    logger.info("Rogue: %d fixtures match international leagues", len(filtered))

    games: list[Game] = []
    markets: list[Market] = []
    raw_detailed: list[dict[str, Any]] = []

    for shallow in filtered:
        eid = shallow.get("_id")
        if not eid:
            continue
        try:
            full = await client.get_event(str(eid), include_markets="default", locale="en")
        except Exception as exc:
            logger.warning("Rogue: deep scan failed for %s: %s", eid, exc)
            continue
        if full is None:
            logger.info("Rogue: event %s returned 204 (settled/ended) — skipping", eid)
            continue
        raw_detailed.append(full)

        game = _map_event_to_game(full)
        if game is None:
            logger.debug("Rogue: could not map event %s to Game", eid)
            continue
        games.append(game)
        markets.extend(_map_event_to_markets(full))

    logger.info("Rogue: loaded %d games and %d markets", len(games), len(markets))
    return games, markets, raw_detailed
