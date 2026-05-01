"""Catalogue loader — pulls real pre-match soccer fixtures from Rogue at boot.

Scope (Stage 1):
  - Soccer only.
  - Pre-match fixtures (no live events yet).
  - International top leagues only (EPL, La Liga, Bundesliga, Serie A, Ligue 1,
    UCL, Europa) — identified by league-name match since Rogue's LeagueIds are
    per-operator.
  - Deep-scan each fixture with `includeMarkets=all` so we can surface richer
    market types (corners, cards, anytime scorer, half-time, AH, DNB) on top of
    the original FT 1X2 / O/U / BTTS / Double Chance set.
  - Map to internal Game + Market objects.

Whitelist by exact market Name (case-insensitive) — Rogue returns ~250 markets
per fixture, most of which are noise (alt lines, cast markets, odd specials).
Only a curated set is surfaced as `Market` objects.
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
# market of each type is accepted, plus the richer half-time / corners /
# cards / scorer / AH / DNB set added in the market-coverage expansion.
#
# A market name in `ALL_MARKET_NAME_MAP` keys is normalised (strip + lower)
# and looked up directly. Use a tuple value when multiple Rogue spellings
# should map to the same internal type; the dict is exhaustive so the
# default branch returns "other".
_NAME_TO_TYPE: dict[str, str] = {
    # Match result (FT 1X2)
    "ft 1x2": "match_result",
    "ft match result": "match_result",
    "match result": "match_result",
    "full time result": "match_result",
    # Full-time goals O/U
    "total goals o/u": "over_under",
    "ft total goals": "over_under",
    "total goals": "over_under",
    "total goals over/under": "over_under",
    "ft o/u": "over_under",
    # BTTS
    "both teams to score": "btts",
    "btts": "btts",
    # Double Chance
    "double chance": "double_chance",
    "ft double chance": "double_chance",
    # ── New (market-coverage expansion) ────────────────────────────────
    # Corners
    "corners ft o/u": "corners_ou",
    "corners ft 1x2": "corners_3way",
    # Cards
    "cards ft o/u": "cards_ou",
    "total cards over/under": "cards_ou",
    "cards ft 1x2": "cards_3way",
    # First half
    "1st half 1x2": "first_half_result",
    "1st half total goals o/u": "first_half_goals_ou",
    # Asian Handicap (FT) — main line
    "ft asian handicap": "asian_handicap",
    # Draw No Bet
    "draw no bet": "draw_no_bet",
    # Anytime scorer
    "goalscorer": "goalscorer",
}


def _classify_market(name: str, raw_type: str) -> str:
    key = (name or "").strip().lower()
    return _NAME_TO_TYPE.get(key, "other")


# Markets we surface on pre-match cards. Correct score / scorecast / player
# 2+ goals require richer card shapes — deferred.
ALLOWED_MARKET_TYPES = {
    "match_result",
    "over_under",
    "btts",
    "double_chance",
    "corners_ou",
    "corners_3way",
    "cards_ou",
    "cards_3way",
    "first_half_result",
    "first_half_goals_ou",
    "asian_handicap",
    "draw_no_bet",
    "goalscorer",
}

# Market types that share the "Over / Under at a single Points line" shape
# and should be collapsed to one main line. Adding a new totals-style market
# type? Add it here too so `_pick_main_ou_line` runs on it.
_OU_STYLE_TYPES = {
    "over_under",
    "corners_ou",
    "cards_ou",
    "first_half_goals_ou",
}

# Market types that share the home/draw/away (or home/away) shape. Sort key
# uses _MATCH_RESULT_ORDER for these.
_3WAY_STYLE_TYPES = {
    "match_result",
    "first_half_result",
    "corners_3way",
    "cards_3way",
}

# Goalscorer markets carry 100+ player selections. We keep all of them in
# the catalogue (sorted favourite-first) so the engine can match a player
# named in news.mentions; the candidate builder trims down before publish.
GOALSCORER_DEFAULT_TOP_N = 6


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
    def key(sel: dict[str, Any]) -> tuple[Any, ...]:
        side = (sel.get("OutcomeType") or "").strip().lower()
        name = _selection_name(sel).lower()
        if market_type in _3WAY_STYLE_TYPES:
            # Try OutcomeType first, then fall back to selection name
            # (Rogue's draw selection has Name="X" but no consistent OutcomeType).
            if side in _MATCH_RESULT_ORDER:
                return (_MATCH_RESULT_ORDER[side],)
            if name in _MATCH_RESULT_ORDER:
                return (_MATCH_RESULT_ORDER[name],)
            return (99,)
        if market_type == "btts":
            return (_BTTS_ORDER.get(name, 99),)
        if market_type in _OU_STYLE_TYPES:
            return (_OU_ORDER.get(side, 99),)
        if market_type == "asian_handicap":
            # Home then Away within a single line.
            return (_MATCH_RESULT_ORDER.get(side, 99),)
        if market_type == "draw_no_bet":
            return (_MATCH_RESULT_ORDER.get(side, 99),)
        if market_type == "goalscorer":
            # Favourite first.
            try:
                return (float(sel.get("TrueOdds") or 99),)
            except (TypeError, ValueError):
                return (99,)
        return (99,)
    return sorted(raw, key=key)


def _selections(market: dict[str, Any], market_type: str) -> list[MarketSelection]:
    raw = _active_selections(market)
    if market_type in _OU_STYLE_TYPES:
        raw = _pick_main_ou_line(raw)
    elif market_type == "asian_handicap":
        raw = _pick_main_ah_line(raw)
    elif market_type == "goalscorer":
        # Sort all players by ascending odds (favourite first). We KEEP the
        # full list so candidate_builder can match a specific player from
        # news.mentions (e.g. "Saka returns" → find the Saka selection
        # even though he's not in the top-6 favourites). The card never
        # publishes the whole list — candidate_builder trims to a single
        # matched player, or to the top-N favourites as a fallback.
        raw = sorted(
            raw,
            key=lambda s: float(s.get("TrueOdds") or 9999),
        )
    raw = _sort_selections(raw, market_type)
    out: list[MarketSelection] = []
    for sel in raw:
        label = _selection_name(sel)
        if not label:
            continue
        # For OU-style, suffix the points so the label is explicit
        # ("Over 2.5", "Over 9" for corners, "Over 4" for cards).
        if market_type in _OU_STYLE_TYPES and sel.get("Points") is not None:
            label = f"{label} {sel['Points']}"
        # For Asian Handicap, suffix the handicap value ("Atletico Madrid -0.5").
        if market_type == "asian_handicap" and sel.get("Points") is not None:
            label = f"{label} {sel['Points']:+g}"
        out.append(MarketSelection(
            label=label,
            odds=_display_odds(sel),
            selection_id=str(sel.get("_id") or "") or None,
            outcome_type=str(sel.get("OutcomeType") or "") or None,
        ))
    return out


def _pick_main_ou_line(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a multi-line Over/Under market to a single line pair.

    Works for any totals-shaped market — goals, corners, cards, half goals.
    Strategy: pick the (Over, Under) pair whose odds are closest to a true
    coin flip (difference between decimal odds minimised). That's almost
    always the main line the operator is balancing.
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


def _pick_main_ah_line(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse a multi-line Asian Handicap market to one (Home, Away) pair.

    Strategy: pick the line whose absolute Points value is smallest (closest
    to a pick-em). On ties prefer the line with home/away odds closest to
    each other. Returns the (home, away) pair sorted by VenueRole.
    """
    by_abs_points: dict[float, dict[str, dict[str, Any]]] = {}
    for sel in selections:
        points = sel.get("Points")
        if points is None:
            continue
        side = (sel.get("OutcomeType") or "").strip().lower()
        if side not in ("home", "away"):
            continue
        try:
            abs_pt = abs(float(points))
        except (TypeError, ValueError):
            continue
        by_abs_points.setdefault(abs_pt, {})[side] = sel

    best: list[dict[str, Any]] | None = None
    best_gap = float("inf")
    for abs_pt in sorted(by_abs_points.keys()):
        pair = by_abs_points[abs_pt]
        if "home" not in pair or "away" not in pair:
            continue
        try:
            h = float(pair["home"].get("TrueOdds") or 0)
            a = float(pair["away"].get("TrueOdds") or 0)
        except Exception:
            continue
        # Prefer the line whose two prices are closest. Within that, smaller
        # abs(handicap) wins (sorted iteration above keeps that monotone).
        gap = abs(h - a)
        if gap < best_gap:
            best_gap = gap
            best = [pair["home"], pair["away"]]
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

    # `Settings.IsBetBuilderEnabled` per Rogue OpenAPI (EventSettingsModel).
    # Default False if Settings absent / missing — conservative; the HOT-tier
    # classifier's BB filter will then drop the fixture rather than letting
    # an unverified row through.
    settings = event.get("Settings") or {}
    bb_enabled = bool(settings.get("IsBetBuilderEnabled", False)) if isinstance(settings, dict) else False

    # ── Fixture importance signals (Phase 1) ──
    # All Rogue-native fields. Stamped here so downstream Phase 2 scorer
    # can read them off Game without re-fetching the raw event.
    # `is_operator_featured` is set later in `fetch_soccer_snapshot` after
    # we cross-reference the getFeaturedEvents response.
    league_order_raw = event.get("LeagueOrder")
    try:
        league_order = int(league_order_raw) if league_order_raw is not None else None
    except (TypeError, ValueError):
        league_order = None

    early_payout_value_raw = event.get("EarlyPayoutValue")
    try:
        early_payout_value = float(early_payout_value_raw) if early_payout_value_raw is not None else None
    except (TypeError, ValueError):
        early_payout_value = None

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
        is_bet_builder_enabled=bb_enabled,
        league_order=league_order,
        is_early_payout=bool(event.get("IsEarlyPayout", False)),
        early_payout_value=early_payout_value,
        is_top_league=bool(event.get("IsTopLeague", False)),
        region_code=(str(event["RegionCode"]) if event.get("RegionCode") else None),
        league_group_id=(str(event["LeagueGroupId"]) if event.get("LeagueGroupId") else None),
        is_operator_featured=False,  # set downstream in fetch_soccer_snapshot
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
            full = await client.get_event(str(eid), include_markets="all", locale="en")
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
    # Funnel composition: spot if Rogue suddenly stops sending the BB flag.
    bb_on = sum(1 for g in games if g.is_bet_builder_enabled)
    pct = int(round(100.0 * bb_on / len(games))) if games else 0
    logger.info(
        "[catalogue] loaded %d fixtures, %d with BB-enabled (%d%% of total)",
        len(games), bb_on, pct,
    )

    # ── Tag operator-featured fixtures (Phase 1, fixture-importance.md) ──
    # Cross-reference the operator's curated featured-events list against
    # our loaded catalogue. A match is the strongest single importance
    # signal we have — no Pulse-side admin needed; the operator's normal
    # merchandising flow IS the configuration.
    await _tag_operator_featured(client, games)

    # Boot-time visibility: prove Phase 1 captured the importance signals.
    _log_importance_signal_summary(games)

    # ── Phase 2a: stamp importance_score + log distribution ──
    # Pure compute over already-captured Game fields. No engine routing
    # consumes this yet — observability + future-proof for Phase 2b.
    _stamp_importance_scores(games)
    _log_importance_score_distribution(games)

    return games, markets, raw_detailed


async def _tag_operator_featured(client: RogueClient, games: list[Game]) -> None:
    """Set `Game.is_operator_featured=True` for fixtures in the operator's
    featured-events list. Failure-tolerant — on any error, every Game
    keeps `is_operator_featured=False` and engine continues.
    """
    try:
        featured = await client.get_featured_events(locale="en")
    except Exception as exc:
        logger.warning("[catalogue] featured-events tagging skipped (fetch failed): %s", exc)
        return

    featured_ids = {str(e.get("_id")) for e in featured if e.get("_id")}
    if not featured_ids:
        logger.info("[catalogue] tagged 0 fixtures as operator-featured (out of %d total catalogued)", len(games))
        return

    tagged = 0
    for g in games:
        if g.id in featured_ids:
            g.is_operator_featured = True
            tagged += 1

    logger.info(
        "[catalogue] tagged %d fixtures as operator-featured (out of %d total catalogued)",
        tagged, len(games),
    )


def _log_importance_signal_summary(games: list[Game]) -> None:
    """One-line boot-log summary so anyone reading runtime logs can verify
    Phase 1 plumbing actually captured the signals from Rogue.
    """
    if not games:
        return
    featured_n = sum(1 for g in games if g.is_operator_featured)
    top_league_n = sum(1 for g in games if g.is_top_league)
    early_payout_n = sum(1 for g in games if g.is_early_payout)
    league_orders = [g.league_order for g in games if g.league_order is not None]
    if league_orders:
        lo_min, lo_max = min(league_orders), max(league_orders)
        lo_range = f"[{lo_min}, {lo_max}]"
    else:
        lo_range = "[none]"
    logger.info(
        "[catalogue] importance signals: featured=%d, top_league=%d, early_payout=%d "
        "(league_order range: %s, n=%d)",
        featured_n, top_league_n, early_payout_n, lo_range, len(league_orders),
    )


def _stamp_importance_scores(games: list[Game]) -> None:
    """Stamp `importance_score` on every Game using rank-based normalization.

    Recomputed on every catalogue load. The score is rank-percentile of
    `league_order` within the loaded catalogue (featured fixtures float
    to 1.0). Phase 2b's gradient router in CandidateEngine reads
    `game.importance_score` directly — no recomputation in the hot path.
    """
    from app.engine.importance_scorer import assign_rank_importance_scores

    assign_rank_importance_scores(games)


def _log_importance_score_distribution(games: list[Game]) -> None:
    """Phase 2a: log score distribution + top-5 fixtures by score.

    Calibration window — one production cycle later we read the actual
    distribution before flipping on Phase 2b's tier router. Per
    `feedback_calibrate_before_scoring.md`: capture-and-observe before
    behaviour-routing.
    """
    from app.engine.importance_scorer import (
        DEFAULT_DEEP_THRESHOLD,
        DEFAULT_MINIMAL_THRESHOLD,
        classify_score,
    )

    if not games:
        return

    scored = [(g, g.importance_score if g.importance_score is not None else 0.0)
              for g in games]

    deep_n = standard_n = minimal_n = 0
    for _, s in scored:
        bucket = classify_score(s)
        if bucket == "deep":
            deep_n += 1
        elif bucket == "standard":
            standard_n += 1
        else:
            minimal_n += 1

    logger.info(
        "[importance] score distribution: deep=%d (>=%.1f), standard=%d (%.1f-%.1f), minimal=%d (<%.1f)",
        deep_n, DEFAULT_DEEP_THRESHOLD,
        standard_n, DEFAULT_MINIMAL_THRESHOLD, DEFAULT_DEEP_THRESHOLD,
        minimal_n, DEFAULT_MINIMAL_THRESHOLD,
    )

    # Top-5 by descending score; tiebreak by ascending league_order
    # (lower = higher operator priority, design-doc convention).
    def _sort_key(item: tuple[Game, float]) -> tuple[float, int]:
        g, s = item
        # Negate score so descending; positive league_order so ascending.
        # `league_order is None` sorts last on the tiebreak.
        lo = g.league_order if g.league_order is not None else 10**12
        return (-s, lo)

    top5 = sorted(scored, key=_sort_key)[:5]
    parts = []
    for g, s in top5:
        league = g.broadcast or "?"
        home = g.home_team.name if g.home_team else "?"
        away = g.away_team.name if g.away_team else "?"
        parts.append(f"{s:.2f}={league}: {home} vs {away}")
    logger.info(
        "[importance] top 5 fixtures by score: %s",
        "; ".join(parts) if parts else "(none)",
    )
