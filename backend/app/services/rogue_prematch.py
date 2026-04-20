"""Programmatic pre-match card generator for the Rogue data path.

Stage 1 fills a single card per fixture — league + kickoff headline and the
Match Result market. Heuristics (value, form, themes) land in Stage 3–4.
"""
from __future__ import annotations

import logging

from app.models.schemas import (
    BadgeType,
    Game,
    Market,
    StatDisplay,
)
from app.services.market_catalog import MarketCatalog
from app.engine.card_assembler import CardAssembler

logger = logging.getLogger(__name__)


# Priority order for which market becomes the card's primary market when more
# than one is available for a fixture.
_MARKET_PRIORITY = [
    "match_result",
    "over_under",
    "btts",
    "double_chance",
    "spread",
]


def _pick_primary_market(markets: list[Market]) -> Market | None:
    by_type: dict[str, Market] = {}
    for m in markets:
        by_type.setdefault(m.market_type, m)
    for mt in _MARKET_PRIORITY:
        if mt in by_type:
            return by_type[mt]
    return markets[0] if markets else None


def _narrative(game: Game, market: Market) -> str:
    league = game.broadcast or "Top-league fixture"
    home = game.home_team.name
    away = game.away_team.name
    when = game.start_time or "Kickoff TBC"
    return f"{league} — {home} host {away} ({when})"


def _stats_from_match_result(market: Market) -> list[StatDisplay]:
    stats: list[StatDisplay] = []
    for sel in market.selections[:3]:
        stats.append(StatDisplay(label=sel.label, value=sel.odds or "—"))
    return stats


def build_prematch_cards(
    games: list[Game],
    catalog: MarketCatalog,
    assembler: CardAssembler,
    limit: int = 20,
) -> list:
    """Generate one pre-match card per game with a headline market.

    Deliberately simple — Stage 3 replaces this with the singles recommender
    and Stage 4 adds combo/BB cards on top.
    """
    cards = []
    for game in games[:limit]:
        markets = catalog.get_by_game(game.id)
        if not markets:
            logger.debug("Rogue: no markets for %s, skipping card", game.id)
            continue
        primary = _pick_primary_market(markets)
        if primary is None:
            continue
        stats = _stats_from_match_result(primary) if primary.market_type == "match_result" else []
        card = assembler.assemble_prematch(
            game=game,
            market=primary,
            narrative=_narrative(game, primary),
            badge=BadgeType.TRENDING,
            relevance=0.5,
            stats=stats,
            tweets=[],
        )
        cards.append(card)
    return cards
