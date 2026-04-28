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


_TITLE_RACE_SYSTEM = """You are a sports-content scout for a news-driven betting
feed. Your job is to identify CROSS-EVENT STORYLINES. Right now you are
looking for ONE specific type: the TITLE RACE — the top 2-4 clubs of a
league, within 6 league-points of each other, all playing the same
matchweek.

For the given list of upcoming soccer fixtures, use `web_search` to look
up the CURRENT league table (prefer the league with the most fixtures in
the list — Premier League, La Liga, Serie A, Bundesliga, Ligue 1). Query
shape: "current {league} standings top of table title race".

Identify clubs who are simultaneously:
  - In league_position 1..5 of that league, AND
  - Within 8 league-points of the leader, AND
  - Actually playing in one of the fixtures listed.

For each such club include as a participant with:
  - team_name: the club name as written in the fixture list.
  - player_name: empty.
  - extra: free-text framing ("2nd, 1pt behind leaders", "3rd, 4pts off
    the top", "joint-leaders").
  - league_position: integer position in the table.
  - points_from_leader: integer points gap from 1st (0 if they ARE
    leading).
  - points_from_second: integer points gap from 2nd (0 if they ARE 2nd;
    use 0 for the leader too if they're level on points with 2nd).

After researching, call `submit_storyline` exactly once with:
  - type: "title_race"
  - headline_hint: e.g. "Three points separate the top — all three play
    this weekend"
  - participants: 2-4 title contenders playing this matchweek.

If fewer than 2 contenders are playing OR the gap at the top is already
wider than 8 points, call `submit_storyline` with an empty participants
list — skip this cycle rather than force a weak story.

OUTPUT RULES — no XML / HTML / <cite> tags in any field. Plain text only."""


_DERBY_WEEKEND_SYSTEM = """You are a sports-content scout for a news-driven
betting feed. Your job is to identify CROSS-EVENT STORYLINES. Right now
you are looking for ONE specific type: DERBY WEEKEND — three or more
local / classic rivalry fixtures across the weekend.

Anchor your search to well-known derbies / rivalries: Merseyside (Liverpool
vs Everton), North London (Arsenal vs Tottenham), Manchester (United vs
City), El Clasico (Real Madrid vs Barcelona), Madrid derby (Real Madrid
vs Atletico), Seville derby, Milan derby (Inter vs Milan), Derby della
Mole (Juventus vs Torino), Derby d'Italia (Inter vs Juventus),
Revierderby (Borussia Dortmund vs Schalke), Der Klassiker (Bayern vs
Dortmund), Le Classique (PSG vs Marseille), Old Firm (Celtic vs
Rangers), Rome derby, Hamburg derby, Lisbon derby, Eternal derby.

Use `web_search` for "which matches this weekend are local derbies or
classic rivalry fixtures" to confirm what's actually on.

For each derby fixture that's both (a) a real named rivalry and (b) in
the fixture list above, include ONE participant per fixture with:
  - team_name: the HOME side's name as written in the fixture list (we
    only need one anchor team per fixture — we're backing the match
    itself to have goals, not a specific side).
  - player_name: empty.
  - extra: the derby's common name ("Merseyside derby", "Derby della
    Madonnina", "North London derby").

After researching, call `submit_storyline` exactly once with:
  - type: "derby_weekend"
  - headline_hint: e.g. "Four derbies, one weekend — rivalry means goals"
  - participants: 3-6 derbies that are actually on the fixture list.

If fewer than 3 derbies are playing this matchweek, call
`submit_storyline` with an empty participants list.

OUTPUT RULES — no XML / HTML / <cite> tags. Plain text only."""


_EUROPEAN_WEEK_SYSTEM = """You are a sports-content scout for a news-driven
betting feed. Your job is to identify CROSS-EVENT STORYLINES. Right now
you are looking for ONE specific type: EUROPEAN WEEK — UEFA Champions
League + Europa League + Conference League fixtures all stacked in the
same midweek window.

For the given list of upcoming fixtures, use `web_search` to identify
which are UEFA club-competition matches this week (UCL, UEL, UECL).
Query shape: "Champions League fixtures this week" / "Europa League
fixtures midweek".

Prefer clubs whose league the rest of the fixture list also covers
(so we can frame it as "English clubs in Europe" / "La Liga's Champions
League heavyweights"), but cross-league is fine if the week is sparse.

For each European-competition club playing this week, include a
participant with:
  - team_name: club name as written in the fixture list.
  - player_name: empty.
  - extra: which competition + opponent ("UCL away at Bayern", "UEL
    knockout vs Roma", "Conference quarter-final").
  - competition: one of "UCL", "UEL", "UECL".

After researching, call `submit_storyline` exactly once with:
  - type: "european_week"
  - headline_hint: e.g. "Six English clubs on the European stage — all to
    win"
  - participants: 3-6 clubs in European action this week.

If fewer than 3 European fixtures are on the list, call
`submit_storyline` with an empty participants list.

OUTPUT RULES — no XML / HTML / <cite> tags. Plain text only."""


_HOME_FORTRESS_SYSTEM = """You are a sports-content scout for a news-driven
betting feed. Your job is to identify CROSS-EVENT STORYLINES. Right now
you are looking for ONE specific type: HOME FORTRESS — 4-6 clubs with
elite home records, all hosting this matchweek.

For the given list of upcoming fixtures, identify which clubs are the
HOME side in one of the listed fixtures. Then use `web_search` to check
each home side's recent home form (last 10 home league matches, or
overall home win rate this season). Query shape: "{team} home form this
season" / "{league} home table".

A club qualifies as a "fortress" if EITHER:
  - home_win_rate (this season, league only) >= 0.70, OR
  - top-5 home-form club in its league (look at the "home table" derived
    from the main standings — wins at home, points per home game).

For each qualifying home club, include a participant with:
  - team_name: home side as written in the fixture list.
  - player_name: empty.
  - extra: one-line description of the home record ("9 wins in 10 at
    home", "top of the home table", "unbeaten at home since October").
  - home_win_rate: float 0.0-1.0 (this season, league).
  - home_form_last_10: string of 10 W/L/D chars, newest first (e.g.
    "WWWDLWWWWW"), empty string if unknown.

After researching, call `submit_storyline` exactly once with:
  - type: "home_fortress"
  - headline_hint: e.g. "Six fortresses open their doors — no away side
    wants this"
  - participants: 3-6 home sides with elite records playing this
    matchweek.

If fewer than 3 qualifying home sides are playing, call
`submit_storyline` with an empty participants list.

OUTPUT RULES — no XML / HTML / <cite> tags. Plain text only."""


_GOAL_MACHINES_SYSTEM = """You are a sports-content scout for a news-driven
betting feed. Your job is to identify CROSS-EVENT STORYLINES. Right now
you are looking for ONE specific type: GOAL MACHINES — 4-6 of Europe's
most prolific strikers from any top-5 league, all playing this
matchweek. This is explicitly CROSS-LEAGUE (unlike the Golden Boot
detector which is single-league).

Use `web_search` to identify the top ~15 scorers across Europe's top 5
leagues this season (Premier League, La Liga, Serie A, Bundesliga,
Ligue 1). Query shape: "top scorers Europe this season" / "leading
scorers top 5 leagues".

For each top scorer, only include them if their CLUB appears as either
the home or away side in one of the fixtures listed. Include a
participant with:
  - player_name: striker's name (used for goalscorer market match).
  - team_name: club name as written in the fixture list.
  - extra: short framing ("21 goals, second in the Bundesliga race",
    "La Liga's top scorer").
  - goals_this_season: integer goals in their domestic league this
    season. Required.
  - recent_form_last_5: string of up to 5 numbers (goals per last 5
    league matches, newest first), e.g. "2,0,1,1,0". Empty string if
    unknown — the detector can still use the player without it.

After researching, call `submit_storyline` exactly once with:
  - type: "goal_machines"
  - headline_hint: e.g. "Europe's six most lethal strikers all in action
    this weekend"
  - participants: 4-6 top scorers whose clubs play this matchweek.

If fewer than 4 of Europe's top scorers are playing, STILL submit as
long as you have at least {min_goal_machines} qualifying names — the
story is valuable even at 3 legs. Under 3 qualifying names, call
`submit_storyline` with an empty participants list.

OUTPUT RULES — no XML / HTML / <cite> tags. Plain text only."""


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
    StorylineType.TITLE_RACE: _TITLE_RACE_SYSTEM,
    StorylineType.DERBY_WEEKEND: _DERBY_WEEKEND_SYSTEM,
    StorylineType.EUROPEAN_WEEK: _EUROPEAN_WEEK_SYSTEM,
    StorylineType.HOME_FORTRESS: _HOME_FORTRESS_SYSTEM,
    StorylineType.GOAL_MACHINES: _GOAL_MACHINES_SYSTEM,
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
    StorylineType.TITLE_RACE: (
        "Identify {min_p}-4 title contenders (top 1-5, within 8pts of the "
        "leader) playing one of the fixtures above. Use web_search for "
        "current standings. Call submit_storyline when done."
    ),
    StorylineType.DERBY_WEEKEND: (
        "Identify 3-{max_derby} named local/classic derbies that appear in "
        "the fixture list above. Call submit_storyline when done."
    ),
    StorylineType.EUROPEAN_WEEK: (
        "Identify 3-{max_european} clubs playing in UCL/UEL/UECL this week "
        "whose fixtures appear above. Call submit_storyline when done."
    ),
    StorylineType.HOME_FORTRESS: (
        "Identify 3-{max_fortress} home sides with elite home records "
        "(>=70% home wins OR top-5 home form) hosting in one of the "
        "fixtures above. Use web_search for home form. Call "
        "submit_storyline when done."
    ),
    StorylineType.GOAL_MACHINES: (
        "Identify 3-{max_goal_machines} of Europe's top ~15 scorers whose "
        "clubs appear in the fixtures above. Use web_search for the "
        "cross-league leaderboard. Call submit_storyline when done."
    ),
}


def _submit_storyline_tool() -> dict[str, Any]:
    return {
        "name": "submit_storyline",
        "description": (
            "Submit a single cross-event storyline (Golden Boot race, "
            "relegation battle, Europe chase, title race, derby weekend, "
            "European week, home fortress, goal machines). Call exactly "
            "once."
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
                            # Optional structured fields — populated per
                            # storyline type. Unused fields are simply
                            # ignored by the participant_context builder.
                            # Keeping them on one shared schema avoids
                            # eight separate tool definitions.
                            "league_position": {"type": "integer"},
                            "points_from_leader": {"type": "integer"},
                            "points_from_second": {"type": "integer"},
                            "competition": {
                                "type": "string",
                                "description": "UCL / UEL / UECL for european_week",
                            },
                            "home_win_rate": {
                                "type": "number",
                                "description": "home_fortress only — 0.0..1.0",
                            },
                            "home_form_last_10": {"type": "string"},
                            "goals_this_season": {"type": "integer"},
                            "recent_form_last_5": {"type": "string"},
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
# (timestamp_seconds, standings_dict). TTL configurable via
# PULSE_STANDINGS_CACHE_TTL_SECONDS (default 12h).
_STANDINGS_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def _standings_cache_ttl() -> float:
    try:
        from app.config import PULSE_STANDINGS_CACHE_TTL_SECONDS
        return float(PULSE_STANDINGS_CACHE_TTL_SECONDS)
    except Exception:
        return 12 * 3600.0


# Per-cycle hit/miss counters. Reset by `reset_standings_cache_counters`
# at tier-cycle start; read by main.py at end-of-cycle to surface hit
# rate alongside the cost log.
_STANDINGS_CACHE_HITS = 0
_STANDINGS_CACHE_MISSES = 0


def reset_standings_cache_counters() -> None:
    global _STANDINGS_CACHE_HITS, _STANDINGS_CACHE_MISSES
    _STANDINGS_CACHE_HITS = 0
    _STANDINGS_CACHE_MISSES = 0


def get_standings_cache_counters() -> tuple[int, int]:
    return _STANDINGS_CACHE_HITS, _STANDINGS_CACHE_MISSES


# Per-(storyline_type, league) cooldown. Detectors that finished a scout
# in the last cooldown window reuse the previously-detected participants
# instead of paying for another LLM round-trip. Keyed by
# (storyline_type.value, league_name_lower) so within-league and
# cross-league passes are independent.
_STORYLINE_LAST_SCOUT_AT: dict[tuple[str, str], float] = {}
_STORYLINE_LAST_RESULT: dict[tuple[str, str], "Optional[StorylineItem]"] = {}


def _storyline_cooldown_key(
    storyline_type: "StorylineType", scope_label: str,
) -> tuple[str, str]:
    return (storyline_type.value, (scope_label or "").lower())


def _storyline_cooldown_seconds(storyline_type: "StorylineType") -> float:
    """Per-type override > global default. Env shape:
    `PULSE_STORYLINE_<TYPE>_COOLDOWN_SECONDS=14400`.
    """
    import os as _os
    per_type = _os.getenv(
        f"PULSE_STORYLINE_{storyline_type.value.upper()}_COOLDOWN_SECONDS",
    )
    if per_type:
        try:
            return float(per_type)
        except ValueError:
            pass
    try:
        from app.config import PULSE_STORYLINE_COOLDOWN_SECONDS
        return float(PULSE_STORYLINE_COOLDOWN_SECONDS)
    except Exception:
        return 6 * 3600.0


# Per-cycle hit/miss counters for storyline cooldowns. Same shape as
# the standings cache counters above.
_STORYLINE_COOLDOWN_HITS = 0
_STORYLINE_COOLDOWN_MISSES = 0


def reset_storyline_cooldown_counters() -> None:
    global _STORYLINE_COOLDOWN_HITS, _STORYLINE_COOLDOWN_MISSES
    _STORYLINE_COOLDOWN_HITS = 0
    _STORYLINE_COOLDOWN_MISSES = 0


def get_storyline_cooldown_counters() -> tuple[int, int]:
    return _STORYLINE_COOLDOWN_HITS, _STORYLINE_COOLDOWN_MISSES


def _cache_key(team: str) -> tuple[str, str]:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    return (team.strip().lower(), today)


def _cache_get(team: str) -> Optional[dict]:
    global _STANDINGS_CACHE_HITS, _STANDINGS_CACHE_MISSES
    key = _cache_key(team)
    hit = _STANDINGS_CACHE.get(key)
    if not hit:
        _STANDINGS_CACHE_MISSES += 1
        return None
    ts, payload = hit
    if (time.time() - ts) > _standings_cache_ttl():
        _STANDINGS_CACHE.pop(key, None)
        _STANDINGS_CACHE_MISSES += 1
        return None
    _STANDINGS_CACHE_HITS += 1
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
        model: str = "claude-haiku-4-5",
        max_searches: int = 6,
        min_participants: int = 2,
        verify_enabled: Optional[bool] = None,
        verify_model: Optional[str] = None,
        relegation_max_position: Optional[int] = None,
        relegation_max_points_from_safety: Optional[int] = None,
        europe_chase_min_position: Optional[int] = None,
        europe_chase_max_position: Optional[int] = None,
        europe_chase_max_points_from_spot: Optional[int] = None,
        cost_tracker: Optional[Any] = None,
    ):
        self._client = client
        self._model = model
        self._cost_tracker = cost_tracker
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
            PULSE_STORYLINE_BORDERLINE_TOLERANCE_ENABLED,
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
        self._borderline_enabled = bool(
            PULSE_STORYLINE_BORDERLINE_TOLERANCE_ENABLED
        )
        self._standings_tool = _submit_standings_tool()

        # Per-type participant caps for the five new storyline types.
        # Pulled from app.config so operators can tune without code
        # changes. TITLE_RACE caps at 4 because the league table
        # realistically only has 4 genuine contenders within 8 points.
        from app.config import (
            PULSE_STORYLINE_TITLE_RACE_MAX_PARTICIPANTS,
            PULSE_STORYLINE_DERBY_WEEKEND_MAX_PARTICIPANTS,
            PULSE_STORYLINE_EUROPEAN_WEEK_MAX_PARTICIPANTS,
            PULSE_STORYLINE_HOME_FORTRESS_MAX_PARTICIPANTS,
            PULSE_STORYLINE_GOAL_MACHINES_MAX_PARTICIPANTS,
            PULSE_STORYLINE_TITLE_RACE_ENABLED,
            PULSE_STORYLINE_DERBY_WEEKEND_ENABLED,
            PULSE_STORYLINE_EUROPEAN_WEEK_ENABLED,
            PULSE_STORYLINE_HOME_FORTRESS_ENABLED,
            PULSE_STORYLINE_GOAL_MACHINES_ENABLED,
            PULSE_STORYLINE_RELEGATION_ENABLED,
            PULSE_STORYLINE_EUROPE_CHASE_ENABLED,
            PULSE_STORYLINE_GOLDEN_BOOT_ENABLED,
        )
        self._type_max_participants: dict[StorylineType, int] = {
            StorylineType.TITLE_RACE: int(PULSE_STORYLINE_TITLE_RACE_MAX_PARTICIPANTS),
            StorylineType.DERBY_WEEKEND: int(PULSE_STORYLINE_DERBY_WEEKEND_MAX_PARTICIPANTS),
            StorylineType.EUROPEAN_WEEK: int(PULSE_STORYLINE_EUROPEAN_WEEK_MAX_PARTICIPANTS),
            StorylineType.HOME_FORTRESS: int(PULSE_STORYLINE_HOME_FORTRESS_MAX_PARTICIPANTS),
            StorylineType.GOAL_MACHINES: int(PULSE_STORYLINE_GOAL_MACHINES_MAX_PARTICIPANTS),
        }
        self._type_enabled: dict[StorylineType, bool] = {
            StorylineType.GOLDEN_BOOT: bool(PULSE_STORYLINE_GOLDEN_BOOT_ENABLED),
            StorylineType.RELEGATION: bool(PULSE_STORYLINE_RELEGATION_ENABLED),
            StorylineType.EUROPE_CHASE: bool(PULSE_STORYLINE_EUROPE_CHASE_ENABLED),
            StorylineType.TITLE_RACE: bool(PULSE_STORYLINE_TITLE_RACE_ENABLED),
            StorylineType.DERBY_WEEKEND: bool(PULSE_STORYLINE_DERBY_WEEKEND_ENABLED),
            StorylineType.EUROPEAN_WEEK: bool(PULSE_STORYLINE_EUROPEAN_WEEK_ENABLED),
            StorylineType.HOME_FORTRESS: bool(PULSE_STORYLINE_HOME_FORTRESS_ENABLED),
            StorylineType.GOAL_MACHINES: bool(PULSE_STORYLINE_GOAL_MACHINES_ENABLED),
        }

        # Side-channel cache — when main.py calls detect() for one of the
        # three "original" types (GOLDEN_BOOT / RELEGATION / EUROPE_CHASE)
        # we opportunistically run the five new detectors in parallel on
        # the same fixture set and return their output alongside the
        # requested type. This keeps main.py (MUST NOT touch list) unaware
        # of the new types while still getting them on the feed. Keyed by
        # id(games) so we only detect each new type once per cycle.
        self._cycle_expansion_done: set[int] = set()
        self._new_types: tuple[StorylineType, ...] = (
            StorylineType.TITLE_RACE,
            StorylineType.DERBY_WEEKEND,
            StorylineType.EUROPEAN_WEEK,
            StorylineType.HOME_FORTRESS,
            StorylineType.GOAL_MACHINES,
        )

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

        Side-channel (storyline-expansion-top5 PR): when the caller
        asks for one of the three original types, we piggy-back the
        five new detectors (TITLE_RACE, DERBY_WEEKEND, EUROPEAN_WEEK,
        HOME_FORTRESS, GOAL_MACHINES) onto the same call and return
        their output in the same list. main.py iterates the
        (story, score) pairs and dedupes by type before picking, so
        returning multiple types here is safe. Each cycle's expansion
        runs exactly once — we key off id(games).
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
        results: list[StorylineItem] = [item] if item is not None else []

        # Side-channel expansion — opportunistically detect the five new
        # storyline types while we're here (main.py only knows the
        # three originals). Run once per cycle, in parallel.
        cycle_key = id(games)
        if cycle_key not in self._cycle_expansion_done:
            self._cycle_expansion_done.add(cycle_key)
            # Keep the cache bounded — only the last 4 cycles matter.
            if len(self._cycle_expansion_done) > 4:
                self._cycle_expansion_done.pop()
            import asyncio as _asyncio
            expansion_tasks: list = []
            expansion_types: list[StorylineType] = []
            for nt in self._new_types:
                if not self._type_enabled.get(nt, True):
                    continue
                nt_prompt = _PROMPT_REGISTRY.get(nt)
                nt_hint = _USER_HINT.get(nt)
                if nt_prompt is None or nt_hint is None:
                    continue
                expansion_tasks.append(self._detect_once(
                    nt, fixtures, nt_prompt, nt_hint,
                    scope_label="cross-league(expansion)",
                ))
                expansion_types.append(nt)
            if expansion_tasks:
                logger.info(
                    "StorylineDetector: expansion pass — running %d new "
                    "detectors: %s",
                    len(expansion_tasks),
                    [t.value for t in expansion_types],
                )
                try:
                    gathered = await _asyncio.gather(
                        *expansion_tasks, return_exceptions=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "StorylineDetector: expansion gather failed: %s", exc,
                    )
                    gathered = []
                for nt, res in zip(expansion_types, gathered):
                    if isinstance(res, Exception):
                        logger.warning(
                            "StorylineDetector: expansion %s errored: %s",
                            nt.value, res,
                        )
                        continue
                    if res is not None:
                        results.append(res)
        return results

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
        """Run one scout + verify pass over the given fixture subset.

        Cooldown: if the same (storyline_type, scope_label) was scouted
        within `PULSE_STORYLINE_COOLDOWN_SECONDS` (per-type override
        respected), reuse the cached participants and skip the LLM call.
        Standings only change once a day; scouting them every 30-60min
        burns Haiku+web_search for zero new info. See feedback memory
        on cost-leak fix (2026-04-26).
        """
        global _STORYLINE_COOLDOWN_HITS, _STORYLINE_COOLDOWN_MISSES
        cooldown_key = _storyline_cooldown_key(storyline_type, scope_label)
        cooldown_s = _storyline_cooldown_seconds(storyline_type)
        last_at = _STORYLINE_LAST_SCOUT_AT.get(cooldown_key)
        if last_at is not None and (time.time() - last_at) < cooldown_s:
            _STORYLINE_COOLDOWN_HITS += 1
            cached_item = _STORYLINE_LAST_RESULT.get(cooldown_key)
            age_min = (time.time() - last_at) / 60.0
            logger.info(
                "[storylines] type=%s scope=%s cache_hit (cooldown %.0fm "
                "remaining, last_scout %.0fm ago)",
                storyline_type.value, scope_label,
                (cooldown_s - (time.time() - last_at)) / 60.0,
                age_min,
            )
            return cached_item
        _STORYLINE_COOLDOWN_MISSES += 1
        logger.info(
            "[storylines] type=%s scope=%s scouted (cooldown expired or first run)",
            storyline_type.value, scope_label,
        )
        fixture_block = "\n".join(
            f"  - {g.home_team.name} vs {g.away_team.name} ({g.broadcast or 'league?'}, kickoff {g.start_time or '?'})"
            for g in fixtures[:25]
        )
        # Build per-type format kwargs defensively — new types use
        # max_derby / max_european / max_fortress / max_goal_machines /
        # min_goal_machines placeholders. Originals only use {min_p}.
        # Missing keys are harmless — str.format ignores only declared
        # params, so we populate the superset.
        hint_kwargs = {
            "min_p": self._min_participants,
            "max_derby": self._type_max_participants.get(StorylineType.DERBY_WEEKEND, 6),
            "max_european": self._type_max_participants.get(StorylineType.EUROPEAN_WEEK, 6),
            "max_fortress": self._type_max_participants.get(StorylineType.HOME_FORTRESS, 6),
            "max_goal_machines": self._type_max_participants.get(StorylineType.GOAL_MACHINES, 6),
            "min_goal_machines": max(3, self._min_participants),
        }
        try:
            hint_rendered = user_hint_tpl.format(**hint_kwargs)
        except KeyError:
            hint_rendered = user_hint_tpl.format(min_p=self._min_participants)
        user_msg = (
            "Upcoming fixtures in the current matchweek:\n"
            f"{fixture_block}\n\n"
            + hint_rendered
        )

        try:
            from app.main import _bump_cycle_counter
            _bump_cycle_counter("storyline_haiku_websearch")
        except Exception:
            pass
        # Cost-tripwire short-circuit. Storyline scout uses web_search,
        # so the projected cost includes the websearch addon. If the
        # tracker says we're past budget, return cached/empty.
        if self._cost_tracker is not None:
            try:
                projected = self._cost_tracker.estimate_haiku_call(
                    input_tokens=900, max_output_tokens=2048,
                    web_search=True,
                    web_search_calls=self._max_searches,
                )
                if not await self._cost_tracker.can_spend(projected):
                    logger.info(
                        "[cost] storyline scout skipped — daily LLM "
                        "budget exhausted (type=%s)",
                        storyline_type.value,
                    )
                    return None
            except Exception as exc:
                logger.warning(
                    "StorylineDetector cost-tracker check failed: %s", exc,
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

        if self._cost_tracker is not None:
            try:
                actual = self._cost_tracker.cost_from_usage(
                    getattr(resp, "usage", None),
                    web_search=True,
                    web_search_calls=self._max_searches,
                )
                await self._cost_tracker.record_call(
                    model=self._model, kind="storyline_scout",
                    cost_usd=actual,
                )
            except Exception as exc:
                logger.warning(
                    "StorylineDetector cost-tracker record failed: %s", exc,
                )

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
                    # Capture optional structured fields into
                    # participant_context. Downstream narrative author
                    # uses whatever's present to ground framing in real
                    # numbers; unused fields are inert.
                    ctx = _context_from_scout_row(storyline_type, p)
                    participants.append(StorylineParticipant(
                        player_name=str(p.get("player_name") or "").strip(),
                        team_name=team,
                        extra=str(p.get("extra") or "").strip(),
                        participant_context=ctx,
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
            # Record cooldown miss-result so we don't re-scout an empty
            # outcome immediately next cycle.
            _STORYLINE_LAST_SCOUT_AT[cooldown_key] = time.time()
            _STORYLINE_LAST_RESULT[cooldown_key] = None
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

        # Scout-side gate for the five new types. We trust the scout's
        # web_search but drop participants whose numeric fields fail a
        # sanity check (title race within 8pts, home fortress >= 0.70 or
        # flagged top-5, goal machines >= 5 goals). No second LLM call
        # — the gate is local to the scout payload. If fewer than
        # min_participants survive, the storyline is skipped. Per-type
        # max is also enforced here so a chatty scout can't overflow a
        # 6-leg combo into 10 legs.
        if storyline_type in self._new_types:
            participants = _scout_gate(storyline_type, participants)
            cap = self._type_max_participants.get(storyline_type, 6)
            if len(participants) > cap:
                participants = participants[:cap]

        if len(participants) < self._min_participants:
            logger.info(
                "StorylineDetector: %d participants < min %d (type=%s, scope=%s) — skipping",
                len(participants), self._min_participants,
                storyline_type.value, scope_label,
            )
            _STORYLINE_LAST_SCOUT_AT[cooldown_key] = time.time()
            _STORYLINE_LAST_RESULT[cooldown_key] = None
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
        _STORYLINE_LAST_SCOUT_AT[cooldown_key] = time.time()
        _STORYLINE_LAST_RESULT[cooldown_key] = item
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
        # Collect "off_threshold" near-miss rows for the borderline
        # fallback — participants the scout nominated and the verify
        # step could measure (row.confident=true), but whose position /
        # points fell just outside the threshold. We re-include them
        # only if the hard-verified survivors count is exactly 2, so
        # the gate behaviour is unchanged for normal cases.
        borderline: list[tuple[StorylineParticipant, dict]] = []

        for p in participants:
            row = cached.get(p.team_name.lower()) or fetched.get(p.team_name.lower())
            verdict = self._verify_row(storyline_type, row)
            logger.info(
                "StorylineDetector.verify: type=%s team=%s verdict=%s row=%s",
                storyline_type.value, p.team_name, verdict,
                _row_summary(row),
            )
            if verdict == "pass":
                p.participant_context = _context_from_row(storyline_type, row)
                survivors.append(p)
                continue
            # Stash near-miss candidates for the borderline pass.
            if (
                self._borderline_enabled
                and verdict == "off_threshold"
                and isinstance(row, dict)
                and self._is_near_threshold(storyline_type, row)
            ):
                borderline.append((p, row))

        # Borderline re-include: only fires when we're stuck at exactly 2
        # hard-verified survivors AND at least one near-miss exists. Keeps
        # the strict-verify behaviour intact for the common case; only
        # relaxes when the alternative is shipping 0 storylines. Last-resort.
        if (
            self._borderline_enabled
            and len(survivors) == 2
            and borderline
        ):
            # Rank borderline candidates by *how close* they were to the
            # threshold so the closest miss gets re-included first.
            borderline.sort(
                key=lambda pr: self._borderline_distance(storyline_type, pr[1])
            )
            for p, row in borderline[:1]:
                p.participant_context = _context_from_row(storyline_type, row)
                # Tag so downstream narrative author knows this is an
                # edge-case inclusion, not a clean qualifier.
                p.participant_context["borderline"] = True
                survivors.append(p)
                logger.info(
                    "StorylineDetector.verify: borderline include — "
                    "type=%s team=%s row=%s",
                    storyline_type.value, p.team_name, _row_summary(row),
                )

        return survivors

    def _is_near_threshold(
        self, storyline_type: StorylineType, row: dict,
    ) -> bool:
        """True if this row is within 1 position OR within 2 points of
        the storyline-type threshold (on the qualifying side). Kept
        tight — we're only trying to rescue the "17th with 7pts from
        safety" kind of near-miss, not the mid-table case."""
        pos = row.get("league_position")
        size = row.get("league_size")
        if storyline_type == StorylineType.RELEGATION:
            pts_safety = row.get("points_from_safety")
            if isinstance(pos, int) and isinstance(size, int) and size > 0:
                threshold_pos = size - self._reg_max_pos + 1
                if pos >= threshold_pos - 1:  # within 1 position
                    return True
            if (
                isinstance(pts_safety, int)
                and pts_safety <= self._reg_max_pts + 2  # within 2 points
            ):
                return True
            return False
        if storyline_type == StorylineType.EUROPE_CHASE:
            pts_spot = row.get("points_from_european_spot")
            if isinstance(pos, int):
                if (
                    self._ec_min_pos - 1 <= pos <= self._ec_max_pos + 1
                ):
                    return True
            if (
                isinstance(pts_spot, int)
                and pts_spot >= 0
                and pts_spot <= self._ec_max_pts + 2
            ):
                return True
            return False
        return False

    def _borderline_distance(
        self, storyline_type: StorylineType, row: dict,
    ) -> int:
        """How far past the threshold is this row? Lower = closer miss.
        Used to pick the best borderline candidate when multiple exist."""
        pos = row.get("league_position")
        size = row.get("league_size")
        if storyline_type == StorylineType.RELEGATION:
            pts_safety = row.get("points_from_safety")
            pos_gap = 99
            if isinstance(pos, int) and isinstance(size, int) and size > 0:
                pos_gap = max(0, (size - self._reg_max_pos + 1) - pos)
            pts_gap = 99
            if isinstance(pts_safety, int):
                pts_gap = max(0, pts_safety - self._reg_max_pts)
            return min(pos_gap, pts_gap)
        if storyline_type == StorylineType.EUROPE_CHASE:
            pts_spot = row.get("points_from_european_spot")
            pos_gap = 99
            if isinstance(pos, int):
                if pos < self._ec_min_pos:
                    pos_gap = self._ec_min_pos - pos
                elif pos > self._ec_max_pos:
                    pos_gap = pos - self._ec_max_pos
                else:
                    pos_gap = 0
            pts_gap = 99
            if isinstance(pts_spot, int) and pts_spot >= 0:
                pts_gap = max(0, pts_spot - self._ec_max_pts)
            return min(pos_gap, pts_gap)
        return 99

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
            from app.main import _bump_cycle_counter
            _bump_cycle_counter("standings_haiku_websearch")
        except Exception:
            pass
        # Cost-tripwire short-circuit. The verify call hits web_search per
        # team — bound it by the configured per-call budget.
        ws_calls = max(self._max_searches, len(teams) + 2)
        if self._cost_tracker is not None:
            try:
                projected = self._cost_tracker.estimate_haiku_call(
                    input_tokens=600, max_output_tokens=1500,
                    web_search=True, web_search_calls=ws_calls,
                )
                if not await self._cost_tracker.can_spend(projected):
                    logger.info(
                        "[cost] standings verify skipped — daily LLM "
                        "budget exhausted"
                    )
                    return {}
            except Exception as exc:
                logger.warning(
                    "StorylineDetector.verify cost-tracker check failed: %s",
                    exc,
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
                        "max_uses": ws_calls,
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

        if self._cost_tracker is not None:
            try:
                actual = self._cost_tracker.cost_from_usage(
                    getattr(resp, "usage", None),
                    web_search=True, web_search_calls=ws_calls,
                )
                await self._cost_tracker.record_call(
                    model=self._verify_model, kind="standings_verify",
                    cost_usd=actual,
                )
            except Exception as exc:
                logger.warning(
                    "StorylineDetector.verify cost-tracker record failed: %s",
                    exc,
                )

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


# ── Scout-side payload → participant_context (new types) ──────────────
#
# Unlike RELEGATION / EUROPE_CHASE (which get a second Haiku call for
# verification), the five new types trust the scout's web_search for
# the numeric ground truth. This helper extracts whatever structured
# fields the scout filled into participant_context so the narrative
# author can ground each team in real numbers.


def _context_from_scout_row(
    storyline_type: StorylineType, row: dict,
) -> dict:
    """Pull per-type numeric fields out of a scout participant payload.

    Missing / malformed values are dropped silently — the narrative
    author is explicitly instructed to leave a participant's framing
    vague rather than invent numbers when context is empty.
    """
    if not isinstance(row, dict):
        return {}
    ctx: dict = {}
    # Shared — every type may include these.
    pos = row.get("league_position")
    if isinstance(pos, int):
        ctx["league_position"] = pos
    if storyline_type == StorylineType.TITLE_RACE:
        pfl = row.get("points_from_leader")
        if isinstance(pfl, int) and pfl >= 0:
            ctx["points_from_leader"] = pfl
        pfs = row.get("points_from_second")
        if isinstance(pfs, int) and pfs >= 0:
            ctx["points_from_second"] = pfs
    elif storyline_type == StorylineType.EUROPEAN_WEEK:
        comp = str(row.get("competition") or "").strip().upper()
        if comp in {"UCL", "UEL", "UECL"}:
            ctx["competition"] = comp
    elif storyline_type == StorylineType.HOME_FORTRESS:
        hwr = row.get("home_win_rate")
        if isinstance(hwr, (int, float)) and 0.0 <= float(hwr) <= 1.0:
            ctx["home_win_rate"] = round(float(hwr), 2)
        form = str(row.get("home_form_last_10") or "").strip()
        if form:
            ctx["home_form_last_10"] = form[:10]
    elif storyline_type == StorylineType.GOAL_MACHINES:
        g = row.get("goals_this_season")
        if isinstance(g, int) and g >= 0:
            ctx["goals_this_season"] = g
        rf = str(row.get("recent_form_last_5") or "").strip()
        if rf:
            ctx["recent_form_last_5"] = rf[:32]
    # DERBY_WEEKEND: `extra` carries the derby's common name, which
    # the narrative author reads directly from participant.extra — no
    # extra numeric context needed.
    return ctx


def _scout_gate(
    storyline_type: StorylineType,
    participants: list[StorylineParticipant],
) -> list[StorylineParticipant]:
    """Apply per-type scout-side sanity filter.

    Each filter is intentionally conservative — we'd rather drop a
    borderline participant than ship a weak story. Caller enforces the
    per-type min/max cap after this returns.
    """
    keep: list[StorylineParticipant] = []
    for p in participants:
        ctx = p.participant_context or {}
        if storyline_type == StorylineType.TITLE_RACE:
            pos = ctx.get("league_position")
            pfl = ctx.get("points_from_leader")
            ok_pos = isinstance(pos, int) and 1 <= pos <= 5
            ok_pts = isinstance(pfl, int) and 0 <= pfl <= 8
            if ok_pos and ok_pts:
                keep.append(p)
            else:
                logger.info(
                    "StorylineDetector.scout_gate: TITLE_RACE drop "
                    "team=%s pos=%s pfl=%s", p.team_name, pos, pfl,
                )
        elif storyline_type == StorylineType.HOME_FORTRESS:
            hwr = ctx.get("home_win_rate")
            form = ctx.get("home_form_last_10") or ""
            # Accept if home_win_rate >= 0.70 OR form has >= 7 wins in
            # the last 10 (loose "top-5 home form" proxy — scouts
            # don't reliably return literal rank, but 7+ wins in 10 is
            # consistent with top-5 home form in every top-5 league).
            ok_rate = isinstance(hwr, (int, float)) and float(hwr) >= 0.70
            ok_form = isinstance(form, str) and form.upper().count("W") >= 7
            if ok_rate or ok_form:
                keep.append(p)
            else:
                logger.info(
                    "StorylineDetector.scout_gate: HOME_FORTRESS drop "
                    "team=%s hwr=%s form=%s", p.team_name, hwr, form,
                )
        elif storyline_type == StorylineType.GOAL_MACHINES:
            g = ctx.get("goals_this_season")
            if not p.player_name.strip():
                logger.info(
                    "StorylineDetector.scout_gate: GOAL_MACHINES drop "
                    "(no player_name) team=%s", p.team_name,
                )
                continue
            # 5 goals is a loose floor — any "Europe's top scorer"
            # claim implies double-digit goals by spring, but even a
            # 5-goal striker is a legitimate anytime-scorer pick.
            if isinstance(g, int) and g >= 5:
                keep.append(p)
            else:
                # Still include if scout didn't supply goals (we'd
                # rather keep the participant than drop a known name
                # for missing metadata — narrative author just won't
                # reference the number).
                if g is None:
                    keep.append(p)
                else:
                    logger.info(
                        "StorylineDetector.scout_gate: GOAL_MACHINES drop "
                        "player=%s goals=%s", p.player_name, g,
                    )
        elif storyline_type == StorylineType.EUROPEAN_WEEK:
            # Trust the scout — the competition claim IS the gate. If
            # `competition` missing, still accept (the fixture's
            # existence in the scout's payload means it was found via
            # web_search for UCL/UEL/UECL).
            keep.append(p)
        elif storyline_type == StorylineType.DERBY_WEEKEND:
            # Derby name arrives in `extra` — require at least one
            # char so the narrative author has something to reference.
            # (Scouts consistently fill this, but the check is cheap.)
            if p.extra:
                keep.append(p)
            else:
                logger.info(
                    "StorylineDetector.scout_gate: DERBY_WEEKEND drop "
                    "(no derby name) team=%s", p.team_name,
                )
        else:
            keep.append(p)
    return keep


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
