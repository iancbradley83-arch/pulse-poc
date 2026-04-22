"""Candidate builder — news item -> CandidateCard list.

For each news item that resolved to one or more fixtures, pick the market(s)
the news touches and emit a draft CandidateCard. Stage 2 only emits singles;
Stage 3 extends this to combos + Bet Builder.

Market selection by hook_type (rough mapping — refined via admin review):

  injury / team_news / tactical -> match_result (primary angle)
                                    + over_under if the news implies goal impact
  manager_quote                 -> match_result
  transfer                      -> match_result
  preview / article             -> match_result (safest default)

No player-prop routing yet — we don't have player-prop markets in the Stage 1
snapshot. When we add them, injury/team_news about a specific player will
route here.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    HookType,
    NewsItem,
)
from app.models.schemas import Market
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


class CandidateBuilder:
    def __init__(self, catalog: MarketCatalog):
        self._catalog = catalog

    def build(self, item: NewsItem) -> list[CandidateCard]:
        if not item.fixture_ids:
            return []
        priority = HOOK_MARKET_PRIORITY.get(item.hook_type, HOOK_DEFAULT_MARKETS)
        out: list[CandidateCard] = []

        for game_id in item.fixture_ids:
            market = self._pick_market(game_id, priority)
            if market is None:
                logger.debug(
                    "CandidateBuilder: no suitable market for game=%s hook=%s",
                    game_id, item.hook_type.value,
                )
                continue
            out.append(CandidateCard(
                news_item_id=item.id,
                hook_type=item.hook_type,
                bet_type=BetType.SINGLE,
                game_id=game_id,
                market_ids=[market.id],
                narrative=item.headline,   # Stage 4 rewrites via LLM
                status=CandidateStatus.DRAFT,
            ))
        return out

    def _pick_market(self, game_id: str, priority: list[str]) -> Optional[Market]:
        markets = self._catalog.get_by_game(game_id)
        if not markets:
            return None
        by_type: dict[str, Market] = {}
        for m in markets:
            by_type.setdefault(m.market_type, m)
        for mt in priority:
            if mt in by_type:
                return by_type[mt]
        return markets[0]
