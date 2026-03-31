"""Entity Resolver — maps unstructured text mentions to entity IDs."""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


class EntityResolver:
    """For the POC, uses a static alias map. In production, this would call Claude Haiku for NER."""

    def __init__(self):
        self._player_aliases: dict[str, str] = {}
        self._team_aliases: dict[str, str] = {}
        self._build_aliases()

    def _build_aliases(self):
        players = json.loads((DATA_DIR / "mock_players.json").read_text())
        for p in players:
            pid = p["id"]
            name = p["name"]
            # Map full name, first name, last name, and common aliases
            self._player_aliases[name.lower()] = pid
            parts = name.split()
            if len(parts) >= 2:
                self._player_aliases[parts[-1].lower()] = pid  # last name
                self._player_aliases[parts[0].lower()] = pid   # first name

        # Manual aliases for the POC
        self._player_aliases.update({
            "the king": "lebron", "king james": "lebron", "bron": "lebron",
            "jt": "tatum", "pm15": "mahomes", "pat mahomes": "mahomes",
        })

        games = json.loads((DATA_DIR / "mock_games.json").read_text())
        for g in games:
            for side in ["home_team", "away_team"]:
                team = g[side]
                tid = team["id"]
                self._team_aliases[team["name"].lower()] = tid
                self._team_aliases[team["short_name"].lower()] = tid

        self._team_aliases.update({
            "gunners": "ars", "gooners": "ars",
            "blues": "che",  # context-dependent but fine for POC
            "the chiefs": "kc",
        })

    def resolve_player(self, text: str) -> list[str]:
        """Return player IDs mentioned in the text."""
        text_lower = text.lower()
        found = []
        for alias, pid in self._player_aliases.items():
            if alias in text_lower and pid not in found:
                found.append(pid)
        return found

    def resolve_team(self, text: str) -> list[str]:
        """Return team IDs mentioned in the text."""
        text_lower = text.lower()
        found = []
        for alias, tid in self._team_aliases.items():
            if alias in text_lower and tid not in found:
                found.append(tid)
        return found
