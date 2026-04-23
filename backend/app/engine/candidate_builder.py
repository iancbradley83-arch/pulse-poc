"""Candidate builder — news item -> CandidateCard list.

For each news item that resolved to one or more fixtures, pick the market(s)
the news touches and emit a draft CandidateCard.

Market selection by hook_type — see HOOK_MARKET_PRIORITY below for the
authoritative mapping.

Player-aware goalscorer routing (added 2026-04-22): when a TEAM_NEWS /
TRANSFER / MANAGER_QUOTE story names a specific player who appears in the
fixture's Goalscorer market, we route to that player's anytime-scorer
selection ahead of the generic priority list. This turns "Saka returns
from injury" into a single tied to Saka's price, not the overall favourite.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    HookType,
    NewsItem,
)
from app.models.schemas import Game, Market, MarketSelection, MarketStatus
from app.services.catalogue_loader import GOALSCORER_DEFAULT_TOP_N
from app.services.market_catalog import MarketCatalog

logger = logging.getLogger(__name__)


# Which internal market_type values each hook prefers, in priority order.
# First match wins. Unlisted hooks fall back to HOOK_DEFAULT_MARKETS.
#
# After the market-coverage expansion, hooks can now route to richer markets:
#   - INJURY            → goalscorer (striker out → who scores instead?),
#                         over/under (goal-impact), then DNB / FT 1X2.
#   - TEAM_NEWS         → goalscorer (key player back) → over/under → 1X2.
#   - TACTICAL          → corners O/U + cards O/U (chaos signals), totals,
#                         then 1X2.
#   - MANAGER_QUOTE     → 1st-half 1X2 (intent for early pressure) + 1X2.
#   - PREVIEW / ARTICLE → totals + 1X2 (safest defaults).
#   - TRANSFER          → 1X2 + goalscorer (new signing).
HOOK_MARKET_PRIORITY: dict[HookType, list[str]] = {
    # Injury: a named player is out → their team is weaker → opponent without
    # draw risk (DNB-opp) is the natural read. "Less goals" (over_under-under)
    # is generic and only true when the injured player is an attacker — we
    # don't differentiate position yet, so DNB-opp is the safer first hop.
    HookType.INJURY: ["draw_no_bet", "over_under", "match_result", "btts", "goalscorer"],
    HookType.TEAM_NEWS: ["over_under", "match_result", "goalscorer", "btts"],
    # Tactical = chaos signal: corners + cards first, then totals.
    HookType.TACTICAL: ["corners_ou", "cards_ou", "over_under", "btts", "match_result"],
    # Transfer = usually a new attacker, goalscorer angle is natural.
    HookType.TRANSFER: ["goalscorer", "match_result", "over_under"],
    # Manager quote = intent + early pressure. 1st half result fits.
    HookType.MANAGER_QUOTE: ["first_half_result", "match_result", "over_under"],
    HookType.PREVIEW: ["over_under", "match_result", "double_chance"],
    HookType.ARTICLE: ["match_result", "over_under"],
}
HOOK_DEFAULT_MARKETS = ["match_result", "over_under"]


# Hooks that, even without a player-aware match, pass the "specific actor"
# test by virtue of the hook type itself (an INJURY card is about a named
# actor; a TRANSFER is about a named signing; a MANAGER_QUOTE is about a
# named manager — even if we can't player-match the goalscorer market).
# PREVIEW and ARTICLE are NOT in this set: they're generic "look at this
# fixture" hooks with no specific actor, so their singles get dropped.
_HOOKS_WITH_INTRINSIC_ACTOR = {
    HookType.INJURY,
    HookType.TRANSFER,
    HookType.MANAGER_QUOTE,
    HookType.TEAM_NEWS,
    HookType.TACTICAL,
}


def _has_specific_actor(item: NewsItem) -> bool:
    """A news item has a 'specific actor' when its mentions list includes
    at least one name that isn't just a team. Conservative heuristic: if
    there are mentions beyond the resolved team_ids count, at least one is
    likely a player or coach. Falls back to True for hook types whose news
    is intrinsically actor-led (INJURY, TRANSFER, MANAGER_QUOTE)."""
    if item.hook_type in _HOOKS_WITH_INTRINSIC_ACTOR:
        # Even without parseable player names, these hook types are
        # about a specific real-world actor by definition.
        return True
    # Generic hooks (PREVIEW, ARTICLE, OTHER): require at least 2 mentions
    # so we can be confident there's a player/coach beyond the team itself.
    return len(item.mentions or []) >= 2


# Hooks where naming a specific player shifts the card angle positively
# (they're MORE likely to score). Injury stories are excluded because an
# injured player scoring is anti-correlated, and we don't have position
# data to pick the opposing striker instead.
_PLAYER_AWARE_HOOKS = {HookType.TEAM_NEWS, HookType.TRANSFER, HookType.MANAGER_QUOTE}

# Words in `news.mentions` that clearly aren't player names. Used to
# avoid false matches like "Arsenal" matching selection "Arsenal Bukayo"
# (if Rogue ever returns that shape) or 2-letter noise.
_MENTION_STOPWORDS = {
    "the", "and", "for", "from", "with", "into", "over", "under",
    "fc", "cf", "ac", "city", "united", "rovers", "athletic",
}


def _normalize_name(s: str) -> str:
    """Lowercase + strip diacritics. `Mbappé` -> `mbappe`, `Jesús` -> `jesus`."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.lower()


def _name_tokens(s: str) -> list[str]:
    """Split a name into lowercase/de-accented word tokens of length >= 3."""
    norm = _normalize_name(s)
    return [t for t in re.split(r"\W+", norm) if len(t) >= 3 and t not in _MENTION_STOPWORDS]


def _match_player_selection(
    market: Market, mentions: list[str]
) -> Optional[MarketSelection]:
    """Find a goalscorer selection whose label matches a player named in
    `mentions`. Prefer last-name matches (more specific); fall back to any
    token match. Returns the shortest-odds match when several players in
    the same mention tie, so "Arsenal (Saka, Saliba)" prefers Saka."""
    if not market.selections or not mentions:
        return None

    mention_tokens: list[tuple[str, list[str]]] = []
    for m in mentions:
        toks = _name_tokens(m)
        if toks:
            mention_tokens.append((m, toks))

    if not mention_tokens:
        return None

    def _odds(sel: MarketSelection) -> float:
        try:
            return float(sel.odds) if sel.odds else 9999.0
        except ValueError:
            return 9999.0

    best: Optional[tuple[int, float, MarketSelection]] = None  # (priority, odds, sel)
    for sel in market.selections:
        sel_tokens = _name_tokens(sel.label)
        if not sel_tokens:
            continue
        sel_set = set(sel_tokens)
        sel_last = sel_tokens[-1]
        for raw_mention, m_toks in mention_tokens:
            # Priority 1: the surname (last token) of the selection appears
            # as a whole word in the mention. "Saka" mention vs "Bukayo
            # Saka" selection -> match on last token.
            if sel_last in set(m_toks):
                priority = 1
            elif sel_set & set(m_toks):
                priority = 2
            else:
                continue
            key = (priority, _odds(sel), sel)
            if best is None or key < best:
                best = key
                break
    return best[2] if best else None


# ── Position-aware INJURY routing ───────────────────────────────────────────
#
# Problem we're solving (2026-04-23 live review, 1-2/5 injury cards):
#   INJURY hook used a hard "DNB opponent" rule regardless of:
#     - whether the injured player actually moves the market we're routing to
#       (striker out -> Under, not opponent-to-win);
#     - whether the opponent is already a heavy favourite (DNB @ 1.07 = no edge);
#     - whether the narrative contradicts the pick (collapsing defence + DNB
#       on that same team).
#
# New behaviour: inspect news.injury_details, pick the dominant out-position
# on the affected side, and route to a market+selection whose story matches.
#
#   Striker / winger / attacking_mid OUT on team A
#     -> team_A's team-total under (if we had it) or Under 2.5 or BTTS NO.
#        Do NOT route DNB-opponent.
#   Centre-back / fullback OUT on team A
#     -> Over 2.5 or BTTS YES (team A concedes more).
#   Goalkeeper OUT on team A
#     -> Over 2.5 or BTTS YES.
#   Defensive mid OUT on team A
#     -> lean corners/cards O/U over + Over 2.5 (game opens up).
#   Multiple at the same position -> confidence boost (future: score bump).
#   Unknown position -> fall through to the old priority list.
#
# We don't yet have team_total_under in the catalogue (TODO: add when
# includeMarkets coverage grows). Until then we pick the closest substitute
# that doesn't back the injured team.
#
# (market_type, outcome_key) — outcome_key values map to _find_injury_selection.
InjuryLeg = tuple[str, str]

_INJURY_ROUTES: dict[str, list[InjuryLeg]] = {
    # Attackers out => fewer goals.
    "striker":        [("over_under", "under"), ("btts", "btts_no")],
    "winger":         [("over_under", "under"), ("btts", "btts_no")],
    "attacking_mid":  [("over_under", "under"), ("btts", "btts_no")],
    # Defenders out => more goals / both to score.
    "centre_back":    [("over_under", "over"), ("btts", "btts_yes")],
    "fullback":       [("over_under", "over"), ("btts", "btts_yes")],
    "goalkeeper":     [("over_under", "over"), ("btts", "btts_yes")],
    # Defensive mid => game opens up, chaos signals.
    "defensive_mid":  [("corners_ou", "over"), ("cards_ou", "over"),
                       ("over_under", "over")],
    # No generic "midfielder" bucket — the scout is forced to pick
    # attacking_mid or defensive_mid (or fall through to "unknown"). Old
    # cached rows with "midfielder" are folded to "unknown" at the
    # news_ingester parse layer and fall through to the legacy priority
    # list here (routes.get(pos, []) returns empty → no forced routing).
}


def _affected_side_for_news(news: NewsItem, game: Game) -> Optional[str]:
    """Return 'home' / 'away' / None — which side the news touches."""
    home = game.home_team.id in news.team_ids
    away = game.away_team.id in news.team_ids
    if home and not away:
        return "home"
    if away and not home:
        return "away"
    if home and away:
        return "home"
    return None


def _dominant_out_position(
    injury_details: list[dict], affected_team_name: str
) -> Optional[str]:
    """Pick the position with the most CONFIRMED-out players on the affected
    side. Ties break by hierarchy (striker > centre_back > goalkeeper > ...)
    — the most market-moving role wins. Returns None when nothing actionable."""
    affected_low = (affected_team_name or "").lower()
    counts: dict[str, int] = {}
    for d in injury_details or []:
        if not isinstance(d, dict):
            continue
        if not d.get("is_out_confirmed"):
            continue
        pos = (d.get("position_guess") or "unknown").lower()
        if pos == "unknown":
            continue
        team = (d.get("team") or "").lower()
        # Loose team match: scout sometimes emits short forms ("Napoli") where
        # Rogue uses the full name. Accept if either contains the other.
        if affected_low and team and (
            team in affected_low or affected_low in team
        ):
            counts[pos] = counts.get(pos, 0) + 1
        elif not affected_low or not team:
            # Fall through — if we can't tell, still count it.
            counts[pos] = counts.get(pos, 0) + 1
    if not counts:
        return None
    # Tie-break priority: market-moving roles first.
    tie_break = {
        "striker": 0, "winger": 1, "attacking_mid": 2,
        "goalkeeper": 3, "centre_back": 4, "fullback": 5,
        "defensive_mid": 6,
    }
    return sorted(
        counts.items(), key=lambda kv: (-kv[1], tie_break.get(kv[0], 99))
    )[0][0]


def _find_injury_selection(
    market: Market, outcome_key: str, affected: str
) -> Optional[MarketSelection]:
    """Return the MarketSelection matching outcome_key within `market`.
    Mirrors ComboBuilder's _find_selection but scoped to the injury routes."""
    opponent = "away" if affected == "home" else "home"

    def by_outcome(o: str) -> Optional[MarketSelection]:
        wanted = o.lower()
        for sel in market.selections:
            if (sel.outcome_type or "").lower() == wanted:
                return sel
        return None

    def by_label_substr(*needles: str) -> Optional[MarketSelection]:
        for sel in market.selections:
            label = (sel.label or "").lower()
            if all(n in label for n in needles):
                return sel
        return None

    if outcome_key == "affected":
        return by_outcome(affected)
    if outcome_key == "opponent":
        return by_outcome(opponent)
    if outcome_key == "over":
        return by_outcome("over") or by_label_substr("over")
    if outcome_key == "under":
        return by_outcome("under") or by_label_substr("under")
    if outcome_key == "btts_yes":
        return by_label_substr("yes") or (
            market.selections[0] if market.selections else None
        )
    if outcome_key == "btts_no":
        return by_label_substr("no") or (
            market.selections[1] if len(market.selections) > 1 else None
        )
    return None


def _build_injury_single(
    item: NewsItem,
    game: Game,
    by_type: dict[str, Market],
) -> Optional[tuple[Market, Optional[MarketSelection]]]:
    """Position-aware INJURY routing — return (market, selection) or None
    if the news lacks position data or no mapped market exists."""
    side = _affected_side_for_news(item, game)
    if side is None:
        return None
    affected_team_name = (
        game.home_team.name if side == "home" else game.away_team.name
    )
    pos = _dominant_out_position(item.injury_details, affected_team_name)
    if pos is None:
        return None
    routes = _INJURY_ROUTES.get(pos, [])
    for market_type, outcome_key in routes:
        market = by_type.get(market_type)
        if market is None:
            continue
        sel = _find_injury_selection(market, outcome_key, side)
        if sel is None or not sel.selection_id:
            continue
        logger.info(
            "CandidateBuilder: INJURY position=%s -> %s/%s on side=%s (game=%s)",
            pos, market_type, outcome_key, side, game.id,
        )
        return market, sel
    return None


class CandidateBuilder:
    def __init__(self, catalog: MarketCatalog, games: Optional[dict[str, Game]] = None):
        self._catalog = catalog
        # Games keyed by id — required for position-aware INJURY routing so
        # we can resolve affected side. Caller (candidate_engine) passes the
        # same dict it iterates over. Optional for back-compat with any
        # lingering test callers; INJURY routing falls back to the old
        # priority list when games is None.
        self._games = games or {}

    def build(self, item: NewsItem) -> list[CandidateCard]:
        if not item.fixture_ids:
            return []
        # Principle 2: singles fire only when the news has a specific actor.
        # Generic "back the favourite on a fixture preview" cards get dropped;
        # the BB version of the news item (which can spread the angle across
        # legs) carries the story instead. PREVIEW + ARTICLE hooks with no
        # named entity beyond the team name fail this gate.
        if not _has_specific_actor(item):
            logger.info(
                "CandidateBuilder: dropping single — no specific actor "
                "(hook=%s mentions=%s)", item.hook_type.value, item.mentions,
            )
            return []
        priority = HOOK_MARKET_PRIORITY.get(item.hook_type, HOOK_DEFAULT_MARKETS)
        out: list[CandidateCard] = []

        for game_id in item.fixture_ids:
            picked = self._pick_market_for_item(item, game_id, priority)
            if picked is None:
                logger.debug(
                    "CandidateBuilder: no suitable market for game=%s hook=%s",
                    game_id, item.hook_type.value,
                )
                continue
            market, matched_selection = picked
            # For player-matched goalscorer singles we stamp the matched
            # selection_id on the candidate; the publisher trims the market
            # to just that selection at render time. We keep the original
            # market.id so `catalog.get(market_id)` still resolves.
            selection_ids: list[str] = []
            if matched_selection is not None and matched_selection.selection_id:
                selection_ids = [matched_selection.selection_id]
            out.append(CandidateCard(
                news_item_id=item.id,
                hook_type=item.hook_type,
                bet_type=BetType.SINGLE,
                game_id=game_id,
                market_ids=[market.id],
                selection_ids=selection_ids,
                narrative=item.headline,
                status=CandidateStatus.DRAFT,
            ))
        return out

    def _pick_market_for_item(
        self, item: NewsItem, game_id: str, priority: list[str]
    ) -> Optional[tuple[Market, Optional[MarketSelection]]]:
        """Pick (market, optional matched-selection) for a candidate single.

        Returns the matched goalscorer selection when a player named in
        news.mentions resolves to a real selection in the Goalscorer
        market. Returns selection=None for all other paths (publisher
        renders the whole market as before).
        """
        markets = self._catalog.get_by_game(game_id)
        if not markets:
            return None

        by_type: dict[str, Market] = {}
        for m in markets:
            by_type.setdefault(m.market_type, m)

        # Player-aware goalscorer hop: for TEAM_NEWS / TRANSFER /
        # MANAGER_QUOTE, if the fixture has a Goalscorer market AND the
        # mentioned player matches a selection, surface that player directly.
        if item.hook_type in _PLAYER_AWARE_HOOKS and "goalscorer" in by_type:
            gs_market = by_type["goalscorer"]
            match = _match_player_selection(gs_market, item.mentions)
            if match is not None:
                logger.info(
                    "CandidateBuilder: matched goalscorer player %r for hook=%s game=%s",
                    match.label, item.hook_type.value, game_id,
                )
                return gs_market, match

        # Position-aware INJURY routing (2026-04-23). Supersedes the hard
        # HOOK_MARKET_PRIORITY[INJURY] rule when injury_details carries
        # confirmed-out players with a known position. Falls through to the
        # old priority list when the story doesn't have enough structure
        # (no injury_details, or only 'unknown' positions).
        if item.hook_type == HookType.INJURY:
            game = self._games.get(game_id)
            if game is not None:
                picked = _build_injury_single(item, game, by_type)
                if picked is not None:
                    return picked

        for mt in priority:
            if mt in by_type:
                return by_type[mt], None
        return markets[0], None
