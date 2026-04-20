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
        """Populate team_ids and fixture_ids in-place-ish (returns the item)."""
        matched_games: set[str] = set()
        matched_teams: set[str] = set()

        for mention in item.mentions:
            m = _norm(mention)
            if not m:
                continue
            # Exact alias match first
            if m in self._team_alias_to_games:
                for gid in self._team_alias_to_games[m]:
                    matched_games.add(gid)
                if m in self._team_name_to_id:
                    matched_teams.add(self._team_name_to_id[m])
                continue
            # Substring fallback — "Bukayo Saka" mention matches Arsenal via
            # the player's (implicit) team; we don't have squad rosters yet,
            # so a substring like "Arsenal" inside a mention still hits.
            for alias, gids in self._team_alias_to_games.items():
                if alias and alias in m:
                    for gid in gids:
                        matched_games.add(gid)
                    if alias in self._team_name_to_id:
                        matched_teams.add(self._team_name_to_id[alias])

        item.fixture_ids = sorted(matched_games)
        item.team_ids = sorted(matched_teams)
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
