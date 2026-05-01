"""Tests for Phase 2a fixture-importance scorer.

Pure-function module + catalogue-loader integration. No live LLM calls,
no live Rogue HTTP. Phase 2a is observability only — these tests prove
the scorer math is correct and that the boot log fires the expected lines,
NOT that any engine routing changes (it doesn't).

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


# ── (1) compute_operator_signal ───────────────────────────────────────


def test_compute_operator_signal_featured_fixture_returns_one():
    """Featured fixture → 1.0 regardless of league_order."""
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=True, league_order=64_000_000)
    assert compute_operator_signal(g) == 1.0


def test_compute_operator_signal_uses_league_order_when_not_featured():
    """Non-featured high-prestige fixture → ~0.99 (league_order=1_000_001)."""
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=1_000_001)
    signal = compute_operator_signal(g)
    assert signal == pytest.approx(0.98999999, abs=1e-6)
    # And clearly above the deep threshold
    assert signal > 0.9


def test_compute_operator_signal_handles_missing_league_order():
    """No league_order, not featured → 0.0 (no crash)."""
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=None)
    assert compute_operator_signal(g) == 0.0


def test_compute_operator_signal_handles_extreme_league_order():
    """league_order > 100M → clamps to 0.0, never goes negative."""
    from app.engine.importance_scorer import compute_operator_signal

    g = _make_game(is_operator_featured=False, league_order=10_000_000_000)
    assert compute_operator_signal(g) == 0.0


def test_compute_operator_signal_max_of_signals():
    """Featured beats a low league_order signal."""
    from app.engine.importance_scorer import compute_operator_signal

    # league_order=64M would yield ~0.36; featured forces 1.0
    g = _make_game(is_operator_featured=True, league_order=64_000_000)
    assert compute_operator_signal(g) == 1.0


# ── (2) compute_importance_score ──────────────────────────────────────


def test_compute_importance_score_default_multipliers_pass_through():
    """With default factors=1.0, score == operator_signal."""
    from app.engine.importance_scorer import (
        compute_importance_score,
        compute_operator_signal,
    )

    g = _make_game(is_operator_featured=False, league_order=6_000_001)
    expected = compute_operator_signal(g)
    assert compute_importance_score(g) == pytest.approx(expected)


def test_compute_importance_score_applies_multipliers():
    """Score = operator_signal × calendar × intrinsic. Intrinsic > 1 allowed."""
    from app.engine.importance_scorer import compute_importance_score

    # Featured → operator_signal = 1.0; * 0.5 * 2.0 = 1.0
    g = _make_game(is_operator_featured=True)
    score = compute_importance_score(g, calendar_phase_factor=0.5, intrinsic_score=2.0)
    assert score == pytest.approx(1.0)

    # Non-featured league_order=20M → operator_signal=0.8; * 0.5 * 2.0 = 0.8
    g2 = _make_game(is_operator_featured=False, league_order=20_000_000)
    score2 = compute_importance_score(g2, calendar_phase_factor=0.5, intrinsic_score=2.0)
    assert score2 == pytest.approx(0.8)


# ── (3) classify_score ────────────────────────────────────────────────


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
    """Default thresholds: deep>=0.7, standard>=0.4, else minimal."""
    from app.engine.importance_scorer import classify_score

    assert classify_score(score) == expected


def test_classify_score_custom_thresholds():
    """Custom thresholds replace the defaults cleanly."""
    from app.engine.importance_scorer import classify_score

    # Move the deep threshold up to 0.9 — 0.85 now falls to standard.
    assert classify_score(0.85, deep_threshold=0.9, minimal_threshold=0.5) == "standard"
    assert classify_score(0.95, deep_threshold=0.9, minimal_threshold=0.5) == "deep"

    # Move minimal threshold down to 0.1 — 0.2 is now standard.
    assert classify_score(0.2, deep_threshold=0.7, minimal_threshold=0.1) == "standard"
    assert classify_score(0.05, deep_threshold=0.7, minimal_threshold=0.1) == "minimal"


# ── (4) catalogue_loader integration ──────────────────────────────────


def test_catalogue_loader_stamps_importance_score():
    """`_stamp_importance_scores` writes `compute_importance_score(game)`
    onto every Game.importance_score."""
    from app.engine.importance_scorer import compute_importance_score
    from app.services.catalogue_loader import _stamp_importance_scores

    games = [
        _make_game(fixture_id="a", is_operator_featured=True, league_order=1_000_001),
        _make_game(fixture_id="b", is_operator_featured=False, league_order=6_000_001),
        _make_game(fixture_id="c", is_operator_featured=False, league_order=64_000_000),
        _make_game(fixture_id="d", is_operator_featured=False, league_order=None),
    ]

    _stamp_importance_scores(games)

    for g in games:
        assert g.importance_score == pytest.approx(compute_importance_score(g))

    # Sanity: featured ranks high, no-signal ranks zero.
    by_id = {g.id: g for g in games}
    assert by_id["a"].importance_score == pytest.approx(1.0)
    assert by_id["d"].importance_score == pytest.approx(0.0)


def test_catalogue_loader_log_includes_distribution_and_top_5(caplog):
    """Both Phase 2a log lines fire, with the expected prefixes."""
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

    dist = distribution_lines[0]
    # 3 fixtures clear 0.7 (two featured + EPL=0.93); 1 minimal (CONCACAF=0.36)
    assert "deep=3" in dist
    assert "minimal=1" in dist

    top5 = top5_lines[0]
    # Top entry should be the featured UCL fixture
    assert "Atletico Madrid vs Arsenal" in top5
    # Lowest-prestige fixture is also displayed (only 4 fixtures, all fit in top-5)
    assert "Pumas vs Toluca" in top5


def test_log_importance_score_distribution_handles_empty_games(caplog):
    """No games = no log output, no crash."""
    from app.services.catalogue_loader import _log_importance_score_distribution

    caplog.set_level(logging.INFO, logger="app.services.catalogue_loader")
    _log_importance_score_distribution([])

    messages = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("[importance]") for m in messages)
