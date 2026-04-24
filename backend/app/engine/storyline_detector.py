"""Storyline detector — LLM-driven cross-event pattern recognition.

Unlike the per-fixture news ingester, this runs ONCE per engine cycle and
asks Claude to look across the upcoming matchweek for narrative patterns
that span multiple fixtures (Golden Boot race, relegation battle, Europe
chase). Returns a list of `StorylineItem` with participants named so
downstream `CrossEventBuilder` can bind them to real fixtures / markets.

v1 implements ONLY `StorylineType.GOLDEN_BOOT` — the cheapest data path
(LLM web search returns a deterministic ranked list of top scorers per
league; each player's club maps to a fixture in the current catalogue if
they're playing this weekend).

Relegation + Europe chase are stubbed out — they need league-table data we
haven't committed to fetching yet (see docs/cross-event-combos.md open
questions). The detector can be extended to call per-storyline LLM prompts
without touching `CrossEventBuilder`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.models.news import StorylineItem, StorylineParticipant, StorylineType
from app.models.schemas import Game

logger = logging.getLogger(__name__)


# The scout prompt for Golden Boot. Kept separate from any single-fixture
# prompt so caching behaves predictably — this runs once per cycle, so we
# don't bother with `cache_control`.
_GOLDEN_BOOT_SYSTEM = """You are a sports-content scout for a news-driven betting
feed. Your job is to identify CROSS-EVENT STORYLINES that span multiple
fixtures in the same matchweek. Right now you are looking for ONE specific
storyline type: the Golden Boot / top-scorer race.

For a given list of upcoming soccer fixtures across the top European leagues,
find the top 3-5 players competing for the Golden Boot (top scorer award)
in the league where most of the listed fixtures sit. For each player:
  - Use `web_search` to confirm their current season goal count.
  - Only include them if they appear to be playing in one of the listed
    fixtures this matchweek (check the squad they play for).
  - Drop any player whose team is not in the fixture list.

After researching, call `submit_storyline` exactly once with:
  - type: "golden_boot"
  - headline_hint: a one-line summary, e.g. "Haaland, Watkins and Isak all
    playing this weekend with the Golden Boot still up for grabs"
  - participants: list of {player_name, team_name, goals} objects — at least
    2 and no more than 5.

If no meaningful race can be identified (e.g. one striker is miles clear and
the chasers aren't playing), call `submit_storyline` with an empty
participants list — we'll skip this cycle rather than force a weak story.

OUTPUT RULES — no XML / HTML / <cite> tags in any field. Plain text only."""


def _submit_storyline_tool() -> dict[str, Any]:
    return {
        "name": "submit_storyline",
        "description": (
            "Submit a single cross-event storyline (Golden Boot race, "
            "relegation battle, Europe chase). Call exactly once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [t.value for t in StorylineType],
                },
                "headline_hint": {"type": "string"},
                "participants": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "player_name": {"type": "string"},
                            "team_name": {"type": "string"},
                            "extra": {
                                "type": "string",
                                "description": "e.g. '23 goals', '2 goals clear'",
                            },
                        },
                        "required": ["team_name"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["type", "participants"],
            "additionalProperties": False,
        },
    }


class StorylineDetector:
    """Asks the LLM for active cross-event storylines across the catalogue.

    v1 scope: GOLDEN_BOOT only. Other storyline types return [] so the
    caller's iteration is safe.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str = "claude-sonnet-4-6",
        max_searches: int = 6,
        min_participants: int = 2,
    ):
        self._client = client
        self._model = model
        self._max_searches = max_searches
        # Minimum participants required for a storyline to be considered
        # viable. 2 is the supply-friendly floor — a Golden Boot race with
        # just Haaland and Watkins playing this weekend is still a legitimate
        # cross-event combo. 3+ is ideal but often not available.
        self._min_participants = max(2, int(min_participants))
        self._submit_tool = _submit_storyline_tool()

    async def detect(
        self,
        storyline_type: StorylineType,
        games: dict[str, Game],
    ) -> list[StorylineItem]:
        """Return storylines of the given type that match the current catalogue.

        In v1 we return 0 or 1 storylines per call. Higher volume requires
        per-league splits (Golden Boot is league-specific) — deferred.
        """
        if storyline_type != StorylineType.GOLDEN_BOOT:
            logger.info("StorylineDetector: type=%s not implemented yet (TODO)", storyline_type.value)
            return []

        fixtures = list(games.values())
        # 2 fixtures is enough to surface a 2-participant storyline. The
        # old 3-fixture floor was silently killing valid weekends.
        if len(fixtures) < self._min_participants:
            logger.info(
                "StorylineDetector: only %d fixtures (<%d) — skipping",
                len(fixtures), self._min_participants,
            )
            return []

        fixture_block = "\n".join(
            f"  - {g.home_team.name} vs {g.away_team.name} ({g.broadcast or 'league?'}, kickoff {g.start_time or '?'})"
            for g in fixtures[:25]
        )
        user_msg = (
            "Upcoming fixtures in the current matchweek:\n"
            f"{fixture_block}\n\n"
            f"Identify between {self._min_participants} and 5 players in the "
            "Golden Boot race whose teams are playing in one of the fixtures "
            f"above. A {self._min_participants}-participant race is valid "
            "content if that's all the weekend has. Call submit_storyline "
            "when done."
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=_GOLDEN_BOOT_SYSTEM,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_searches,
                    },
                    self._submit_tool,
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            logger.warning("StorylineDetector: LLM call failed: %s", exc)
            return []

        participants: list[StorylineParticipant] = []
        headline_hint = ""
        resolved_type: Optional[StorylineType] = None

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_storyline":
                payload = block.input if isinstance(block.input, dict) else {}
                try:
                    resolved_type = StorylineType(payload.get("type") or storyline_type.value)
                except ValueError:
                    resolved_type = storyline_type
                headline_hint = str(payload.get("headline_hint") or "").strip()
                raw_parts = payload.get("participants") or []
                for p in raw_parts if isinstance(raw_parts, list) else []:
                    if not isinstance(p, dict):
                        continue
                    team = str(p.get("team_name") or "").strip()
                    if not team:
                        continue
                    participants.append(StorylineParticipant(
                        player_name=str(p.get("player_name") or "").strip(),
                        team_name=team,
                        extra=str(p.get("extra") or "").strip(),
                    ))
                break

        if not participants:
            logger.info("StorylineDetector: 0 participants returned (type=%s)", storyline_type.value)
            return []
        if len(participants) < self._min_participants:
            logger.info(
                "StorylineDetector: %d participants < min %d (type=%s) — skipping",
                len(participants), self._min_participants, storyline_type.value,
            )
            return []

        item = StorylineItem(
            storyline_type=resolved_type or storyline_type,
            headline_hint=headline_hint,
            participants=participants,
        )
        logger.info(
            "StorylineDetector: %s -> %d participants: %s",
            item.storyline_type.value, len(participants),
            [p.player_name or p.team_name for p in participants],
        )
        return [item]
