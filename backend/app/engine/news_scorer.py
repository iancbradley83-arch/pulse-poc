"""News-first relevance scorer and policy layer.

Score = weighted sum of signals, each in [0, 1]:

  news_quality        0.30   — source credibility, recency, specificity
  market_coverage     0.20   — does a real market exist for this fixture?
  fixture_proximity   0.20   — kickoff within N days (nearer = higher)
  hook_weight         0.20   — injury > team_news > tactical > preview > other
  stats_support       0.10   — placeholder until Stage 4 enrichment

Policy layer:
  - per-fixture cap (default 3 per day, tunable)
  - per-(fixture, hook_type) dedupe — only the highest-scoring wins
  - publish threshold from config.PULSE_PUBLISH_THRESHOLD

Everything below the threshold stays in the store as status=DRAFT so the
admin table can show shadow candidates for tuning.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    HookType,
    NewsItem,
)
from app.models.schemas import Game

logger = logging.getLogger(__name__)


W_NEWS = 0.30
W_COVERAGE = 0.20
W_PROXIMITY = 0.20
W_HOOK = 0.20
W_STATS = 0.10

_CREDIBLE_SOURCES = {
    "bbc sport", "sky sports", "the athletic", "marca", "as",
    "gazzetta dello sport", "mundo deportivo",
    "fabrizio romano", "david ornstein", "adam schefter", "shams charania",
}

_HOOK_WEIGHT: dict[HookType, float] = {
    HookType.INJURY: 0.95,
    HookType.TRANSFER: 0.85,
    HookType.TEAM_NEWS: 0.80,
    HookType.TACTICAL: 0.70,
    HookType.MANAGER_QUOTE: 0.60,
    HookType.PREVIEW: 0.50,
    HookType.ARTICLE: 0.45,
    HookType.PRICE_MOVE: 0.75,
    HookType.LIVE_MOMENT: 0.85,
    HookType.OTHER: 0.35,
}


# Per-hook BB-vs-single preference. Values:
#   "bb"     — when both BB and single candidates exist for the same
#              news_item_id, keep the BB (multi-leg stack is the right
#              framing for rich news items with multiple angles).
#   "single" — keep the single (the story is a specific market thesis;
#              a 3-leg BB on this hook type feels forced).
#   "both"   — emit both; downstream ranker picks which to surface. Safe
#              default for hook types we haven't opinionated on yet.
#
# Tunable live via PULSE_HOOK_BET_TYPE_PREFERENCE_JSON env var — see
# `PolicyLayer.from_env_override`. Change the dict below to move the
# default baseline, or flip the env var for an A/B without redeploy.
HOOK_BET_TYPE_PREFERENCE: dict[HookType, str] = {
    # High-conviction hooks → emit BOTH a BB and a single so the ranker
    # has real supply to pull from (2026-04-24 volume-up PR). INJURY /
    # TEAM_NEWS typically produce a clean single-market angle (opponent
    # DNB / over-under shift) AND a rich BB stack, and both framings are
    # legitimate for the same story. Downstream feed_ranker mixes them.
    HookType.INJURY: "both",
    HookType.TEAM_NEWS: "both",
    # TRANSFER stays BB-only: transfer news is inherently multi-market
    # (player goals + team outcome + BTTS). A single here usually lacks
    # a specific thesis.
    HookType.TRANSFER: "bb",
    # 2026-04-24 mix-balance PR: upgrade TACTICAL / MANAGER_QUOTE /
    # PREVIEW from "single" to "both". User feedback post-volume-up was
    # "mix is single-heavy, want more BB / more combos". The hook themes
    # in combo_builder.py were simultaneously enriched to 3-4 legs for
    # these hooks so the BB side isn't just a two-leg throwaway — it
    # carries a real thesis (TACTICAL = corners+cards+overs+btts;
    # MANAGER_QUOTE = scorer + 1st-half + FT; PREVIEW = pick + overs +
    # BTTS). `both` means downstream keeps the single AND the BB; ranker
    # quotas decide which surface.
    HookType.TACTICAL: "both",
    HookType.MANAGER_QUOTE: "both",
    HookType.PREVIEW: "both",
    HookType.ARTICLE: "single",
    HookType.OTHER: "single",
    # Still deciding — emit both and let the ranker choose.
    HookType.PRICE_MOVE: "both",
    HookType.LIVE_MOMENT: "both",
}


class NewsScorer:
    def score(
        self,
        *,
        candidate: CandidateCard,
        news: NewsItem,
        game: Optional[Game],
    ) -> tuple[float, str]:
        news_quality = self._news_quality(news)
        market_coverage = 1.0 if candidate.market_ids else 0.0
        proximity = self._proximity(game)
        hook_weight = _HOOK_WEIGHT.get(news.hook_type, 0.35)
        stats_support = 0.5  # Stage 4 placeholder

        score = (
            W_NEWS * news_quality
            + W_COVERAGE * market_coverage
            + W_PROXIMITY * proximity
            + W_HOOK * hook_weight
            + W_STATS * stats_support
        )
        score = max(0.0, min(1.0, score))

        reason = (
            f"news={news_quality:.2f}, coverage={market_coverage:.2f}, "
            f"proximity={proximity:.2f}, hook={hook_weight:.2f}, "
            f"stats={stats_support:.2f} -> {score:.2f}"
        )
        return round(score, 3), reason

    def _news_quality(self, news: NewsItem) -> float:
        # Credibility
        src = (news.source_name or "").strip().lower()
        credibility = 0.9 if src in _CREDIBLE_SOURCES else (0.6 if src else 0.4)

        # Specificity — mentions of a player + team beats a vague preview
        mentions = len(news.mentions)
        specificity = min(1.0, 0.4 + 0.15 * mentions)

        # Recency — mock data has no published_at, default 0.8
        recency = 0.8
        if news.published_at:
            try:
                dt = datetime.fromisoformat(news.published_at.replace("Z", "+00:00"))
                age_h = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
                if age_h <= 12: recency = 1.0
                elif age_h <= 36: recency = 0.9
                elif age_h <= 72: recency = 0.7
                else: recency = 0.5
            except Exception:
                pass

        return round(0.5 * credibility + 0.3 * specificity + 0.2 * recency, 3)

    def _proximity(self, game: Optional[Game]) -> float:
        if game is None or not game.start_time:
            return 0.5
        # `start_time` is a display string like "21 Apr 19:00 UTC". Rather
        # than reparse, fall back to a neutral 0.7 when we can't compute.
        # Precise proximity scoring arrives once we carry start_dt through.
        return 0.7


class PolicyLayer:
    """Applies dedupe, per-fixture caps, and the publish threshold."""

    def __init__(
        self,
        *,
        publish_threshold: float,
        per_fixture_cap: int = 3,
        hook_bet_type_preference: Optional[dict[HookType, str]] = None,
    ):
        self._threshold = publish_threshold
        self._per_fixture_cap = per_fixture_cap
        # Copy so tests / env-overrides can't mutate the module default.
        self._hook_pref: dict[HookType, str] = dict(
            hook_bet_type_preference
            if hook_bet_type_preference is not None
            else HOOK_BET_TYPE_PREFERENCE
        )

    def _preferred_bet_type(self, hook: HookType) -> str:
        """Return 'bb' / 'single' / 'both' for this hook. Default 'both'."""
        return self._hook_pref.get(hook, "both")

    def apply(self, candidates: list[CandidateCard], *, headlines_by_id: Optional[dict[str, str]] = None) -> list[CandidateCard]:
        # 0. Per-news-item collapse driven by per-hook preference. BB, single,
        #    or both can survive depending on hook_type. Within a shape (two
        #    BBs or two singles for the same news id) higher score wins.
        def better_same_shape(a: CandidateCard, b: CandidateCard) -> CandidateCard:
            """Fallback resolver for content-dedupe when hook preference
            doesn't nominate a single winner."""
            a_bb = a.bet_type == BetType.BET_BUILDER
            b_bb = b.bet_type == BetType.BET_BUILDER
            if a_bb != b_bb:
                # If we've gotten here with a tie vote, fall back to the old
                # BB-wins heuristic so the existing behaviour is preserved.
                return a if a_bb else b
            return a if a.score >= b.score else b

        # Bucket by (news_item_id, bet_type_shape). "both" hooks keep one per
        # shape; "bb" / "single" hooks collapse via _pick_per_news.
        by_news_shape: dict[tuple[str, str], CandidateCard] = {}
        no_news: list[CandidateCard] = []
        for c in candidates:
            if not c.news_item_id:
                no_news.append(c)
                continue
            shape = "bb" if c.bet_type == BetType.BET_BUILDER else "single"
            key = (c.news_item_id, shape)
            prev = by_news_shape.get(key)
            if prev is None:
                by_news_shape[key] = c
            else:
                # Same shape, same news — higher score wins.
                by_news_shape[key] = prev if prev.score >= c.score else c

        # Now apply per-hook preference: for each news_item_id, decide which
        # shape(s) to emit.
        per_news: dict[str, list[CandidateCard]] = {}
        for (nid, _shape), c in by_news_shape.items():
            per_news.setdefault(nid, []).append(c)

        resolved: list[CandidateCard] = []
        for nid, cands in per_news.items():
            if len(cands) == 1:
                resolved.append(cands[0])
                continue
            # 2 candidates, different shapes (BB + single).
            a, b = cands[0], cands[1]
            pref = self._preferred_bet_type(a.hook_type)
            a_bb = a.bet_type == BetType.BET_BUILDER
            if pref == "bb":
                resolved.append(a if a_bb else b)
            elif pref == "single":
                resolved.append(b if a_bb else a)
            else:  # "both" — emit both shapes
                resolved.extend(cands)

        candidates = resolved + no_news

        # 0b. Content dedupe by normalized headline. The scout runs once per
        #     fixture and can independently surface the same real-world story
        #     for several fixtures (different news_item_ids, same headline).
        #     Collapse to one candidate per headline per shape (so a "both"
        #     hook's BB and single stay alongside each other).
        if headlines_by_id:
            def _norm_head(s: str) -> str:
                return " ".join((s or "").lower().split())

            by_head_shape: dict[tuple[str, str], CandidateCard] = {}
            untitled: list[CandidateCard] = []
            for c in candidates:
                head = headlines_by_id.get(c.news_item_id or "") if c.news_item_id else None
                key_h = _norm_head(head) if head else None
                if not key_h:
                    untitled.append(c)
                    continue
                shape = "bb" if c.bet_type == BetType.BET_BUILDER else "single"
                k = (key_h, shape)
                prev = by_head_shape.get(k)
                by_head_shape[k] = c if prev is None else better_same_shape(prev, c)
            candidates = list(by_head_shape.values()) + untitled

        # 1. Best-per-(fixture, hook_type, shape) dedupe. Keying on shape too
        #    so a "both" hook keeps its BB alongside its single — they're
        #    different products and the ranker should see them both.
        best: dict[tuple[str, HookType, str], CandidateCard] = {}
        for c in candidates:
            shape = "bb" if c.bet_type == BetType.BET_BUILDER else "single"
            key = (c.game_id, c.hook_type, shape)
            existing = best.get(key)
            if existing is None or c.score > existing.score:
                best[key] = c

        # 2. Per-fixture cap, keep highest-scoring
        by_fixture: dict[str, list[CandidateCard]] = {}
        for c in best.values():
            by_fixture.setdefault(c.game_id, []).append(c)

        out: list[CandidateCard] = []
        for fixture_id, group in by_fixture.items():
            group.sort(key=lambda c: c.score, reverse=True)
            kept = group[: self._per_fixture_cap]
            dropped = group[self._per_fixture_cap :]
            out.extend(kept)
            for c in dropped:
                c.status = CandidateStatus.REJECTED
                c.reason += " | dropped: per-fixture cap"
                out.append(c)

        # 3. Publish threshold — mark status
        for c in out:
            if c.status == CandidateStatus.REJECTED:
                c.threshold_passed = c.score >= self._threshold
                continue
            c.threshold_passed = c.score >= self._threshold
            if c.threshold_passed:
                c.status = CandidateStatus.PUBLISHED
            else:
                c.status = CandidateStatus.QUEUED

        return out
