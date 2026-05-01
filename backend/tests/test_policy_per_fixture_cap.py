"""Tests for PolicyLayer per-fixture cap override (Phase 2b gradient routing).

The cap-by-id path lets the engine pass a different cap per fixture
based on importance score, while keeping the global default in place
for fixtures the gradient router didn't tag.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_policy_per_fixture_cap.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


_HOOK_CYCLE = ["injury", "team_news", "transfer", "manager_quote", "tactical",
               "preview", "article", "featured"]


def _candidate(*, fid: str, score: float, news_id: str, hook: str, bb=False):
    from app.models.news import BetType, CandidateCard, CandidateStatus, HookType

    return CandidateCard(
        news_item_id=news_id,
        hook_type=HookType(hook),
        bet_type=BetType.BET_BUILDER if bb else BetType.SINGLE,
        game_id=fid,
        market_ids=[f"m-{news_id}"],
        score=score,
        threshold_passed=False,
        reason="seed",
        status=CandidateStatus.DRAFT,
    )


def _spread_candidates(*, fid: str, n: int, score: float = 0.9):
    """Return n candidates for a fixture with distinct (hook, news_id) so the
    PolicyLayer's pre-cap dedupe doesn't collapse them before we hit the cap."""
    return [
        _candidate(
            fid=fid, score=score,
            news_id=f"{fid}-{i}",
            hook=_HOOK_CYCLE[i % len(_HOOK_CYCLE)],
        )
        for i in range(n)
    ]


def test_policy_global_cap_applies_when_no_override():
    """Without per_fixture_cap_by_id, the global cap rules every fixture."""
    from app.engine.news_scorer import PolicyLayer

    policy = PolicyLayer(publish_threshold=0.0, per_fixture_cap=2)
    out = policy.apply(_spread_candidates(fid="f1", n=5))
    kept = [c for c in out if c.status.value != "rejected"]
    assert len(kept) == 2


def test_policy_per_fixture_cap_overrides_global_for_specific_fixture():
    """Per-fixture cap pushes a high-score fixture above the global cap."""
    from app.engine.news_scorer import PolicyLayer

    policy = PolicyLayer(publish_threshold=0.0, per_fixture_cap=2)
    cands = _spread_candidates(fid="deep", n=5) + _spread_candidates(fid="shallow", n=5)
    out = policy.apply(cands, per_fixture_cap_by_id={"deep": 5, "shallow": 1})

    deep_kept = [c for c in out if c.game_id == "deep" and c.status.value != "rejected"]
    shallow_kept = [c for c in out if c.game_id == "shallow" and c.status.value != "rejected"]
    assert len(deep_kept) == 5
    assert len(shallow_kept) == 1


def test_policy_per_fixture_cap_falls_back_to_global_when_unmapped():
    """Fixture missing from the cap-by-id dict still uses the global cap."""
    from app.engine.news_scorer import PolicyLayer

    policy = PolicyLayer(publish_threshold=0.0, per_fixture_cap=2)
    cands = _spread_candidates(fid="a", n=5) + _spread_candidates(fid="b", n=5)
    out = policy.apply(cands, per_fixture_cap_by_id={"a": 4})

    a_kept = [c for c in out if c.game_id == "a" and c.status.value != "rejected"]
    b_kept = [c for c in out if c.game_id == "b" and c.status.value != "rejected"]
    assert len(a_kept) == 4
    assert len(b_kept) == 2  # falls back to global cap=2


def test_policy_per_fixture_cap_zero_drops_all():
    """Zero cap means no candidates kept for that fixture."""
    from app.engine.news_scorer import PolicyLayer

    policy = PolicyLayer(publish_threshold=0.0, per_fixture_cap=2)
    out = policy.apply(_spread_candidates(fid="x", n=3), per_fixture_cap_by_id={"x": 0})
    kept = [c for c in out if c.status.value != "rejected"]
    assert kept == []
