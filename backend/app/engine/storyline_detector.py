"""Storyline detector — LLM-driven cross-event pattern recognition.

Unlike the per-fixture news ingester, this runs ONCE per engine cycle and
asks Claude to look across the upcoming matchweek for narrative patterns
that span multiple fixtures (Golden Boot race, relegation battle, Europe
chase). Returns a list of `StorylineItem` with participants named so
downstream `CrossEventBuilder` can bind them to real fixtures / markets.

Three storyline types are wired:
  - GOLDEN_BOOT — top-scorer race, each participant is a striker + team
  - RELEGATION — bottom-of-table sides all playing same matchweek, each
    participant is a team (no player needed)
  - EUROPE_CHASE — teams fighting for 4th/5th/Europa / Conference spot,
    each participant is a team

Each type uses its own system prompt tailored to the data the LLM needs
to web-search for (top-scorer leaderboard vs league standings). The
`submit_storyline` tool shape is shared — `participants[].player_name` is
optional so relegation / europe-chase returns work without a player.

Other storyline types (MANAGER_PRESSURE, DEBUT_RETURN) are still stubbed
— the enum stays stable across database migrations.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.models.news import StorylineItem, StorylineParticipant, StorylineType
from app.models.schemas import Game

logger = logging.getLogger(__name__)


# ── Per-type system prompts ────────────────────────────────────────────
#
# Each prompt names ONE storyline type and tells the LLM what to
# web-search for + what shape of participants to submit. Kept separate
# so we don't confuse the model with three storylines at once.


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
  - participants: list of {player_name, team_name, extra} objects — at least
    2 and no more than 5.

If no meaningful race can be identified (e.g. one striker is miles clear and
the chasers aren't playing), call `submit_storyline` with an empty
participants list — we'll skip this cycle rather than force a weak story.

OUTPUT RULES — no XML / HTML / <cite> tags in any field. Plain text only."""


_RELEGATION_SYSTEM = """You are a sports-content scout for a news-driven betting
feed. Your job is to identify CROSS-EVENT STORYLINES. Right now you are
looking for ONE specific type: the RELEGATION BATTLE — bottom-of-the-table
clubs all playing the same matchweek.

For the given list of upcoming soccer fixtures (typically a single league
matchweek), use `web_search` to look up the CURRENT league standings. Focus
on the bottom 4 clubs of whichever league is represented by most of the
listed fixtures (Premier League, La Liga, Serie A, Bundesliga, Ligue 1).
Query shape: "current {league} standings bottom 4 relegation zone".

Then cross-reference with the fixture list: which of those bottom-4 clubs
are actually playing this matchweek? For each at-risk club that IS playing:
  - Include as a participant with `team_name` = the at-risk club's name as
    written in the fixture list (so downstream matching works).
  - Leave `player_name` empty.
  - Put the current league position in `extra` ("17th, 3pts above drop",
    "bottom of the table", "18th after losing 5 of last 6"). Free text.

After researching, call `submit_storyline` exactly once with:
  - type: "relegation"
  - headline_hint: a one-line summary, e.g. "Three clubs in the drop zone
    all play this weekend — survival Sunday"
  - participants: 2-5 at-risk clubs that are playing this matchweek.

If fewer than 2 at-risk clubs are playing, call `submit_storyline` with an
empty participants list — skip this cycle rather than force a weak story.

OUTPUT RULES — no XML / HTML / <cite> tags in any field. Plain text only."""


_EUROPE_CHASE_SYSTEM = """You are a sports-content scout for a news-driven betting
feed. Your job is to identify CROSS-EVENT STORYLINES. Right now you are
looking for ONE specific type: the EUROPE CHASE — clubs fighting for
Champions League / Europa / Conference League qualification all playing
the same matchweek.

For the given list of upcoming soccer fixtures (typically a single league
matchweek), use `web_search` to look up the CURRENT league standings. Focus
on the clubs currently clustered around the European qualification line —
4th / 5th / 6th / 7th in the league where most fixtures sit. Query shape:
"current {league} standings top 7 Champions League Europa race".

Then cross-reference with the fixture list: which of those chasing clubs
are actually playing this matchweek? For each chaser that IS playing:
  - Include as a participant with `team_name` = the chaser's name as
    written in the fixture list.
  - Leave `player_name` empty.
  - Put the position and the race they're in in `extra` ("5th, one point
    off the top four", "6th, Europa spot", "7th, Conference line"). Free
    text.

After researching, call `submit_storyline` exactly once with:
  - type: "europe_chase"
  - headline_hint: a one-line summary, e.g. "Four clubs chasing the
    top-four with one game in hand each"
  - participants: 2-5 chasing clubs that are playing this matchweek.

If fewer than 2 chasers are playing, call `submit_storyline` with an empty
participants list — skip this cycle rather than force a weak story.

OUTPUT RULES — no XML / HTML / <cite> tags in any field. Plain text only."""


# Per-type registry — add a new storyline type by appending a row here
# (prompt + user-hint template). Keeps `detect()` a thin dispatch.
_PROMPT_REGISTRY: dict[StorylineType, str] = {
    StorylineType.GOLDEN_BOOT: _GOLDEN_BOOT_SYSTEM,
    StorylineType.RELEGATION: _RELEGATION_SYSTEM,
    StorylineType.EUROPE_CHASE: _EUROPE_CHASE_SYSTEM,
}


_USER_HINT: dict[StorylineType, str] = {
    StorylineType.GOLDEN_BOOT: (
        "Identify between {min_p} and 5 players in the Golden Boot race "
        "whose teams are playing in one of the fixtures above. A {min_p}"
        "-participant race is valid content if that's all the weekend has. "
        "Call submit_storyline when done."
    ),
    StorylineType.RELEGATION: (
        "Identify between {min_p} and 5 bottom-of-the-table clubs that are "
        "playing in one of the fixtures above. Use web_search for current "
        "standings. Call submit_storyline when done."
    ),
    StorylineType.EUROPE_CHASE: (
        "Identify between {min_p} and 5 clubs chasing Champions League / "
        "Europa / Conference spots that are playing in one of the fixtures "
        "above. Use web_search for current standings. Call submit_storyline "
        "when done."
    ),
}


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
                                "description": "e.g. '23 goals', '17th in the table', 'one point off the top four'",
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

    Supports GOLDEN_BOOT, RELEGATION, and EUROPE_CHASE. Other storyline
    types return [] so the caller's iteration is safe — the enum stays
    stable for database compat.
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

        Returns 0 or 1 storylines per call. Caller iterates the enabled
        types and aggregates.
        """
        system_prompt = _PROMPT_REGISTRY.get(storyline_type)
        user_hint_tpl = _USER_HINT.get(storyline_type)
        if system_prompt is None or user_hint_tpl is None:
            logger.info(
                "StorylineDetector: type=%s not implemented yet (TODO)",
                storyline_type.value,
            )
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
            + user_hint_tpl.format(min_p=self._min_participants)
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system_prompt,
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
            logger.warning(
                "StorylineDetector: LLM call failed (type=%s): %s",
                storyline_type.value, exc,
            )
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

        # Belt-and-braces: if the LLM submitted a different type to the one
        # we asked for, trust the requested type — the detector call is the
        # source of truth. Keeps downstream leg-picking consistent.
        if resolved_type is not None and resolved_type != storyline_type:
            logger.info(
                "StorylineDetector: LLM returned type=%s for asked=%s — "
                "overriding to asked type",
                resolved_type.value, storyline_type.value,
            )
            resolved_type = storyline_type

        if not participants:
            logger.info(
                "StorylineDetector: 0 participants returned (type=%s)",
                storyline_type.value,
            )
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
