"""Relevance Scorer — composite scoring for context + market combinations."""

import time
from app.models.schemas import GameEvent, Market, EventType


class RelevanceScorer:
    """Computes a 0-1 relevance score from weighted signals."""

    # Tunable weights
    W_THRESHOLD = 0.30
    W_RECENCY = 0.15
    W_NARRATIVE = 0.25
    W_GAME_STATE = 0.15
    W_SOCIAL = 0.15

    def score(
        self,
        event: GameEvent,
        market: Market,
        game_state: dict,
        tweet_count: int = 0,
    ) -> float:
        signals = {
            "threshold_proximity": self._threshold_proximity(event, market),
            "recency": self._recency(event),
            "narrative_strength": self._narrative_strength(event),
            "game_state_urgency": self._game_state_urgency(game_state),
            "social_momentum": self._social_momentum(tweet_count),
        }

        score = (
            self.W_THRESHOLD * signals["threshold_proximity"]
            + self.W_RECENCY * signals["recency"]
            + self.W_NARRATIVE * signals["narrative_strength"]
            + self.W_GAME_STATE * signals["game_state_urgency"]
            + self.W_SOCIAL * signals["social_momentum"]
        )

        return round(min(1.0, max(0.0, score)), 3)

    def _threshold_proximity(self, event: GameEvent, market: Market) -> float:
        """How close is the player to the line? Higher = closer."""
        pct = event.data.get("percentage", 0)
        if pct >= 1.0:
            return 1.0  # Already past the line
        if pct >= 0.9:
            return 0.95
        if pct >= 0.75:
            return 0.7
        return pct * 0.6

    def _recency(self, event: GameEvent) -> float:
        """How recent is the event? Decays over time."""
        age = time.time() - event.timestamp
        if age < 30:
            return 1.0
        if age < 120:
            return 0.85
        if age < 300:
            return 0.6
        if age < 600:
            return 0.3
        return 0.1

    def _narrative_strength(self, event: GameEvent) -> float:
        """Is there a strong narrative? (milestone, record, rivalry)"""
        if event.event_type == EventType.MILESTONE:
            return 1.0
        if event.event_type == EventType.MOMENTUM_SHIFT:
            return 0.8
        if event.event_type == EventType.THRESHOLD_APPROACH:
            return 0.7
        if event.event_type == EventType.SCORE_CHANGE:
            return 0.5
        return 0.3

    def _game_state_urgency(self, game_state: dict) -> float:
        """Later in the game = more urgent."""
        period = game_state.get("period", "")
        minutes_remaining = game_state.get("minutes_remaining", 48)

        # NBA: Q4 with < 5 min = max urgency
        if "Q4" in period and minutes_remaining < 5:
            return 1.0
        if "Q4" in period or "Q3" in period:
            return 0.7
        # Football: 70+ minutes
        if minutes_remaining <= 20:
            return 0.8

        return 0.3 + (1 - minutes_remaining / 48) * 0.4

    def _social_momentum(self, tweet_count: int) -> float:
        """More tweets = more social signal."""
        if tweet_count >= 5:
            return 1.0
        if tweet_count >= 3:
            return 0.7
        if tweet_count >= 1:
            return 0.4
        return 0.1
