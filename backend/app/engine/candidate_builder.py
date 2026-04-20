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
HOOK_MARKET_PRIORITY: dict[HookType, list[str]] = {
    HookType.INJURY: ["match_result", "over_under", "btts"],
    HookType.TEAM_NEWS: ["match_result", "over_under"],
    HookType.TACTICAL: ["over_under", "btts", "match_result"],
    HookType.TRANSFER: ["match_result"],
    HookType.MANAGER_QUOTE: ["match_result"],
    HookType.PREVIEW: ["match_result"],
    HookType.ARTICLE: ["match_result"],
}
HOOK_DEFAULT_MARKETS = ["match_result"]


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
