"""Tests for `classify_hot_fixtures` — smart HOT-tier filter.

What this file proves:

  1. Fixtures with kickoff <90 min away are dropped (already-too-late).
  2. Non-top-league fixtures are dropped when top_league_only=True.
  3. Fixtures with `IsBetBuilderEnabled=False` are dropped when
     require_bb_enabled=True.
  4. The `max_fixtures` cap is honoured.
  5. Returned list is sorted by soonest kickoff first.

No live LLM calls — fixtures are constructed from the Game pydantic
model with synthetic kickoff strings.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_hot_classifier.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _make_game(
    *,
    fixture_id: str,
    kickoff_dt: datetime,
    league: str = "Premier League",
    bb_enabled: Optional[bool] = None,
):
    """Build a minimal Game-shaped object the classifier can read.

    The classifier reads `start_time` (formatted "23 Apr 20:00 UTC")
    and `broadcast` (league name) from the fixture, plus an optional
    `IsBetBuilderEnabled` attribute.
    """
    from app.models.schemas import Game, GameStatus, Sport, Team

    home = Team(id=f"{fixture_id}-h", name="Home FC", short_name="HFC",
                color="#000000", sport=Sport.SOCCER)
    away = Team(id=f"{fixture_id}-a", name="Away FC", short_name="AFC",
                color="#ffffff", sport=Sport.SOCCER)
    g = Game(
        id=fixture_id,
        sport=Sport.SOCCER,
        home_team=home,
        away_team=away,
        broadcast=league,
        start_time=kickoff_dt.strftime("%d %b %H:%M UTC"),
        status=GameStatus.SCHEDULED,
    )
    if bb_enabled is not None:
        # Pydantic models are immutable by default; attach via __dict__
        # since the classifier reads via getattr fallback.
        object.__setattr__(g, "IsBetBuilderEnabled", bb_enabled)
    return g


def _now() -> tuple[float, datetime]:
    """Use a fixed-ish "now" matched to a recent UTC reference."""
    now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now_dt.timestamp(), now_dt


def test_drops_fixtures_inside_min_kickoff_window():
    """Fixtures kicking off in <90 min are filtered out."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    too_soon = _make_game(
        fixture_id="g-too-soon",
        kickoff_dt=now_dt + timedelta(minutes=30),
    )
    ok = _make_game(
        fixture_id="g-ok",
        kickoff_dt=now_dt + timedelta(hours=2),
    )
    out = classify_hot_fixtures(
        [too_soon, ok], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=False,
        top_league_only=False,
        max_fixtures=10,
    )
    ids = [f.id for f in out]
    assert "g-too-soon" not in ids
    assert "g-ok" in ids


def test_drops_non_top_league_fixtures():
    """Non-top-league fixtures filtered when top_league_only=True."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    top = _make_game(
        fixture_id="g-top",
        kickoff_dt=now_dt + timedelta(hours=3),
        league="Premier League",
    )
    junk = _make_game(
        fixture_id="g-junk",
        kickoff_dt=now_dt + timedelta(hours=3),
        league="Australian Reserves Cup",
    )
    out = classify_hot_fixtures(
        [top, junk], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=False,
        top_league_only=True,
        max_fixtures=10,
    )
    ids = [f.id for f in out]
    assert "g-top" in ids
    assert "g-junk" not in ids


def test_drops_bb_disabled_fixtures():
    """`IsBetBuilderEnabled=False` fixtures filtered when require_bb_enabled=True."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    bb_on = _make_game(
        fixture_id="g-bb-on",
        kickoff_dt=now_dt + timedelta(hours=3),
        bb_enabled=True,
    )
    bb_off = _make_game(
        fixture_id="g-bb-off",
        kickoff_dt=now_dt + timedelta(hours=3),
        bb_enabled=False,
    )
    out = classify_hot_fixtures(
        [bb_on, bb_off], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=True,
        top_league_only=False,
        max_fixtures=10,
    )
    ids = [f.id for f in out]
    assert "g-bb-on" in ids
    assert "g-bb-off" not in ids


def test_respects_max_fixtures_cap():
    """The classifier never returns more than `max_fixtures`."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    fixtures = [
        _make_game(
            fixture_id=f"g-{i}",
            kickoff_dt=now_dt + timedelta(hours=2, minutes=i * 5),
        )
        for i in range(10)
    ]
    out = classify_hot_fixtures(
        fixtures, now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=False,
        top_league_only=False,
        max_fixtures=3,
    )
    assert len(out) == 3


def test_sorts_by_soonest_kickoff_first():
    """Closer-to-kickoff (more news density) ranks ahead of far-out."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    far = _make_game(
        fixture_id="g-far",
        kickoff_dt=now_dt + timedelta(hours=5),
    )
    near = _make_game(
        fixture_id="g-near",
        kickoff_dt=now_dt + timedelta(hours=2),
    )
    middle = _make_game(
        fixture_id="g-middle",
        kickoff_dt=now_dt + timedelta(hours=3),
    )
    out = classify_hot_fixtures(
        [far, near, middle], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=False,
        top_league_only=False,
        max_fixtures=10,
    )
    assert [f.id for f in out] == ["g-near", "g-middle", "g-far"]


def test_full_filter_chain_funnel():
    """Realistic funnel: 10 fixtures → some dropped → top-N capped."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_ts, now_dt = _now()
    fixtures = [
        # too-soon — dropped at step 1
        _make_game(fixture_id="g-soon", kickoff_dt=now_dt + timedelta(minutes=30)),
        # too-far — dropped at step 1
        _make_game(fixture_id="g-far", kickoff_dt=now_dt + timedelta(hours=10)),
        # not top league — dropped at step 2
        _make_game(
            fixture_id="g-junk",
            kickoff_dt=now_dt + timedelta(hours=3),
            league="Croatian 2nd Div",
        ),
        # bb-disabled — dropped at step 3
        _make_game(
            fixture_id="g-no-bb",
            kickoff_dt=now_dt + timedelta(hours=3),
            bb_enabled=False,
        ),
        # eligible
        _make_game(fixture_id="g-eligible-1", kickoff_dt=now_dt + timedelta(hours=2)),
        _make_game(fixture_id="g-eligible-2", kickoff_dt=now_dt + timedelta(hours=3)),
        _make_game(fixture_id="g-eligible-3", kickoff_dt=now_dt + timedelta(hours=4)),
    ]
    out = classify_hot_fixtures(
        fixtures, now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=True,
        top_league_only=True,
        max_fixtures=2,
    )
    # only the 3 eligible survive, then capped to 2 (soonest first)
    assert [f.id for f in out] == ["g-eligible-1", "g-eligible-2"]
