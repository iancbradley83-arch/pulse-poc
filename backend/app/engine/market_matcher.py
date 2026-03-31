"""Market Matcher — given an entity-tagged event, find relevant markets."""
from __future__ import annotations

from app.models.schemas import GameEvent, Market, EventType
from app.services.market_catalog import MarketCatalog


class MarketMatcher:
    def __init__(self, catalog: MarketCatalog):
        self.catalog = catalog

    def match(self, event: GameEvent) -> list[Market]:
        """Find markets relevant to this event."""
        markets = []

        if event.event_type == EventType.THRESHOLD_APPROACH:
            # Match player prop markets for the specific stat
            stat_type = event.data.get("stat_type")
            if event.player_id and stat_type:
                markets = self.catalog.get_by_player(event.player_id, stat_type)
            # Fallback: any market for this player
            if not markets and event.player_id:
                markets = self.catalog.get_by_player(event.player_id)

        elif event.event_type == EventType.SCORE_CHANGE:
            # Match game-level markets (spread, total, match result)
            game_markets = self.catalog.get_by_game(event.game_id)
            markets = [m for m in game_markets if m.market_type in ("spread", "over_under", "match_result")]

        elif event.event_type == EventType.MOMENTUM_SHIFT:
            # Match spread / moneyline for the game
            game_markets = self.catalog.get_by_game(event.game_id)
            markets = [m for m in game_markets if m.market_type in ("spread", "moneyline", "match_result")]

        elif event.event_type == EventType.MILESTONE:
            # Match player-specific markets
            if event.player_id:
                markets = self.catalog.get_by_player(event.player_id)

        elif event.event_type == EventType.STAT_UPDATE:
            stat_type = event.data.get("stat_type")
            if event.player_id and stat_type:
                markets = self.catalog.get_by_player(event.player_id, stat_type)

        return markets
