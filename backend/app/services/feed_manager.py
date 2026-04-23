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
        # Optional render-time card decorator — used by main.py to stamp
        # card.deep_link from the Stage 5 env templates without having to
        # patch every add_prematch_card caller (there are 7, spread across
        # the baseline/engine/featured/mock paths). Callers set this once
        # after instantiation; None means pass-through.
        self._decorator: "callable | None" = None

    def set_decorator(self, decorator) -> None:
        """Install a per-card decorator called on every insert. The callable
        receives a Card and returns it (mutated in place is fine)."""
        self._decorator = decorator

    def _decorate(self, card: Card) -> Card:
        if self._decorator is not None:
            try:
                return self._decorator(card)
            except Exception:
                # Never block a card from being added over a decorator bug.
                return card
        return card

    def add_prematch_card(self, card: Card):
        self.prematch_cards.append(self._decorate(card))
        self._sort_prematch()

    def replace_prematch_cards(self, cards: list[Card]):
        """Atomic swap of the entire pre-match card list. Used by the
        scheduled rerun loop so re-generation doesn't double-render cards
        from the previous cycle. Sort happens once at the end."""
        self.prematch_cards = [self._decorate(c) for c in cards]
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

    def get_card(self, card_id: str) -> Card | None:
        for c in self.prematch_cards:
            if c.id == card_id:
                return c
        for c in self.live_cards:
            if c.id == card_id:
                return c
        return None

    def update_card_total(
        self,
        card_id: str,
        *,
        total_odds: float | None = None,
        leg_odds: dict[str, float] | None = None,
        suspended: bool | None = None,
    ) -> Card | None:
        """Mutate a card's price/leg odds/suspension state in place. Returns
        the updated card (or None if not found). Caller is responsible for
        broadcasting the change via `broadcast_card_update`."""
        card = self.get_card(card_id)
        if card is None:
            return None
        if total_odds is not None:
            card.total_odds = round(float(total_odds), 2)
        if leg_odds:
            for leg in card.legs:
                if leg.selection_id and leg.selection_id in leg_odds:
                    try:
                        leg.odds = round(float(leg_odds[leg.selection_id]), 2)
                    except (TypeError, ValueError):
                        pass
        if suspended is not None:
            card.suspended = bool(suspended)
        return card

    async def broadcast_card_update(self, card: Card) -> None:
        """Push a price/state delta for one card to all WebSocket clients.
        Frontend handler updates the DOM in place + animates the change."""
        data = json.dumps({
            "type": "card_update",
            "card_id": card.id,
            "total_odds": card.total_odds,
            "suspended": card.suspended,
            "leg_odds": {
                leg.selection_id: leg.odds
                for leg in card.legs
                if leg.selection_id
            },
            "ts": time.time(),
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
