"""News entity resolver.

Given a NewsItem with raw mention strings (team / player / coach names),
map it onto live Rogue entities — specifically fixtures and teams in the
current catalogue. The original `EntityResolver` is kept for the scripted
mock flow; this one operates on live Rogue `Game` objects.

Deliberately simple — substring / alias matching. Good enough for Stage 2.
Stage 4 / 5 can swap in an LLM-based NER step if accuracy matters.
"""
from __future__ import annotations

import re
from typing import Optional

from app.models.news import NewsItem
from app.models.schemas import Game


# Short-name and common-alias overrides for clubs whose official name
# differs from how news sources refer to them.
_TEAM_ALIASES: dict[str, list[str]] = {
    "manchester united": ["man utd", "man united", "united"],
    "manchester city": ["man city", "city"],
    "tottenham": ["spurs", "tottenham hotspur"],
    "arsenal": ["gunners"],
    "chelsea": ["blues"],
    "liverpool": ["reds"],
    "newcastle": ["magpies", "newcastle united"],
    "west ham": ["hammers", "west ham united"],
    "brighton": ["seagulls", "brighton and hove albion"],
    "leicester": ["foxes", "leicester city"],
    "wolves": ["wolverhampton", "wolverhampton wanderers"],
    "real madrid": ["madrid", "los blancos"],
    "barcelona": ["barca", "fc barcelona"],
    "atletico madrid": ["atleti", "atletico"],
    "athletic bilbao": ["athletic", "athletic club"],
    "real betis": ["betis"],
    "bayern munich": ["bayern", "fc bayern"],
    "borussia dortmund": ["bvb", "dortmund"],
    "paris saint-germain": ["psg", "paris sg"],
    "juventus": ["juve"],
    "inter milan": ["inter", "internazionale"],
    "ac milan": ["milan"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _alias_in(alias: str, haystack: str) -> bool:
    """True iff `alias` appears as a whole token within `haystack`.

    Stops "man" from matching "manchester", "uni" from matching "united", etc.
    Both inputs are already lowercase-normalised via _norm.
    """
    if not alias or not haystack:
        return False
    return re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", haystack) is not None


class NewsEntityResolver:
    """Alias map built from the live Rogue catalogue.

    Rebuild whenever the catalogue reloads (boot-time only for Stage 2).
    """

    def __init__(self, games: dict[str, Game]):
        self._games = games
        # alias-string -> list[game_id]. A team might play multiple fixtures
        # in the window, so resolve to a list.
        self._team_alias_to_games: dict[str, list[str]] = {}
        # team_name -> canonical team_id (stable Rogue-sourced IDs)
        self._team_name_to_id: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        for game in self._games.values():
            for side in (game.home_team, game.away_team):
                name_norm = _norm(side.name)
                short_norm = _norm(side.short_name)
                self._team_name_to_id[name_norm] = side.id

                keys = {name_norm, short_norm}
                for alias in _TEAM_ALIASES.get(name_norm, []):
                    keys.add(_norm(alias))

                for key in keys:
                    if key:
                        self._team_alias_to_games.setdefault(key, []).append(game.id)

    def resolve(self, item: NewsItem) -> NewsItem:
        """Pick the *single* fixture this news item is about (if any).

        Previous behaviour attached a story to every fixture that featured any
        mentioned team, which produced duplicate cards across unrelated matches
        (e.g. an Atletico Madrid injury story that name-dropped "Arsenal" as
        the UCL semi-final opponent ended up on the weekend Arsenal vs Chelsea
        card). The fix: score each fixture and keep only the top match.

        Scoring:
          2.0  both teams of a fixture appear in mentions (strongest signal — the
               story is clearly ABOUT that match)
          1.5  only one team appears in mentions AND that team plays in exactly
               one fixture in our current catalogue (unambiguous attach)
          1.0  only one team appears and it plays multiple fixtures (weak — use
               earliest-kickoff tiebreak and treat as a last-resort match)

        If nothing scores above 0, the news is dropped rather than spread. The
        candidate engine skips items with no fixture_ids downstream.
        """
        matched_teams: set[str] = set()

        for mention in item.mentions:
            m = _norm(mention)
            if not m:
                continue
            if m in self._team_name_to_id:
                matched_teams.add(self._team_name_to_id[m])
                continue
            # Substring fallback — e.g. "Bukayo Saka, Arsenal" mention matches
            # "arsenal" as a contained alias. Conservative: only whole-alias
            # hits, not partial character runs, by requiring word boundaries.
            for alias, tid in self._team_name_to_id.items():
                if alias and _alias_in(alias, m):
                    matched_teams.add(tid)

        item.team_ids = sorted(matched_teams)
        if not matched_teams:
            item.fixture_ids = []
            return item

        # How many games each team participates in (to detect unambiguous attachments).
        team_to_games: dict[str, list[str]] = {}
        for gid, game in self._games.items():
            for tid in (game.home_team.id, game.away_team.id):
                team_to_games.setdefault(tid, []).append(gid)

        scores: dict[str, float] = {}
        for gid, game in self._games.items():
            home_in = game.home_team.id in matched_teams
            away_in = game.away_team.id in matched_teams
            if home_in and away_in:
                scores[gid] = 2.0
            elif home_in or away_in:
                participating_team = game.home_team.id if home_in else game.away_team.id
                fixtures_for_team = team_to_games.get(participating_team, [])
                scores[gid] = 1.5 if len(fixtures_for_team) == 1 else 1.0

        if not scores:
            item.fixture_ids = []
            return item

        top_score = max(scores.values())
        # Ties broken by earliest kickoff (lexicographic on our "22 Apr 19:00 UTC"
        # strings is good enough inside a single rolling window).
        best = sorted(
            [gid for gid, s in scores.items() if s == top_score],
            key=lambda g: (self._games[g].start_time or ""),
        )[:1]
        item.fixture_ids = best
        return item

    def team_id_for(self, mention: str) -> Optional[str]:
        """Best-effort mapping of a single mention string to a team ID."""
        m = _norm(mention)
        if m in self._team_name_to_id:
            return self._team_name_to_id[m]
        for alias, tid in self._team_name_to_id.items():
            if alias and alias in m:
                return tid
        return None
