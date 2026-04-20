"""Mock news ingester — returns curated realistic news items without an LLM.

Same interface as `NewsIngester.ingest_for_fixture` so the downstream
pipeline (entity resolver, matcher, scorer, policy, admin table) is
identical regardless of whether news is LLM-sourced or mocked. Swap back
to the real ingester whenever an ANTHROPIC_API_KEY is available.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Any

from app.models.news import HookType, NewsItem
from app.services.candidate_store import CandidateStore

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"


class MockNewsIngester:
    """Drop-in replacement for NewsIngester when no API key is available."""

    def __init__(self, store: CandidateStore):
        self._store = store
        self._catalogue: list[dict[str, Any]] = []
        self._by_team: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        raw = json.loads((DATA_DIR / "mock_news.json").read_text())
        for row in raw:
            self._catalogue.append(row)
            for team in row.get("teams", []):
                self._by_team.setdefault(team.lower(), []).append(row)
        logger.info("MockNewsIngester: loaded %d stories across %d teams",
                    len(self._catalogue), len(self._by_team))

    async def ingest_for_fixture(
        self,
        *,
        fixture_id: str,
        home: str,
        away: str,
        league: str,
        kickoff_iso: str,
    ) -> list[NewsItem]:
        """Return 0–4 deterministic-ish news items for the teams in this fixture.

        Deterministic per (fixture_id, date) so repeat runs return the same
        stories — keeps the admin view stable while we iterate.
        """
        seed_key = f"{fixture_id}:{kickoff_iso[:10] if kickoff_iso else 'x'}"
        rng = random.Random(seed_key)

        pool: list[dict[str, Any]] = []
        for team in (home, away):
            candidates = self._by_team.get(team.lower(), [])
            pool.extend(candidates)

        # Also match loose — "Manchester United" in catalogue matches "Man United"
        if not pool:
            hn, an = home.lower(), away.lower()
            for story in self._catalogue:
                for t in story.get("teams", []):
                    tl = t.lower()
                    if tl and (tl in hn or hn in tl or tl in an or an in tl):
                        pool.append(story)

        # De-dupe while preserving order
        seen = set()
        deduped = []
        for story in pool:
            key = story.get("headline")
            if key and key not in seen:
                deduped.append(story)
                seen.add(key)

        if not deduped:
            return []

        # Pick up to 3 stories deterministically
        rng.shuffle(deduped)
        picked = deduped[:3]

        items: list[NewsItem] = []
        for story in picked:
            try:
                hook = HookType(story.get("hook_type", "other"))
            except ValueError:
                hook = HookType.OTHER
            items.append(NewsItem(
                source="mock",
                source_name=story.get("source_name", ""),
                source_url=story.get("source_url", ""),
                headline=story.get("headline", ""),
                summary=story.get("summary", ""),
                hook_type=hook,
                mentions=[str(m) for m in story.get("mentions", [])],
            ))

        await self._store.save_news_items(items)
        return items
