"""Candidate engine orchestrator.

One-shot pipeline for a run of fixtures:

  for fixture in catalogue:
      news = ingester.ingest_for_fixture(fixture)      # LLM or mock
      news = [resolver.resolve(n) for n in news]
      drafts = [candidate_builder.build(n) for n in news]   # flattened
      scored = [scorer.score(c, news, game) for c in drafts]
      final  = policy.apply(scored)
      store.save_candidates(final)

Later (Stage 6+) live SSE triggers a partial run for the affected fixture
rather than the whole catalogue. The scheduling loop that calls `run_once`
lives in main.py / a background task.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from app.engine.candidate_builder import CandidateBuilder
from app.engine.news_entity_resolver import NewsEntityResolver
from app.engine.news_scorer import NewsScorer, PolicyLayer
from app.models.news import CandidateCard, NewsItem
from app.models.schemas import Game
from app.services.candidate_store import CandidateStore

logger = logging.getLogger(__name__)


class NewsIngesterLike(Protocol):
    async def ingest_for_fixture(
        self,
        *,
        fixture_id: str,
        home: str,
        away: str,
        league: str,
        kickoff_iso: str,
    ) -> list[NewsItem]: ...


class CandidateEngine:
    def __init__(
        self,
        *,
        ingester: NewsIngesterLike,
        resolver: NewsEntityResolver,
        builder: CandidateBuilder,
        scorer: NewsScorer,
        policy: PolicyLayer,
        store: CandidateStore,
    ):
        self._ingester = ingester
        self._resolver = resolver
        self._builder = builder
        self._scorer = scorer
        self._policy = policy
        self._store = store

    async def run_once(
        self,
        games: dict[str, Game],
        *,
        max_fixtures: int,
    ) -> dict[str, int]:
        """Run the full pipeline across the current catalogue. Returns counts."""
        counts = {"news": 0, "candidates": 0, "published": 0, "fixtures": 0}
        fixtures = list(games.values())[:max_fixtures]
        counts["fixtures"] = len(fixtures)

        for game in fixtures:
            items = await self._ingester.ingest_for_fixture(
                fixture_id=game.id,
                home=game.home_team.name,
                away=game.away_team.name,
                league=game.broadcast or "",
                kickoff_iso=game.start_time or "",
            )
            counts["news"] += len(items)

            drafts: list[CandidateCard] = []
            for item in items:
                self._resolver.resolve(item)
                if not item.fixture_ids:
                    logger.debug(
                        "Unresolved mention: headline=%r mentions=%s",
                        item.headline[:60], item.mentions,
                    )
                    continue
                drafts.extend(self._builder.build(item))

            # Score
            scored: list[CandidateCard] = []
            for c in drafts:
                news = await self._store.get_news_item(c.news_item_id) if c.news_item_id else None
                if news is None:
                    continue
                game_for_score = games.get(c.game_id)
                c.score, c.reason = self._scorer.score(
                    candidate=c, news=news, game=game_for_score,
                )
                scored.append(c)

            # Policy layer (dedupe + cap + threshold)
            final = self._policy.apply(scored)
            counts["candidates"] += len(final)
            counts["published"] += sum(1 for c in final if c.threshold_passed)

            await self._store.save_candidates(final)

        logger.info(
            "CandidateEngine run: fixtures=%d news=%d candidates=%d published=%d",
            counts["fixtures"], counts["news"], counts["candidates"], counts["published"],
        )
        return counts
