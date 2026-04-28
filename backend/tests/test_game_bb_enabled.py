"""Tests for the `is_bet_builder_enabled` plumbing on the Game model.

Covers two surfaces:

1. `catalogue_loader._map_event_to_game` correctly extracts
   `Settings.IsBetBuilderEnabled` from a Rogue Event payload onto the
   `Game` model, defaulting to `False` when the field is missing.
2. `candidate_engine.classify_hot_fixtures` now actually filters on the
   model field — no fail-soft. With the kill switch on (default), only
   fixtures with `is_bet_builder_enabled=True` survive. With it off, the
   filter is bypassed.

No live LLM calls. No live Rogue HTTP. Pure pydantic + classifier logic.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_game_bb_enabled.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Synthetic Rogue event payloads ────────────────────────────────────

def _rogue_event(
    *,
    event_id: str = "evt-1",
    league: str = "Premier League",
    settings: dict | None = None,
    omit_settings: bool = False,
) -> dict:
    """Minimal Rogue Event-shaped dict that `_map_event_to_game` accepts.

    Mirrors the keys actually consumed by `_map_event_to_game` /
    `_participants` / `_start_time`. Anything we don't read is omitted.
    """
    base = {
        "_id": event_id,
        "LeagueName": league,
        "StartEventDate": "2026-04-27T20:00:00Z",
        "Participants": [
            {"_id": "home-1", "Name": "Home FC", "VenueRole": "Home"},
            {"_id": "away-1", "Name": "Away FC", "VenueRole": "Away"},
        ],
    }
    if not omit_settings:
        base["Settings"] = settings if settings is not None else {}
    return base


def test_catalogue_loader_extracts_bb_flag():
    """Settings.IsBetBuilderEnabled=True propagates to Game.is_bet_builder_enabled."""
    from app.services.catalogue_loader import _map_event_to_game

    event = _rogue_event(settings={"IsBetBuilderEnabled": True})
    game = _map_event_to_game(event)

    assert game is not None
    assert game.is_bet_builder_enabled is True


def test_catalogue_loader_extracts_bb_flag_false():
    """Settings.IsBetBuilderEnabled=False propagates as False (not dropped to default)."""
    from app.services.catalogue_loader import _map_event_to_game

    event = _rogue_event(settings={"IsBetBuilderEnabled": False})
    game = _map_event_to_game(event)

    assert game is not None
    assert game.is_bet_builder_enabled is False


def test_catalogue_loader_defaults_false_when_missing():
    """No Settings block at all → is_bet_builder_enabled=False (don't crash, don't assume True)."""
    from app.services.catalogue_loader import _map_event_to_game

    event = _rogue_event(omit_settings=True)
    game = _map_event_to_game(event)

    assert game is not None
    assert game.is_bet_builder_enabled is False


def test_catalogue_loader_defaults_false_when_settings_empty():
    """Settings present but missing IsBetBuilderEnabled → False (conservative)."""
    from app.services.catalogue_loader import _map_event_to_game

    event = _rogue_event(settings={})
    game = _map_event_to_game(event)

    assert game is not None
    assert game.is_bet_builder_enabled is False


# ── Classifier integration ────────────────────────────────────────────

def _make_game(*, fixture_id: str, kickoff_dt: datetime, bb_enabled: bool):
    from app.models.schemas import Game, GameStatus, Sport, Team

    home = Team(id=f"{fixture_id}-h", name="Home FC", short_name="HFC",
                color="#000000", sport=Sport.SOCCER)
    away = Team(id=f"{fixture_id}-a", name="Away FC", short_name="AFC",
                color="#ffffff", sport=Sport.SOCCER)
    return Game(
        id=fixture_id,
        sport=Sport.SOCCER,
        home_team=home,
        away_team=away,
        broadcast="Premier League",
        start_time=kickoff_dt.strftime("%d %b %H:%M UTC"),
        status=GameStatus.SCHEDULED,
        is_bet_builder_enabled=bb_enabled,
    )


def test_classify_hot_filters_when_bb_disabled():
    """Game with is_bet_builder_enabled=False is dropped when require_bb_enabled=True."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_ts = now_dt.timestamp()
    bb_off = _make_game(
        fixture_id="g-bb-off",
        kickoff_dt=now_dt + timedelta(hours=3),
        bb_enabled=False,
    )

    out = classify_hot_fixtures(
        [bb_off], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=True,
        top_league_only=False,
        max_fixtures=10,
    )

    assert out == []


def test_classify_hot_passes_when_kill_switch_off():
    """Same bb_enabled=False Game passes when require_bb_enabled=False."""
    from app.services.candidate_engine import classify_hot_fixtures

    now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_ts = now_dt.timestamp()
    bb_off = _make_game(
        fixture_id="g-bb-off",
        kickoff_dt=now_dt + timedelta(hours=3),
        bb_enabled=False,
    )

    out = classify_hot_fixtures(
        [bb_off], now_ts,
        min_kickoff_minutes=90,
        max_kickoff_hours=6,
        require_bb_enabled=False,
        top_league_only=False,
        max_fixtures=10,
    )

    assert [f.id for f in out] == ["g-bb-off"]
