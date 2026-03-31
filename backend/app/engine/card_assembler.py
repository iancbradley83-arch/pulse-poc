"""Card Assembler — combines engine outputs into feed-ready Card objects."""
from __future__ import annotations

from app.models.schemas import (
    Card, CardType, BadgeType, GameEvent, Market, Game, Tweet,
    StatDisplay, ProgressDisplay, EventType,
)


class CardAssembler:
    """Assembles a Card from an event, market, narrative, and supporting context."""

    def assemble_prematch(
        self,
        game: Game,
        market: Market,
        narrative: str,
        stats: list[StatDisplay],
        tweets: list[Tweet],
        badge: BadgeType = BadgeType.TRENDING,
        relevance: float = 0.5,
        progress: ProgressDisplay | None = None,
    ) -> Card:
        return Card(
            card_type=CardType.PRE_MATCH,
            game=game,
            badge=badge,
            narrative_hook=narrative,
            stats=stats,
            progress=progress,
            tweets=tweets,
            market=market,
            relevance_score=relevance,
            ttl_seconds=3600,  # pre-match cards last longer
        )

    def assemble_live(
        self,
        event: GameEvent,
        game: Game,
        market: Market,
        narrative: str,
        stats: list[StatDisplay] | None = None,
        tweets: list[Tweet] | None = None,
        badge: BadgeType | None = None,
        relevance: float = 0.5,
        progress: ProgressDisplay | None = None,
    ) -> Card:
        # Determine badge from event type
        if badge is None:
            badge = {
                EventType.MILESTONE: BadgeType.MILESTONE,
                EventType.MOMENTUM_SHIFT: BadgeType.HOT,
                EventType.THRESHOLD_APPROACH: BadgeType.STAT,
                EventType.SCORE_CHANGE: BadgeType.TRENDING,
            }.get(event.event_type, BadgeType.TRENDING)

        # Build event trigger display
        event_trigger = {
            "icon": self._event_icon(event.event_type),
            "icon_type": self._event_icon_type(event.event_type),
            "what": event.description,
            "when": f"{game.clock}" if game.clock else "Just now",
        }

        return Card(
            card_type=CardType.LIVE_EVENT,
            game=game,
            badge=badge,
            event_trigger=event_trigger,
            narrative_hook=narrative,
            stats=stats or [],
            progress=progress,
            tweets=tweets or [],
            market=market,
            relevance_score=relevance,
            ttl_seconds=300,  # live cards expire faster
        )

    def _event_icon(self, event_type: EventType) -> str:
        return {
            EventType.SCORE_CHANGE: "\u26bd",  # or sport-specific
            EventType.THRESHOLD_APPROACH: "\ud83d\udcca",
            EventType.MOMENTUM_SHIFT: "\ud83d\udd25",
            EventType.MILESTONE: "\ud83c\udfc6",
            EventType.INJURY: "\ud83c\udfe5",
            EventType.STAT_UPDATE: "\ud83d\udcca",
        }.get(event_type, "\u26a1")

    def _event_icon_type(self, event_type: EventType) -> str:
        return {
            EventType.SCORE_CHANGE: "score",
            EventType.THRESHOLD_APPROACH: "stat",
            EventType.MOMENTUM_SHIFT: "momentum",
            EventType.MILESTONE: "milestone",
            EventType.INJURY: "injury",
        }.get(event_type, "score")
