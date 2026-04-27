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
from typing import Any, Optional, Protocol

from app.engine.candidate_builder import CandidateBuilder
from app.engine.combo_builder import ComboBuilder
from app.engine.news_entity_resolver import NewsEntityResolver
from app.engine.news_scorer import NewsScorer, PolicyLayer
from app.models.news import CandidateCard, NewsItem
from app.models.schemas import Game
from app.services.candidate_store import CandidateStore

logger = logging.getLogger(__name__)


# ── HOT-tier classifier ────────────────────────────────────────────────
# Smart filter for the HOT tier (kickoff <6h). Replaces the previous
# "first N by kickoff" heuristic. Cheap chain (no LLM):
#
#   1. Kickoff window: now + min_minutes < kickoff < now + max_hours
#      (drops already-started fixtures and far-out ones).
#   2. Top-league allowlist: league name matches one of the
#      INTERNATIONAL_LEAGUE_PATTERNS in catalogue_loader. Soft —
#      controlled by PULSE_HOT_TOP_LEAGUE_ONLY kill switch.
#   3. BetBuilder-eligible: keep only fixtures with `IsBetBuilderEnabled=true`
#      on the catalogue row. Soft — controlled by
#      PULSE_HOT_REQUIRE_BB_ENABLED. Falls back to "pass" when the catalogue
#      doesn't carry the flag (e.g. mock data) so we don't starve the tier.
#   4. Cap at PULSE_TIER_HOT_MAX_FIXTURES, sorted by soonest kickoff first.
#
# Kept here (not a separate module) so the HOT tier loop can `from
# app.services.candidate_engine import classify_hot_fixtures` without a
# new file.

def _kickoff_to_epoch(raw: str) -> Optional[float]:
    """Parse catalogue_loader's kickoff string ("23 Apr 20:00 UTC") to unix ts.

    Mirrors `_parse_kickoff_to_epoch` in main.py — kept inline so the
    classifier doesn't import main.py (circular).
    """
    if not raw:
        return None
    try:
        from datetime import datetime, timedelta, timezone
        txt = raw.strip()
        if txt.endswith(" UTC"):
            txt = txt[:-4]
        now = datetime.now(timezone.utc)
        parsed = datetime.strptime(txt, "%d %b %H:%M").replace(
            year=now.year, tzinfo=timezone.utc,
        )
        if (now - parsed) > timedelta(days=180):
            parsed = parsed.replace(year=now.year + 1)
        return parsed.timestamp()
    except Exception:
        return None


def _is_top_league(league_name: str) -> bool:
    """Return True if the league matches the international top-league allowlist."""
    try:
        from app.services.catalogue_loader import _league_matches
        return _league_matches(league_name or "")
    except Exception:
        # Fallback hard-coded allowlist (matches catalogue_loader patterns).
        n = (league_name or "").lower()
        for p in (
            "premier league", "english premier",
            "laliga", "la liga",
            "bundesliga",
            "serie a",
            "ligue 1",
            "champions league", "europa league", "europa conference",
        ):
            if p in n:
                return True
        return False


def _bb_enabled(fixture: Any) -> bool:
    """Best-effort BetBuilder-enabled check on a fixture / catalogue row.

    The Pulse Game model doesn't currently carry IsBetBuilderEnabled; the
    flag, if surfaced, lives on a sibling dict-shaped catalogue row. Falls
    back to True (don't filter out) when the flag is absent — we don't
    want to starve the tier on legitimate data we just don't yet
    annotate.
    """
    if isinstance(fixture, dict):
        for key in ("IsBetBuilderEnabled", "is_bet_builder_enabled", "bb_enabled"):
            if key in fixture:
                return bool(fixture[key])
        return True
    for attr in ("IsBetBuilderEnabled", "is_bet_builder_enabled", "bb_enabled"):
        if hasattr(fixture, attr):
            return bool(getattr(fixture, attr))
    return True


def classify_hot_fixtures(
    fixtures: list,
    now_ts: float,
    *,
    min_kickoff_minutes: Optional[int] = None,
    max_kickoff_hours: Optional[int] = None,
    require_bb_enabled: Optional[bool] = None,
    top_league_only: Optional[bool] = None,
    max_fixtures: Optional[int] = None,
) -> list:
    """Filter a fixtures list down to HOT-tier-eligible candidates.

    Reads defaults from `app.config` env knobs at call time so a Railway
    env flip takes effect on the next cycle without redeploy. All knobs
    are kill-switchable.

    Returns: ordered list (soonest kickoff first), capped at
    `PULSE_TIER_HOT_MAX_FIXTURES`.
    """
    try:
        from app.config import (
            PULSE_HOT_MAX_KICKOFF_HOURS,
            PULSE_HOT_MIN_KICKOFF_MINUTES,
            PULSE_HOT_REQUIRE_BB_ENABLED,
            PULSE_HOT_TOP_LEAGUE_ONLY,
            PULSE_TIER_HOT_MAX_FIXTURES,
        )
    except Exception:
        PULSE_HOT_MIN_KICKOFF_MINUTES = 90
        PULSE_HOT_MAX_KICKOFF_HOURS = 6
        PULSE_HOT_REQUIRE_BB_ENABLED = True
        PULSE_HOT_TOP_LEAGUE_ONLY = True
        PULSE_TIER_HOT_MAX_FIXTURES = 5

    min_min = int(
        min_kickoff_minutes if min_kickoff_minutes is not None
        else PULSE_HOT_MIN_KICKOFF_MINUTES
    )
    max_hr = int(
        max_kickoff_hours if max_kickoff_hours is not None
        else PULSE_HOT_MAX_KICKOFF_HOURS
    )
    require_bb = bool(
        require_bb_enabled if require_bb_enabled is not None
        else PULSE_HOT_REQUIRE_BB_ENABLED
    )
    top_only = bool(
        top_league_only if top_league_only is not None
        else PULSE_HOT_TOP_LEAGUE_ONLY
    )
    cap = int(
        max_fixtures if max_fixtures is not None
        else PULSE_TIER_HOT_MAX_FIXTURES
    )

    total = len(fixtures)
    lower = float(now_ts) + (min_min * 60.0)
    upper = float(now_ts) + (max_hr * 3600.0)

    # 1. Kickoff window
    in_window: list[tuple[float, Any]] = []
    for f in fixtures:
        # Game has `start_time` (str). Dict catalogue rows may carry
        # `StartDate`/`kickoff_iso` instead — fail-soft.
        ko_raw = (
            getattr(f, "start_time", None)
            or (f.get("start_time") if isinstance(f, dict) else None)
            or (f.get("kickoff_iso") if isinstance(f, dict) else None)
            or ""
        )
        ko = _kickoff_to_epoch(str(ko_raw or ""))
        if ko is None:
            continue
        if ko <= lower or ko >= upper:
            continue
        in_window.append((ko, f))
    after_window = len(in_window)

    # 2. Top-league
    after_league = in_window
    if top_only:
        after_league = []
        for ko, f in in_window:
            league = (
                getattr(f, "broadcast", None)
                or (f.get("broadcast") if isinstance(f, dict) else None)
                or (f.get("league_name") if isinstance(f, dict) else None)
                or (f.get("LeagueName") if isinstance(f, dict) else None)
                or ""
            )
            if _is_top_league(str(league or "")):
                after_league.append((ko, f))

    # 3. BetBuilder-eligible
    after_bb = after_league
    if require_bb:
        after_bb = [(ko, f) for ko, f in after_league if _bb_enabled(f)]

    # 4. Sort + cap
    after_bb.sort(key=lambda kv: kv[0])
    eligible = [f for _, f in after_bb[:max(0, cap)]]

    logger.info(
        "[tier:HOT] classified — total=%d -> eligible=%d -> top=%d "
        "(kickoff_window=%dm..%dh, top_league_only=%s, bb_enabled=%s)",
        total, len(after_bb), len(eligible),
        min_min, max_hr, str(top_only).lower(), str(require_bb).lower(),
    )
    return eligible


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
        combo_builder: Optional[ComboBuilder] = None,
    ):
        self._ingester = ingester
        self._resolver = resolver
        self._builder = builder
        self._scorer = scorer
        self._policy = policy
        self._store = store
        self._combo_builder = combo_builder

    async def run_once(
        self,
        games: dict[str, Game],
        *,
        max_fixtures: int,
    ) -> dict[str, int]:
        """Run the full pipeline across the current catalogue. Returns counts.

        The policy layer runs ONCE at the end across all fixtures so it can
        see (and dedupe) scout-duplicated stories that landed on different
        fixtures with different news_item_ids.
        """
        counts = {"news": 0, "candidates": 0, "published": 0, "fixtures": 0}
        fixtures = list(games.values())[:max_fixtures]
        counts["fixtures"] = len(fixtures)

        all_scored: list[CandidateCard] = []
        all_items: list[NewsItem] = []

        for game in fixtures:
            items = await self._ingester.ingest_for_fixture(
                fixture_id=game.id,
                home=game.home_team.name,
                away=game.away_team.name,
                league=game.broadcast or "",
                kickoff_iso=game.start_time or "",
            )
            counts["news"] += len(items)
            all_items.extend(items)

            drafts: list[CandidateCard] = []
            for item in items:
                self._resolver.resolve(item)
                if not item.fixture_ids:
                    logger.debug(
                        "Unresolved mention: headline=%r mentions=%s",
                        item.headline[:60], item.mentions,
                    )
                    continue
                # Always build a single; it's the safety net if BB generation
                # fails or is rejected by the book.
                drafts.extend(self._builder.build(item))

                # Try a Bet Builder per news item's *primary* fixture.
                if self._combo_builder is not None:
                    for fixture_id in item.fixture_ids:
                        fixture = games.get(fixture_id)
                        if fixture is None:
                            continue
                        bb = await self._combo_builder.build(item, fixture)
                        if bb is not None:
                            drafts.append(bb)

            # Score this fixture's drafts now (needs in-memory news items).
            for c in drafts:
                news = next((it for it in items if it.id == c.news_item_id), None)
                if news is None:
                    news = await self._store.get_news_item(c.news_item_id) if c.news_item_id else None
                if news is None:
                    continue
                game_for_score = games.get(c.game_id)
                c.score, c.reason = self._scorer.score(
                    candidate=c, news=news, game=game_for_score,
                )
                all_scored.append(c)

        # Global policy pass — sees every candidate from every fixture. Lets
        # the headline-dedupe collapse a story that the scout returned for
        # multiple fixtures (e.g. a weekend injury round-up).
        headlines_by_id = {it.id: it.headline for it in all_items if it.id and it.headline}
        final = self._policy.apply(all_scored, headlines_by_id=headlines_by_id)
        counts["candidates"] = len(final)
        counts["published"] = sum(1 for c in final if c.threshold_passed)

        await self._store.save_candidates(final)

        logger.info(
            "CandidateEngine run: fixtures=%d news=%d candidates=%d published=%d",
            counts["fixtures"], counts["news"], counts["candidates"], counts["published"],
        )
        return counts
