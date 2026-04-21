"""Feed Manager — manages feed state, ranking, and card lifecycle."""
from __future__ import annotations

import json
import time
from app.models.schemas import Card, CardType


class FeedManager:
    def __init__(self):
        self.prematch_cards: list[Card] = []
        self.live_cards: list[Card] = []
        self._websocket_clients: list = []

    def add_prematch_card(self, card: Card):
        self.prematch_cards.append(card)
        self._sort_prematch()

    def replace_prematch_cards(self, cards: list[Card]):
        """Atomic swap of the entire pre-match card list. Used by the
        scheduled rerun loop so re-generation doesn't double-render cards
        from the previous cycle. Sort happens once at the end."""
        self.prematch_cards = list(cards)
        self._sort_prematch()

    def add_live_card(self, card: Card):
        self.live_cards.insert(0, card)  # newest first
        self._prune_stale()

    def get_prematch_feed(self, sport: str | None = None, limit: int = 50) -> list[dict]:
        cards = self.prematch_cards
        if sport:
            cards = [c for c in cards if c.game.sport.value == sport]
        return [c.model_dump() for c in cards[:limit]]

    def get_live_feed(self, game_id: str | None = None, limit: int = 50) -> list[dict]:
        self._prune_stale()
        cards = self.live_cards
        if game_id:
            cards = [c for c in cards if c.game.id == game_id]
        return [c.model_dump() for c in cards[:limit]]

    def register_ws(self, ws):
        self._websocket_clients.append(ws)

    def unregister_ws(self, ws):
        if ws in self._websocket_clients:
            self._websocket_clients.remove(ws)

    async def broadcast_card(self, card: Card):
        """Push a new card to all WebSocket clients."""
        data = json.dumps({
            "type": "new_card",
            "card": card.model_dump(),
        }, default=str)
        dead = []
        for ws in self._websocket_clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister_ws(ws)

    async def broadcast_feed_refresh(self):
        """Tell connected clients to re-pull /api/feed (used after a
        scheduled candidate-engine rerun replaces the card list)."""
        data = json.dumps({"type": "feed_refresh", "ts": time.time()}, default=str)
        dead = []
        for ws in self._websocket_clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister_ws(ws)

    async def broadcast_game_update(self, game_data: dict):
        """Push a game state update to all WebSocket clients."""
        data = json.dumps({
            "type": "game_update",
            "game": game_data,
        }, default=str)
        dead = []
        for ws in self._websocket_clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister_ws(ws)

    def _sort_prematch(self):
        self.prematch_cards.sort(key=lambda c: c.relevance_score, reverse=True)

    def _prune_stale(self):
        now = time.time()
        self.live_cards = [
            c for c in self.live_cards
            if now - c.created_at < c.ttl_seconds
        ]
