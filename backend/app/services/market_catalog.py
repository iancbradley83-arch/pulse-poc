"""Market Catalog Service — stores and indexes markets by entity for fast lookup."""

from __future__ import annotations
import json
from pathlib import Path
from collections import defaultdict
from typing import Optional
from app.models.schemas import Market


DATA_DIR = Path(__file__).parent.parent / "data"


class MarketCatalog:
    def __init__(self):
        self.markets: dict[str, Market] = {}
        self._by_player: dict[str, list[str]] = defaultdict(list)
        self._by_team: dict[str, list[str]] = defaultdict(list)
        self._by_game: dict[str, list[str]] = defaultdict(list)
        self._by_player_stat: dict[str, list[str]] = defaultdict(list)
        self._load()

    def _load(self):
        raw = json.loads((DATA_DIR / "mock_markets.json").read_text())
        for m in raw:
            market = Market(**m)
            self.markets[market.id] = market
            self._by_game[market.game_id].append(market.id)
            if market.player_id:
                self._by_player[market.player_id].append(market.id)
                if market.stat_type:
                    key = f"{market.player_id}:{market.stat_type}"
                    self._by_player_stat[key].append(market.id)
            if market.team_id:
                self._by_team[market.team_id].append(market.id)

    def get(self, market_id: str) -> Market | None:
        return self.markets.get(market_id)

    def get_by_game(self, game_id: str) -> list[Market]:
        return [self.markets[mid] for mid in self._by_game.get(game_id, [])]

    def get_by_player(self, player_id: str, stat_type: str | None = None) -> list[Market]:
        if stat_type:
            key = f"{player_id}:{stat_type}"
            ids = self._by_player_stat.get(key, [])
        else:
            ids = self._by_player.get(player_id, [])
        return [self.markets[mid] for mid in ids]

    def get_by_team(self, team_id: str) -> list[Market]:
        return [self.markets[mid] for mid in self._by_team.get(team_id, [])]

    def update_odds(self, market_id: str, selection_index: int, new_odds: str):
        """Update odds for a market selection, storing previous odds."""
        market = self.markets.get(market_id)
        if market and selection_index < len(market.selections):
            sel = market.selections[selection_index]
            sel.previous_odds = sel.odds
            sel.odds = new_odds

    def suspend(self, market_id: str):
        market = self.markets.get(market_id)
        if market:
            market.status = "suspended"

    def reopen(self, market_id: str):
        market = self.markets.get(market_id)
        if market:
            market.status = "open"
