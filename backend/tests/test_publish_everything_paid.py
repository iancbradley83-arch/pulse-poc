"""Tests for the "publish everything we paid LLM cost for" kill switch.

Item 1 in `docs/follow-ups-from-ops-session-2026-04-28.md`. Today's
engine generated 13 unique cards (LLM cost paid for every one) but
only 11 made it into `/api/feed`. The leak was the feed-ranker
`_dedupe_by_fixture_market` filter — when a fixture has both a single
and a BB whose first leg shares a `market_type`, the lower-scored card
was silently dropped at every `/api/feed` render.

These tests are parametrized over `PULSE_PRUNE_PAID_CARDS`:

  * Default (unset / "false"): every card we paid for is visible — the
    same-fixture+same-market dedupe is a no-op. With N pre-built
    candidates that previously would have been deduped, all N appear
    in `/api/feed`.
  * Kill switch "true": prior pruning is restored, the lower-scored
    duplicate is dropped.

We construct cards directly (no LLM, no Rogue), seed them into a
FeedManager, mount the API route, and hit `/api/feed` with
`fastapi.testclient.TestClient` — exactly the user-facing wire path.

Run:
    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_publish_everything_paid.py -v
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Make backend/app importable when invoked as `pytest tests/...`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.models.schemas import (
    Card,
    CardType,
    Game,
    GameStatus,
    Market,
    MarketSelection,
    Sport,
    Team,
)


# ── Fixtures ────────────────────────────────────────────────────────────
def _team(tid: str, name: str) -> Team:
    return Team(
        id=tid, name=name, short_name=name[:3].upper(),
        color="#000", sport=Sport.SOCCER,
    )


def _game(gid: str = "g1", *, hours_ahead: int = 24) -> Game:
    """Build a Game whose start_time string is in the future so the
    no-show filter doesn't drop it. The ranker parses
    "DD MMM HH:MM UTC"; we render now+N hours in that format."""
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    start = when.strftime("%d %b %H:%M UTC")
    return Game(
        id=gid,
        sport=Sport.SOCCER,
        home_team=_team("h", "Home FC"),
        away_team=_team("a", "Away FC"),
        status=GameStatus.SCHEDULED,
        broadcast="EPL",
        start_time=start,
    )


def _market(*, market_type: str = "match_result", mid: str = "m1") -> Market:
    return Market(
        id=mid,
        game_id="g1",
        market_type=market_type,
        label="Match Result",
        selections=[
            MarketSelection(label="Home", odds="2.00", selection_id="sel-h"),
        ],
    )


def _card(
    *,
    card_id: str,
    headline: str,
    relevance: float,
    market_type: str = "match_result",
    bet_type: str = "single",
    hook_type: str = "injury",
    ago_minutes: int = 30,
    fixture_id: str = "g1",
) -> Card:
    return Card(
        id=card_id,
        card_type=CardType.PRE_MATCH,
        game=_game(gid=fixture_id),
        narrative_hook="hook",
        headline=headline,
        market=_market(market_type=market_type, mid=f"m-{card_id}"),
        relevance_score=relevance,
        bet_type=bet_type,
        hook_type=hook_type,
        ago_minutes=ago_minutes,
    )


def _build_app(feed) -> FastAPI:
    """Mount only the feed routes — minimal app for the wire test.

    `/api/feed` itself only reads from FeedManager, so the catalog +
    simulator passed to `create_routes` are dummy stand-ins here. Other
    routes (`/api/games`, `/api/games/{id}/markets`, `/api/simulator/*`)
    aren't exercised by these tests.

    `app.api.routes` defines `router` at module scope, so successive
    `create_routes(...)` calls register additional copies of the same
    paths against the same router. Tests reload the module to get a
    fresh router (and a closure over THIS test's `feed`).
    """
    # Force a fresh router instance closing over this test's feed.
    if "app.api.routes" in sys.modules:
        importlib.reload(sys.modules["app.api.routes"])
    from app.api.routes import create_routes
    from app.services.market_catalog import MarketCatalog

    class _FakeSim:
        is_running = False
        _games: dict = {}

        async def start(self):
            return None

        async def stop(self):
            return None

    app = FastAPI()
    catalog = MarketCatalog()
    app.include_router(create_routes(catalog, feed, _FakeSim()))
    return app


def _reload_config_modules():
    """Re-import config + feed_ranker so the env-var-derived flag picks
    up the monkeypatched value. Without this, the module-level
    `PULSE_PRUNE_PAID_CARDS = os.getenv(...)` snapshot taken at first
    import freezes the test's env mutation."""
    for name in ("app.config", "app.engine.feed_ranker"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])


# ── Tests ───────────────────────────────────────────────────────────────
def test_default_kill_switch_off_keeps_every_paid_card(monkeypatch):
    """Default behaviour: PULSE_PRUNE_PAID_CARDS unset → no pruning.

    Build 4 candidates; under prior pruning, 2 would have been dropped
    by `_dedupe_by_fixture_market` (same fixture, same market_type).
    With the kill switch off (default), all 4 must appear in /api/feed.
    """
    monkeypatch.delenv("PULSE_PRUNE_PAID_CARDS", raising=False)
    _reload_config_modules()

    from app.services.feed_manager import FeedManager
    feed = FeedManager()

    # 2 fixtures, each with a single AND a BB whose primary market_type
    # collides with the single. Total 4 cards; old behaviour drops 2.
    cards = [
        _card(
            card_id="g1-single", headline="g1 single high", relevance=0.85,
            market_type="match_result", bet_type="single",
            fixture_id="g1",
        ),
        _card(
            card_id="g1-bb", headline="g1 bb low", relevance=0.40,
            market_type="match_result", bet_type="bet_builder",
            hook_type="tactical", fixture_id="g1",
        ),
        _card(
            card_id="g2-single", headline="g2 single high", relevance=0.80,
            market_type="match_result", bet_type="single",
            hook_type="team_news", fixture_id="g2",
        ),
        _card(
            card_id="g2-bb", headline="g2 bb low", relevance=0.35,
            market_type="match_result", bet_type="bet_builder",
            hook_type="preview", fixture_id="g2",
        ),
    ]
    for c in cards:
        feed.add_prematch_card(c)

    app = _build_app(feed)
    client = TestClient(app)
    resp = client.get("/api/feed")
    assert resp.status_code == 200

    ids_in_feed = {c["id"] for c in resp.json()["cards"]}
    expected = {"g1-single", "g1-bb", "g2-single", "g2-bb"}
    assert ids_in_feed == expected, (
        f"every paid card must be visible by default, got {ids_in_feed}"
    )


def test_kill_switch_on_restores_prior_pruning(monkeypatch):
    """Kill switch on: PULSE_PRUNE_PAID_CARDS=true → prior pruning back.

    Same 4-card scenario; the lower-scored same-fixture+same-market
    duplicates must be dropped — only 2 cards visible."""
    monkeypatch.setenv("PULSE_PRUNE_PAID_CARDS", "true")
    _reload_config_modules()

    from app.services.feed_manager import FeedManager
    feed = FeedManager()

    cards = [
        _card(
            card_id="g1-single", headline="g1 single high", relevance=0.85,
            market_type="match_result", bet_type="single",
            fixture_id="g1",
        ),
        _card(
            card_id="g1-bb", headline="g1 bb low", relevance=0.40,
            market_type="match_result", bet_type="bet_builder",
            hook_type="tactical", fixture_id="g1",
        ),
        _card(
            card_id="g2-single", headline="g2 single high", relevance=0.80,
            market_type="match_result", bet_type="single",
            hook_type="team_news", fixture_id="g2",
        ),
        _card(
            card_id="g2-bb", headline="g2 bb low", relevance=0.35,
            market_type="match_result", bet_type="bet_builder",
            hook_type="preview", fixture_id="g2",
        ),
    ]
    for c in cards:
        feed.add_prematch_card(c)

    app = _build_app(feed)
    client = TestClient(app)
    resp = client.get("/api/feed")
    assert resp.status_code == 200

    ids_in_feed = {c["id"] for c in resp.json()["cards"]}
    # Higher-scored card per fixture survives the dedupe; lower-scored
    # duplicate dropped. The ranker keeps the one with the higher
    # `__ranker_score__`, which is dominated by relevance for cards with
    # the same kickoff window + ago_minutes.
    assert "g1-single" in ids_in_feed, "higher-scored g1 card must survive"
    assert "g2-single" in ids_in_feed, "higher-scored g2 card must survive"
    assert "g1-bb" not in ids_in_feed, (
        "kill-switch on: lower-scored g1 duplicate must be pruned"
    )
    assert "g2-bb" not in ids_in_feed, (
        "kill-switch on: lower-scored g2 duplicate must be pruned"
    )
    assert len(ids_in_feed) == 2


def test_kickoff_passed_correctness_drop_still_applies(monkeypatch):
    """The no-show filter (kickoff in the past, suspended) is correctness,
    not pruning — it must keep working with the kill switch off."""
    monkeypatch.delenv("PULSE_PRUNE_PAID_CARDS", raising=False)
    _reload_config_modules()

    from app.services.feed_manager import FeedManager
    feed = FeedManager()

    live_card = _card(
        card_id="future", headline="future", relevance=0.70,
        market_type="match_result", fixture_id="g-future",
    )
    # Kickoff 2h in the past — the ranker's _is_no_show MUST drop it.
    past_card = Card(
        id="past",
        card_type=CardType.PRE_MATCH,
        game=_game(gid="g-past", hours_ahead=-2),
        narrative_hook="hook",
        headline="kickoff passed",
        market=_market(market_type="match_result", mid="m-past"),
        relevance_score=0.99,
        bet_type="single",
        hook_type="injury",
    )
    feed.add_prematch_card(live_card)
    feed.add_prematch_card(past_card)

    app = _build_app(feed)
    client = TestClient(app)
    resp = client.get("/api/feed")
    assert resp.status_code == 200

    ids_in_feed = {c["id"] for c in resp.json()["cards"]}
    assert "future" in ids_in_feed
    assert "past" not in ids_in_feed, (
        "correctness rule (kickoff-passed) MUST still drop, even with "
        "kill switch off"
    )
