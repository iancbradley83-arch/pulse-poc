"""Tests for fixture-importance scorer + Phase 2b gradient routing.

Pure-function module + catalogue-loader integration. No live LLM calls,
no live Rogue HTTP. Phase 2a tests cover the legacy fixed-divisor
helpers (still exported for back-compat); Phase 2b tests cover
rank-based normalization, `gradient_factor`, and the catalogue stamp.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_importance_scorer.py -v
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────


def _make_game(
    *,
    fixture_id: str = "evt-1",
    league_order: Optional[int] = None,
    is_operator_featured: bool = False,
    is_top_league: bool = False,
    is_early_payout: bool = False,
    league_name: str = "Premier League",
    home: str = "Home FC",
    away: str = "Away FC",
):
    from app.models.schemas import Game, GameStatus, Sport, Team

    home_team = Team(id=f"{fixture_id}-h", name=home, short_name="HFC",
                     color="#000", sport=Sport.SOCCER)
    away_team = Team(id=f"{fixture_id}-a", name=away, short_name="AFC",
                     color="#fff", sport=Sport.SOCCER)
    return Game(
        id=fixture_id,
        sport=Sport.SOCCER,
        home_team=home_team,
        away_team=away_team,
        broadcast=league_name,
        start_time="29 Apr 20:00 UTC",
        status=GameStatus.SCHEDULED,
        league_order=league_order,
        is_operator_featured=is_operator_featured,
        is_top_league=is_top_league,
        is_early_payout=is_early_payout,
    )


# ── (1) Phase 2a back-compat: compute_operator_signal ─────────────────


def test_compute_operator_signal_featured_fixture_returns_one():
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=True, league_order=64_000_000)
    assert compute_operator_signal(g) == 1.0


def test_compute_operator_signal_uses_league_order_when_not_featured():
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=1_000_001)
    signal = compute_operator_signal(g)
    assert signal == pytest.approx(0.98999999, abs=1e-6)


def test_compute_operator_signal_handles_missing_league_order():
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=None)
    assert compute_operator_signal(g) == 0.0


def test_compute_operator_signal_handles_extreme_league_order():
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=10_000_000_000)
    assert compute_operator_signal(g) == 0.0


def test_compute_importance_score_default_multipliers_pass_through():
    from app.engine.importance_scorer import (
        compute_importance_score,
        compute_operator_signal,
    )

    g = _make_game(is_operator_featured=False, league_order=6_000_001)
    expected = compute_operator_signal(g)
    assert compute_importance_score(g) == pytest.approx(expected)


def test_compute_importance_score_applies_multipliers():
    from app.engine.importance_scorer import compute_importance_score

    g = _make_game(is_operator_featured=True)
    score = compute_importance_score(g, calendar_phase_factor=0.5, intrinsic_score=2.0)
    assert score == pytest.approx(1.0)

    g2 = _make_game(is_operator_featured=False, league_order=20_000_000)
    score2 = compute_importance_score(g2, calendar_phase_factor=0.5, intrinsic_score=2.0)
    assert score2 == pytest.approx(0.8)


# ── (2) Phase 2a classify_score (kept for log-line back-compat) ───────


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, "minimal"),
        (0.39, "minimal"),
        (0.4, "standard"),
        (0.5, "standard"),
        (0.69, "standard"),
        (0.7, "deep"),
        (0.99, "deep"),
        (1.0, "deep"),
    ],
)
def test_classify_score_thresholds(score: float, expected: str):
    from app.engine.importance_scorer import classify_score

    assert classify_score(score) == expected


# ── (3) Phase 2b: rank-based normalization ────────────────────────────


def test_league_rank_signal_orders_by_league_order_ascending():
    """Lower league_order → higher rank signal. Ties broken by id."""
    from app.engine.importance_scorer import league_rank_signal

    games = [
        _make_game(fixture_id="ucl", league_order=1_000_001),
        _make_game(fixture_id="lib", league_order=6_000_001),
        _make_game(fixture_id="epl", league_order=7_000_002),
        _make_game(fixture_id="cnc", league_order=64_000_000),
    ]
    rank = league_rank_signal(games)
    assert rank["ucl"] == pytest.approx(1.0)
    assert rank["lib"] == pytest.approx(2 / 3)
    assert rank["epl"] == pytest.approx(1 / 3)
    assert rank["cnc"] == pytest.approx(0.0)


def test_league_rank_signal_handles_none_league_order():
    """Games without league_order get 0.0 and don't affect others' ranks."""
    from app.engine.importance_scorer import league_rank_signal

    games = [
        _make_game(fixture_id="a", league_order=1_000_000),
        _make_game(fixture_id="b", league_order=2_000_000),
        _make_game(fixture_id="c", league_order=None),
    ]
    rank = league_rank_signal(games)
    assert rank["a"] == pytest.approx(1.0)
    assert rank["b"] == pytest.approx(0.0)
    assert rank["c"] == pytest.approx(0.0)


def test_league_rank_signal_single_game_returns_one():
    from app.engine.importance_scorer import league_rank_signal

    games = [_make_game(fixture_id="solo", league_order=5_000_000)]
    rank = league_rank_signal(games)
    assert rank["solo"] == pytest.approx(1.0)


def test_league_rank_signal_empty_list():
    from app.engine.importance_scorer import league_rank_signal

    assert league_rank_signal([]) == {}


def test_league_rank_signal_robust_to_per_operator_ranges():
    """Same rank shape regardless of underlying numeric range."""
    from app.engine.importance_scorer import league_rank_signal

    apuesta = [
        _make_game(fixture_id="x", league_order=1_000_001),
        _make_game(fixture_id="y", league_order=64_000_000),
    ]
    other = [
        _make_game(fixture_id="x", league_order=10),
        _make_game(fixture_id="y", league_order=999),
    ]
    a = league_rank_signal(apuesta)
    b = league_rank_signal(other)
    assert a == b == {"x": 1.0, "y": 0.0}


def test_assign_rank_importance_scores_featured_fixture_floats_to_one():
    from app.engine.importance_scorer import assign_rank_importance_scores

    games = [
        _make_game(fixture_id="featured-but-low", is_operator_featured=True,
                   league_order=64_000_000),
        _make_game(fixture_id="prestige", is_operator_featured=False,
                   league_order=1_000_001),
    ]
    assign_rank_importance_scores(games)
    by_id = {g.id: g for g in games}
    assert by_id["featured-but-low"].importance_score == pytest.approx(1.0)
    assert by_id["prestige"].importance_score == pytest.approx(1.0)


def test_assign_rank_importance_scores_apuesta_total_distribution():
    """Rebuild the Apuesta Total live-boot scenario from the design doc."""
    from app.engine.importance_scorer import assign_rank_importance_scores

    games = [
        _make_game(fixture_id="ucl-am-ars", is_operator_featured=True,
                   league_order=1_000_001),
        _make_game(fixture_id="lib-1", is_operator_featured=True,
                   league_order=6_000_001),
        _make_game(fixture_id="sud-1", is_operator_featured=True,
                   league_order=6_000_002),
        _make_game(fixture_id="epl-1", is_operator_featured=False,
                   league_order=7_000_002),
        _make_game(fixture_id="cnc-1", is_operator_featured=False,
                   league_order=64_001_601),
        _make_game(fixture_id="other-1", is_operator_featured=False,
                   league_order=12_000_001),
    ]
    assign_rank_importance_scores(games)
    by_id = {g.id: g.importance_score for g in games}

    # Featured fixtures all 1.0
    assert by_id["ucl-am-ars"] == pytest.approx(1.0)
    assert by_id["lib-1"] == pytest.approx(1.0)
    assert by_id["sud-1"] == pytest.approx(1.0)

    # Non-featured: EPL ranks above OTHER which ranks above CONCACAF.
    assert by_id["epl-1"] > by_id["other-1"] > by_id["cnc-1"]
    assert by_id["cnc-1"] == pytest.approx(0.0)


def test_assign_rank_importance_scores_calendar_phase_passthrough():
    """Phase 4 multipliers compose without changing call sites."""
    from app.engine.importance_scorer import assign_rank_importance_scores

    games = [
        _make_game(fixture_id="a", is_operator_featured=True, league_order=1_000_001),
        _make_game(fixture_id="b", is_operator_featured=False, league_order=64_000_000),
    ]
    assign_rank_importance_scores(
        games, calendar_phase_factor=0.5, intrinsic_score=1.5,
    )
    by_id = {g.id: g.importance_score for g in games}
    # a: 1.0 * 0.5 * 1.5 = 0.75
    assert by_id["a"] == pytest.approx(0.75)
    # b: 0.0 * 0.5 * 1.5 = 0.0
    assert by_id["b"] == pytest.approx(0.0)


# ── (4) gradient_factor ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "score,lo,hi,expected",
    [
        (0.0, 2, 5, 2.0),
        (1.0, 2, 5, 5.0),
        (0.5, 2, 5, 3.5),
        (0.25, 0.05, 0.40, 0.05 + 0.25 * 0.35),
        (-0.5, 2, 5, 2.0),  # clamped to 0
        (1.5, 2, 5, 5.0),   # clamped to 1
    ],
)
def test_gradient_factor(score, lo, hi, expected):
    from app.engine.importance_scorer import gradient_factor

    assert gradient_factor(score, lo, hi) == pytest.approx(expected)


def test_gradient_factor_inverted_range_works():
    """`hi < lo` is allowed — the 'ceiling' is just the value at score=1."""
    from app.engine.importance_scorer import gradient_factor

    assert gradient_factor(1.0, 5, 2) == pytest.approx(2.0)
    assert gradient_factor(0.0, 5, 2) == pytest.approx(5.0)


# ── (5) catalogue_loader integration (Phase 2b rank-based) ────────────


def test_catalogue_loader_stamps_rank_based_score():
    """`_stamp_importance_scores` writes rank-based scores onto every Game."""
    from app.services.catalogue_loader import _stamp_importance_scores

    games = [
        _make_game(fixture_id="a", is_operator_featured=True, league_order=1_000_001),
        _make_game(fixture_id="b", is_operator_featured=False, league_order=6_000_001),
        _make_game(fixture_id="c", is_operator_featured=False, league_order=64_000_000),
        _make_game(fixture_id="d", is_operator_featured=False, league_order=None),
    ]
    _stamp_importance_scores(games)

    by_id = {g.id: g for g in games}
    # Featured floats to 1.0; rank=0 (lowest league_order) also 1.0
    assert by_id["a"].importance_score == pytest.approx(1.0)
    # Rank-1 of 3 rankable games: 1 - 1/2 = 0.5
    assert by_id["b"].importance_score == pytest.approx(0.5)
    # Rank-2 of 3: 1 - 2/2 = 0.0
    assert by_id["c"].importance_score == pytest.approx(0.0)
    # No league_order, not featured → 0.0
    assert by_id["d"].importance_score == pytest.approx(0.0)


def test_catalogue_loader_log_includes_distribution_and_top_5(caplog):
    """Both observability log lines fire under the new rank-based scoring."""
    from app.services.catalogue_loader import (
        _log_importance_score_distribution,
        _stamp_importance_scores,
    )

    games = [
        _make_game(fixture_id="ucl", league_name="Champions League",
                   home="Atletico Madrid", away="Arsenal",
                   is_operator_featured=True, league_order=1_000_001),
        _make_game(fixture_id="lib", league_name="Copa Libertadores",
                   home="Boca Juniors", away="River Plate",
                   is_operator_featured=True, league_order=6_000_001),
        _make_game(fixture_id="epl", league_name="Premier League",
                   home="Brighton", away="Brentford",
                   is_operator_featured=False, league_order=7_000_002),
        _make_game(fixture_id="cnc", league_name="CONCACAF Champions Cup",
                   home="Pumas", away="Toluca",
                   is_operator_featured=False, league_order=64_001_601),
    ]
    _stamp_importance_scores(games)

    caplog.set_level(logging.INFO, logger="app.services.catalogue_loader")
    _log_importance_score_distribution(games)

    messages = [r.getMessage() for r in caplog.records]
    distribution_lines = [m for m in messages if m.startswith("[importance] score distribution:")]
    top5_lines = [m for m in messages if m.startswith("[importance] top 5 fixtures by score:")]

    assert len(distribution_lines) == 1, f"expected one distribution line, got: {messages}"
    assert len(top5_lines) == 1, f"expected one top-5 line, got: {messages}"

    top5 = top5_lines[0]
    assert "Atletico Madrid vs Arsenal" in top5
    assert "Pumas vs Toluca" in top5


def test_log_importance_score_distribution_handles_empty_games(caplog):
    """No games = no log output, no crash."""
    from app.services.catalogue_loader import _log_importance_score_distribution

    caplog.set_level(logging.INFO, logger="app.services.catalogue_loader")
    _log_importance_score_distribution([])

    messages = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("[importance]") for m in messages)


# ── (6) GradientRoutingConfig.for_score ───────────────────────────────


def test_gradient_routing_disabled_returns_ceiling_for_every_score():
    from app.engine.importance_scorer import GradientRoutingConfig

    cfg = GradientRoutingConfig(
        enabled=False,
        max_searches_floor=2, max_searches_ceil=5,
        per_fixture_cap_floor=2, per_fixture_cap_ceil=5,
        cost_cap_usd_floor=0.05, cost_cap_usd_ceil=0.40,
    )
    for score in (0.0, 0.5, 1.0, None):
        knobs = cfg.for_score(score)
        assert knobs["max_searches"] == 5
        assert knobs["per_fixture_cap"] == 5
        assert knobs["max_cost_usd"] == pytest.approx(0.40)


def test_gradient_routing_enabled_interpolates_per_fixture():
    from app.engine.importance_scorer import GradientRoutingConfig

    cfg = GradientRoutingConfig(
        enabled=True,
        max_searches_floor=2, max_searches_ceil=5,
        per_fixture_cap_floor=2, per_fixture_cap_ceil=5,
        cost_cap_usd_floor=0.05, cost_cap_usd_ceil=0.40,
    )
    floor = cfg.for_score(0.0)
    assert floor["max_searches"] == 2
    assert floor["per_fixture_cap"] == 2
    assert floor["max_cost_usd"] == pytest.approx(0.05)

    ceiling = cfg.for_score(1.0)
    assert ceiling["max_searches"] == 5
    assert ceiling["per_fixture_cap"] == 5
    assert ceiling["max_cost_usd"] == pytest.approx(0.40)

    mid = cfg.for_score(0.5)
    assert mid["max_searches"] == 4  # round(2 + 0.5 * 3) = round(3.5) = 4
    assert mid["per_fixture_cap"] == 4
    assert mid["max_cost_usd"] == pytest.approx(0.225)


def test_gradient_routing_no_score_falls_open_to_ceiling():
    """Game without importance_score (e.g. mid-deploy) gets ceiling, not floor."""
    from app.engine.importance_scorer import GradientRoutingConfig

    cfg = GradientRoutingConfig(
        enabled=True,
        max_searches_floor=2, max_searches_ceil=5,
        per_fixture_cap_floor=2, per_fixture_cap_ceil=5,
        cost_cap_usd_floor=0.05, cost_cap_usd_ceil=0.40,
    )
    knobs = cfg.for_score(None)
    assert knobs["max_searches"] == 5
    assert knobs["per_fixture_cap"] == 5
    assert knobs["max_cost_usd"] == pytest.approx(0.40)


def test_gradient_routing_disabled_cost_cap_passes_none():
    """Operators can opt out of the per-fixture cost cap (None floor/ceil)."""
    from app.engine.importance_scorer import GradientRoutingConfig

    cfg = GradientRoutingConfig(
        enabled=True,
        max_searches_floor=2, max_searches_ceil=5,
        per_fixture_cap_floor=2, per_fixture_cap_ceil=5,
        cost_cap_usd_floor=None, cost_cap_usd_ceil=None,
    )
    for score in (0.0, 0.5, 1.0):
        knobs = cfg.for_score(score)
        assert knobs["max_cost_usd"] is None
