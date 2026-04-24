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

Standings verification (post-scout pass)
----------------------------------------
For RELEGATION / EUROPE_CHASE the scout's word isn't enough — we've been
bitten by it miscasting a mid-table side as "fighting the drop". After
the scout emits participants, a second Haiku+web_search call verifies
each team's actual league_position and points_from_safety /
points_from_european_spot. Participants that fail the positional
threshold are dropped. If fewer than 2 valid participants survive, the
whole storyline is skipped — better to ship 0 relegation cards than 1
with a miscast team.

Within-league preference
------------------------
`detect_grouped_by_league()` splits fixtures by league first and tries
each league in isolation. Cross-league mixing only happens if no single
league has >=2 valid participants — within-league framing is easier to
write cohesively ("three Premier League clubs in the drop zone") than
cross-league ("one Premier League club and two La Liga clubs").

Other storyline types (MANAGER_PRESSURE, DEBUT_RETURN) are still stubbed
— the enum stays stable across database migrations.
"""
from __future__ import annotations

import json
import logging
import time
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

CRITICAL ACCURACY RULES — we lose user trust when we miscast teams:
  - A "relegation" participant must be a club that is CURRENTLY battling
    the drop. Mid-table clubs are NOT relegation candidates, regardless
    of how their season has gone recently. Do NOT include any team that
    is comfortably mid-table (roughly positions 8-12 in a 20-team
    league). When in doubt, LEAVE THEM OUT.
  - Never describe a still-competing team as "relegated" — they are
    "battling the drop", "relegation-threatened", "fighting to survive".
    Past-tense "relegated" means already down, which is wrong for any
    club still playing its league season.

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
    text. Prefer the SPECIFIC competition ("Champions League spot",
    "Europa Conference place") over the generic "Europa League".

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


# ── Standings verification ─────────────────────────────────────────────

# Simple process-wide cache so a team's standings aren't re-looked-up on
# every cycle. Key: (lowered_team_name, yyyy-mm-dd). Value:
# (timestamp_seconds, standings_dict). 12h TTL per spec.
_STANDINGS_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_STANDINGS_CACHE_TTL_SECONDS = 12 * 3600


def _cache_key(team: str) -> tuple[str, str]:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    return (team.strip().lower(), today)


def _cache_get(team: str) -> Optional[dict]:
    key = _cache_key(team)
    hit = _STANDINGS_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.time() - ts) > _STANDINGS_CACHE_TTL_SECONDS:
        _STANDINGS_CACHE.pop(key, None)
        return None
    return payload


def _cache_put(team: str, payload: dict) -> None:
    _STANDINGS_CACHE[_cache_key(team)] = (time.time(), payload)


_VERIFY_SYSTEM = """You verify CURRENT league standings for football teams.
Use `web_search` to look up today's table for each team you're asked about.
Return one row per team via the `submit_standings` tool. Required fields per
team: team_name (as given), league, league_position (int), league_size (int,
total clubs in the league — e.g. 20 for the Premier League), form_last_5
(string of WLD chars for the most recent 5 league matches, newest first;
empty string if you can't find it). Depending on context type, also fill
EITHER points_from_safety (for RELEGATION context — points above the drop
zone; 0 means inside it; negative ok if the team is below the line) OR
points_from_european_spot (for EUROPE_CHASE context — points from the
nearest European qualification line the team is chasing; 0 means on the
line). If you cannot confidently fill the numeric fields for a team, set
confident=false for that team; we'll drop it rather than ship a guess.

OUTPUT RULES — no XML / HTML / <cite> tags. Plain text strings only."""


def _submit_standings_tool() -> dict[str, Any]:
    return {
        "name": "submit_standings",
        "description": "Submit current standings rows for the requested teams. Call exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "teams": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "team_name": {"type": "string"},
                            "league": {"type": "string"},
                            "league_position": {"type": "integer"},
                            "league_size": {"type": "integer"},
                            "points_from_safety": {"type": "integer"},
                            "points_from_european_spot": {"type": "integer"},
                            "form_last_5": {"type": "string"},
                            "confident": {"type": "boolean"},
                        },
                        "required": ["team_name", "confident"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["teams"],
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
        verify_enabled: Optional[bool] = None,
        verify_model: Optional[str] = None,
        relegation_max_position: Optional[int] = None,
        relegation_max_points_from_safety: Optional[int] = None,
        europe_chase_min_position: Optional[int] = None,
        europe_chase_max_position: Optional[int] = None,
        europe_chase_max_points_from_spot: Optional[int] = None,
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

        # Standings-verification knobs. Pull from app.config so main.py
        # (on the MUST NOT touch list for this PR) doesn't have to thread
        # them through. Explicit constructor args still win for tests.
        from app.config import (
            PULSE_STORYLINE_STANDINGS_VERIFY_ENABLED,
            PULSE_STORYLINE_VERIFY_MODEL,
            PULSE_STORYLINE_RELEGATION_MAX_POSITION,
            PULSE_STORYLINE_RELEGATION_MAX_POINTS_FROM_SAFETY,
            PULSE_STORYLINE_EUROPE_CHASE_MIN_POSITION,
            PULSE_STORYLINE_EUROPE_CHASE_MAX_POSITION,
            PULSE_STORYLINE_EUROPE_CHASE_MAX_POINTS_FROM_SPOT,
        )
        self._verify_enabled = bool(
            PULSE_STORYLINE_STANDINGS_VERIFY_ENABLED
            if verify_enabled is None else verify_enabled
        )
        self._verify_model = (
            verify_model or PULSE_STORYLINE_VERIFY_MODEL
        )
        self._reg_max_pos = int(
            PULSE_STORYLINE_RELEGATION_MAX_POSITION
            if relegation_max_position is None else relegation_max_position
        )
        self._reg_max_pts = int(
            PULSE_STORYLINE_RELEGATION_MAX_POINTS_FROM_SAFETY
            if relegation_max_points_from_safety is None
            else relegation_max_points_from_safety
        )
        self._ec_min_pos = int(
            PULSE_STORYLINE_EUROPE_CHASE_MIN_POSITION
            if europe_chase_min_position is None else europe_chase_min_position
        )
        self._ec_max_pos = int(
            PULSE_STORYLINE_EUROPE_CHASE_MAX_POSITION
            if europe_chase_max_position is None else europe_chase_max_position
        )
        self._ec_max_pts = int(
            PULSE_STORYLINE_EUROPE_CHASE_MAX_POINTS_FROM_SPOT
            if europe_chase_max_points_from_spot is None
            else europe_chase_max_points_from_spot
        )
        self._standings_tool = _submit_standings_tool()

    # ── Public API ─────────────────────────────────────────────────────

    async def detect(
        self,
        storyline_type: StorylineType,
        games: dict[str, Game],
    ) -> list[StorylineItem]:
        """Return storylines of the given type that match the current catalogue.

        Returns 0 or 1 storylines per call. Caller iterates the enabled
        types and aggregates.

        For RELEGATION and EUROPE_CHASE this method now prefers
        within-league storylines (splits the fixture list by league,
        tries each league first). Cross-league mixing is only emitted
        when no within-league storyline has >=2 valid participants.
        GOLDEN_BOOT is unchanged — it was already league-scoped implicitly.
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
        if len(fixtures) < self._min_participants:
            logger.info(
                "StorylineDetector: only %d fixtures (<%d) — skipping",
                len(fixtures), self._min_participants,
            )
            return []

        # For RELEGATION / EUROPE_CHASE: try each league group first.
        # Within-league storylines frame more cohesively than cross-league
        # mashups (the "Mallorca + Forest" bug the standings-verification
        # PR was written to fix).
        if storyline_type in (StorylineType.RELEGATION, StorylineType.EUROPE_CHASE):
            groups = _group_fixtures_by_league(fixtures)
            # Try each league from largest group to smallest — more
            # fixtures = more likely to yield a valid storyline.
            sorted_groups = sorted(
                groups.items(), key=lambda kv: -len(kv[1]),
            )
            for league_name, league_fixtures in sorted_groups:
                if len(league_fixtures) < self._min_participants:
                    continue
                item = await self._detect_once(
                    storyline_type, league_fixtures,
                    system_prompt, user_hint_tpl,
                    scope_label=f"league={league_name}",
                )
                if item is not None:
                    logger.info(
                        "StorylineDetector: within-league storyline for "
                        "%s (league=%s, participants=%d)",
                        storyline_type.value, league_name,
                        len(item.participants),
                    )
                    return [item]
            # Fallback: cross-league. Keeps the feature useful when no
            # single league has enough valid candidates.
            logger.info(
                "StorylineDetector: no within-league storyline for "
                "%s — falling back to cross-league",
                storyline_type.value,
            )

        item = await self._detect_once(
            storyline_type, fixtures,
            system_prompt, user_hint_tpl,
            scope_label="cross-league",
        )
        return [item] if item is not None else []

    # ── Core scout + verify pipeline ───────────────────────────────────

    async def _detect_once(
        self,
        storyline_type: StorylineType,
        fixtures: list[Game],
        system_prompt: str,
        user_hint_tpl: str,
        *,
        scope_label: str,
    ) -> Optional[StorylineItem]:
        """Run one scout + verify pass over the given fixture subset."""
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
                "StorylineDetector: LLM call failed (type=%s, scope=%s): %s",
                storyline_type.value, scope_label, exc,
            )
            return None

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

        # Belt-and-braces: trust the type we asked for.
        if resolved_type is not None and resolved_type != storyline_type:
            logger.info(
                "StorylineDetector: LLM returned type=%s for asked=%s — "
                "overriding to asked type",
                resolved_type.value, storyline_type.value,
            )
            resolved_type = storyline_type

        if not participants:
            logger.info(
                "StorylineDetector: 0 participants returned (type=%s, scope=%s)",
                storyline_type.value, scope_label,
            )
            return None

        # Standings verification — only for RELEGATION and EUROPE_CHASE,
        # only when enabled. Drops participants that don't meet the
        # positional / points threshold.
        if self._verify_enabled and storyline_type in (
            StorylineType.RELEGATION, StorylineType.EUROPE_CHASE,
        ):
            participants = await self._verify_participants(
                storyline_type, participants,
            )

        if len(participants) < self._min_participants:
            logger.info(
                "StorylineDetector: %d participants < min %d (type=%s, scope=%s) — skipping",
                len(participants), self._min_participants,
                storyline_type.value, scope_label,
            )
            return None

        item = StorylineItem(
            storyline_type=resolved_type or storyline_type,
            headline_hint=headline_hint,
            participants=participants,
        )
        logger.info(
            "StorylineDetector: %s -> %d participants (scope=%s): %s",
            item.storyline_type.value, len(participants), scope_label,
            [p.player_name or p.team_name for p in participants],
        )
        return item

    # ── Standings verification ─────────────────────────────────────────

    async def _verify_participants(
        self,
        storyline_type: StorylineType,
        participants: list[StorylineParticipant],
    ) -> list[StorylineParticipant]:
        """Web-search the current standings for each participant. Drop
        teams that don't meet the positional / points threshold for the
        storyline type. Populates `participant_context` in-place on
        survivors.
        """
        if not participants:
            return participants

        # Split cache hits vs misses so we only web-search what we must.
        needs_lookup: list[StorylineParticipant] = []
        cached: dict[str, dict] = {}
        for p in participants:
            hit = _cache_get(p.team_name)
            if hit is not None:
                cached[p.team_name.lower()] = hit
            else:
                needs_lookup.append(p)

        fetched: dict[str, dict] = {}
        if needs_lookup:
            fetched = await self._fetch_standings(
                storyline_type, [p.team_name for p in needs_lookup],
            )
            for team_lower, row in fetched.items():
                # Cache regardless of confidence — even a non-confident
                # answer is worth skipping re-asking for 12h.
                _cache_put(team_lower, row)

        survivors: list[StorylineParticipant] = []
        for p in participants:
            row = cached.get(p.team_name.lower()) or fetched.get(p.team_name.lower())
            verdict = self._verify_row(storyline_type, row)
            logger.info(
                "StorylineDetector.verify: type=%s team=%s verdict=%s row=%s",
                storyline_type.value, p.team_name, verdict,
                _row_summary(row),
            )
            if verdict != "pass":
                continue
            # Attach the verified context for the narrative author.
            p.participant_context = _context_from_row(storyline_type, row)
            survivors.append(p)
        return survivors

    async def _fetch_standings(
        self,
        storyline_type: StorylineType,
        teams: list[str],
    ) -> dict[str, dict]:
        """Call Haiku+web_search to resolve standings for `teams`. Returns
        dict keyed by lowered team name. Missing teams are omitted.
        """
        if not teams:
            return {}
        context_type = (
            "RELEGATION — fill points_from_safety"
            if storyline_type == StorylineType.RELEGATION
            else "EUROPE_CHASE — fill points_from_european_spot"
        )
        team_list = "\n".join(f"  - {t}" for t in teams)
        user_msg = (
            f"Context type: {context_type}.\n"
            "Look up the CURRENT league standings for each team below and "
            "return one row per team. Use web_search for each team's league "
            "table (e.g. 'Premier League table today', 'La Liga standings "
            "current'). Be conservative — if you cannot find a confident "
            "answer for a given team, set confident=false for that row.\n\n"
            f"Teams:\n{team_list}\n"
        )
        try:
            resp = await self._client.messages.create(
                model=self._verify_model,
                max_tokens=1500,
                system=_VERIFY_SYSTEM,
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": max(self._max_searches, len(teams) + 2),
                    },
                    self._standings_tool,
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            logger.warning(
                "StorylineDetector.verify: LLM call failed (type=%s): %s",
                storyline_type.value, exc,
            )
            return {}

        out: dict[str, dict] = {}
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_standings":
                payload = block.input if isinstance(block.input, dict) else {}
                rows = payload.get("teams") or []
                for row in rows if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("team_name") or "").strip()
                    if not name:
                        continue
                    out[name.lower()] = row
                break
        return out

    def _verify_row(
        self, storyline_type: StorylineType, row: Optional[dict],
    ) -> str:
        """Return "pass", "no_data", "not_confident", or "off_threshold".
        Used for logging + filtering."""
        if not row:
            return "no_data"
        if not row.get("confident", False):
            return "not_confident"
        pos = row.get("league_position")
        size = row.get("league_size")
        if storyline_type == StorylineType.RELEGATION:
            pts_safety = row.get("points_from_safety")
            # Bottom-N threshold — team in bottom `relegation_max_position`
            # of its league.
            in_bottom_n = (
                isinstance(pos, int) and isinstance(size, int)
                and size > 0 and pos >= (size - self._reg_max_pos + 1)
            )
            close_to_drop = (
                isinstance(pts_safety, int)
                and pts_safety <= self._reg_max_pts
            )
            if in_bottom_n or close_to_drop:
                return "pass"
            return "off_threshold"
        if storyline_type == StorylineType.EUROPE_CHASE:
            pts_spot = row.get("points_from_european_spot")
            in_band = (
                isinstance(pos, int)
                and self._ec_min_pos <= pos <= self._ec_max_pos
            )
            close_to_spot = (
                isinstance(pts_spot, int)
                and pts_spot >= 0
                and pts_spot <= self._ec_max_pts
            )
            if in_band or close_to_spot:
                return "pass"
            return "off_threshold"
        return "pass"  # unreachable for other types (we don't verify them)


# ── Helpers ────────────────────────────────────────────────────────────


def _group_fixtures_by_league(fixtures: list[Game]) -> dict[str, list[Game]]:
    """Split fixtures by `Game.broadcast` which carries the league label
    from the catalogue loader (e.g. "Premier League", "La Liga"). Unknown
    / missing groups collapse into a single "unknown" bucket."""
    out: dict[str, list[Game]] = {}
    for g in fixtures:
        key = (g.broadcast or "unknown").strip() or "unknown"
        out.setdefault(key, []).append(g)
    return out


def _context_from_row(
    storyline_type: StorylineType, row: Optional[dict],
) -> dict:
    """Extract the narrative-author-facing subset from a verified row.
    Only carries fields we trust (no player names, no free-text extras).
    """
    if not row:
        return {}
    ctx = {
        "league": str(row.get("league") or "").strip(),
        "form_last_5": str(row.get("form_last_5") or "").strip(),
    }
    pos = row.get("league_position")
    size = row.get("league_size")
    if isinstance(pos, int):
        ctx["league_position"] = pos
    if isinstance(size, int):
        ctx["league_size"] = size
    if storyline_type == StorylineType.RELEGATION:
        pts_safety = row.get("points_from_safety")
        if isinstance(pts_safety, int):
            ctx["points_from_safety"] = pts_safety
    elif storyline_type == StorylineType.EUROPE_CHASE:
        pts_spot = row.get("points_from_european_spot")
        if isinstance(pts_spot, int):
            ctx["points_from_european_spot"] = pts_spot
    # Drop empty strings so the author doesn't see {"league": ""}.
    return {k: v for k, v in ctx.items() if v not in ("", None)}


def _row_summary(row: Optional[dict]) -> str:
    """Compact log-friendly summary of a standings row."""
    if not row:
        return "None"
    try:
        keep = {
            k: row.get(k)
            for k in (
                "league", "league_position", "league_size",
                "points_from_safety", "points_from_european_spot",
                "form_last_5", "confident",
            )
            if k in row
        }
        return json.dumps(keep, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(row)[:120]
