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
from app.models.schemas import Market, MarketSelection, MarketStatus
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
    # Injury → totals lean (less goals), DNB safer than 1X2. Goalscorer is
    # last because we don't yet match the leg's player to news.mentions —
    # surfacing "Anytime Scorer · Haaland" on a Burnley-keeper-out story
    # would be a non-sequitur. Player-aware routing is a follow-up PR.
    HookType.INJURY: ["over_under", "draw_no_bet", "match_result", "btts", "goalscorer"],
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


class CandidateBuilder:
    def __init__(self, catalog: MarketCatalog):
        self._catalog = catalog

    def build(self, item: NewsItem) -> list[CandidateCard]:
        if not item.fixture_ids:
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

        for mt in priority:
            if mt in by_type:
                return by_type[mt], None
        return markets[0], None
