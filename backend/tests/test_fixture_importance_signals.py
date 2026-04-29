"""Tests for Phase 1 fixture-importance signal plumbing.

Covers:

1. `_map_event_to_game` (catalogue_loader) reads the new Rogue-native
   importance signals onto the `Game` model:
     - LeagueOrder       -> game.league_order
     - IsEarlyPayout     -> game.is_early_payout
     - EarlyPayoutValue  -> game.early_payout_value
     - IsTopLeague       -> game.is_top_league
     - RegionCode        -> game.region_code
     - LeagueGroupId     -> game.league_group_id
   …and that all of them default to None / False when the raw event
   omits them (so old Rogue payloads / minimal stubs don't blow up).

2. `RogueClient.get_featured_events`:
     - returns a list of event dicts on success
     - caches results within the TTL window (single HTTP call across N invocations)
     - returns [] (no exception) on HTTP failure

3. `fetch_soccer_snapshot` post-processing:
     - tags Game.is_operator_featured=True when the fixture id appears
       in the featured-events response
     - leaves all fixtures `is_operator_featured=False` (no exception)
       when the featured-events fetch fails / returns []

No live LLM calls. No live Rogue HTTP. Pure pydantic + monkeypatched
async client.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_fixture_importance_signals.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Synthetic Rogue event payload ─────────────────────────────────────

def _rogue_event(**overrides: Any) -> dict:
    """Minimal Rogue Event-shaped dict that `_map_event_to_game` accepts.

    Any field may be overridden / removed via kwargs. Pop a key by passing
    `field=None` AND then deleting it post-merge (or just construct fresh).
    """
    base: dict[str, Any] = {
        "_id": "evt-1",
        "LeagueName": "Premier League",
        "StartEventDate": "2026-04-29T20:00:00Z",
        "Participants": [
            {"_id": "home-1", "Name": "Home FC", "VenueRole": "Home"},
            {"_id": "away-1", "Name": "Away FC", "VenueRole": "Away"},
        ],
        "Settings": {"IsBetBuilderEnabled": True},
        "LeagueOrder": 1000001,
        "IsEarlyPayout": True,
        "EarlyPayoutValue": 2,
        "IsTopLeague": True,
        "RegionCode": "EU",
        "LeagueGroupId": "uefa-club-cup",
    }
    base.update(overrides)
    return base


# ── (1) _map_event_to_game importance-signal extraction ───────────────

def test_normalize_event_captures_league_order():
    """LeagueOrder propagates to Game.league_order as int."""
    from app.services.catalogue_loader import _map_event_to_game

    game = _map_event_to_game(_rogue_event(LeagueOrder=1000001))
    assert game is not None
    assert game.league_order == 1000001
    assert isinstance(game.league_order, int)


def test_normalize_event_captures_early_payout():
    """IsEarlyPayout + EarlyPayoutValue propagate as bool + float."""
    from app.services.catalogue_loader import _map_event_to_game

    game = _map_event_to_game(_rogue_event(IsEarlyPayout=True, EarlyPayoutValue=2))
    assert game is not None
    assert game.is_early_payout is True
    assert game.early_payout_value == 2.0
    assert isinstance(game.early_payout_value, float)


def test_normalize_event_captures_top_league_and_region():
    """IsTopLeague + RegionCode + LeagueGroupId propagate verbatim."""
    from app.services.catalogue_loader import _map_event_to_game

    game = _map_event_to_game(_rogue_event(
        IsTopLeague=True,
        RegionCode="SAM",
        LeagueGroupId="conmebol-libertadores",
    ))
    assert game is not None
    assert game.is_top_league is True
    assert game.region_code == "SAM"
    assert game.league_group_id == "conmebol-libertadores"
    # is_operator_featured is set later in fetch_soccer_snapshot — defaults False here.
    assert game.is_operator_featured is False


def test_normalize_event_handles_missing_optional_fields():
    """A minimal Rogue dict (no importance signals) yields safe defaults — no exception."""
    from app.services.catalogue_loader import _map_event_to_game

    minimal = {
        "_id": "evt-2",
        "LeagueName": "Some League",
        "StartEventDate": "2026-05-01T18:00:00Z",
        "Participants": [
            {"_id": "h", "Name": "Home", "VenueRole": "Home"},
            {"_id": "a", "Name": "Away", "VenueRole": "Away"},
        ],
    }
    game = _map_event_to_game(minimal)
    assert game is not None
    assert game.league_order is None
    assert game.is_early_payout is False
    assert game.early_payout_value is None
    assert game.is_top_league is False
    assert game.region_code is None
    assert game.league_group_id is None
    assert game.is_operator_featured is False


def test_normalize_event_coerces_bad_league_order_to_none():
    """Non-numeric LeagueOrder shouldn't crash — coerce to None."""
    from app.services.catalogue_loader import _map_event_to_game

    game = _map_event_to_game(_rogue_event(LeagueOrder="not-a-number"))
    assert game is not None
    assert game.league_order is None


# ── (2) RogueClient.get_featured_events ───────────────────────────────

async def _make_client_with_stub(stub_request):
    """Construct a RogueClient and replace `_request` with `stub_request`.

    Must be called from inside a running event loop — Python 3.9
    `asyncio.Lock()` requires one at construction time.
    """
    from app.services.rogue_client import RogueClient

    client = RogueClient(base_url="https://example.test", config_jwt="dummy")

    async def _stub(path, params=None, requires_auth=True):
        result = stub_request(path, params)
        if asyncio.iscoroutine(result):
            return await result
        return result

    client._request = _stub  # type: ignore[assignment]
    return client


def test_get_featured_events_returns_list():
    """Happy path: returns the Events list from the wrapped response."""
    sample = {
        "Events": [
            {"_id": "abc", "EventName": "Team A vs Team B"},
            {"_id": "def", "EventName": "Team C vs Team D"},
        ],
        "TotalCount": 2,
    }
    calls: list[tuple] = []

    def stub(path, params):
        calls.append((path, params))
        return sample

    async def run():
        client = await _make_client_with_stub(stub)
        try:
            return await client.get_featured_events(locale="en")
        finally:
            await client.close()

    events = asyncio.run(run())
    assert isinstance(events, list)
    assert [e["_id"] for e in events] == ["abc", "def"]
    assert calls[0][0] == "/v1/sportsdata/featured/events"


def test_get_featured_events_caches_within_ttl():
    """Two calls in the same TTL window hit HTTP only once."""
    sample = {"Events": [{"_id": "abc"}], "TotalCount": 1}
    fetch_count = {"n": 0}

    def stub(path, params):
        fetch_count["n"] += 1
        return sample

    async def run() -> tuple[list, list]:
        client = await _make_client_with_stub(stub)
        try:
            first = await client.get_featured_events(locale="en")
            second = await client.get_featured_events(locale="en")
            return first, second
        finally:
            await client.close()

    first, second = asyncio.run(run())
    assert fetch_count["n"] == 1, "second call should be served from cache"
    assert first == second


def test_get_featured_events_returns_empty_on_http_failure():
    """If `_request` raises, return [] without bubbling — no boot-time crash."""
    from app.services.rogue_client import RogueApiError

    def stub(path, params):
        raise RogueApiError(503, "upstream busted")

    async def run():
        client = await _make_client_with_stub(stub)
        try:
            return await client.get_featured_events(locale="en")
        finally:
            await client.close()

    events = asyncio.run(run())
    assert events == []


def test_get_featured_events_handles_bare_list_response():
    """Defensive: if Rogue returns a bare list (shape drift), don't crash."""
    sample = [{"_id": "abc"}, {"_id": "def"}]

    def stub(path, params):
        return sample

    async def run():
        client = await _make_client_with_stub(stub)
        try:
            return await client.get_featured_events(locale="en")
        finally:
            await client.close()

    events = asyncio.run(run())
    assert [e["_id"] for e in events] == ["abc", "def"]


# ── (3) catalogue-loader tagging step ─────────────────────────────────

def _make_game(fixture_id: str):
    from app.models.schemas import Game, GameStatus, Sport, Team

    home = Team(id=f"{fixture_id}-h", name="Home FC", short_name="HFC",
                color="#000", sport=Sport.SOCCER)
    away = Team(id=f"{fixture_id}-a", name="Away FC", short_name="AFC",
                color="#fff", sport=Sport.SOCCER)
    return Game(
        id=fixture_id,
        sport=Sport.SOCCER,
        home_team=home,
        away_team=away,
        broadcast="Premier League",
        start_time="29 Apr 20:00 UTC",
        status=GameStatus.SCHEDULED,
    )


class _StubFeaturedClient:
    """Stand-in for RogueClient that just exposes get_featured_events."""

    def __init__(self, featured: list[dict] | None = None, raise_exc: Exception | None = None):
        self._featured = featured or []
        self._raise = raise_exc

    async def get_featured_events(self, *, locale: str = "en") -> list[dict]:
        if self._raise is not None:
            raise self._raise
        return self._featured


def test_catalogue_loader_tags_featured_fixtures():
    """`_tag_operator_featured` flips the flag on any Game whose id matches."""
    from app.services.catalogue_loader import _tag_operator_featured

    games = [_make_game("abc"), _make_game("xyz")]
    client = _StubFeaturedClient(featured=[{"_id": "abc", "EventName": "X vs Y"}])

    asyncio.run(_tag_operator_featured(client, games))

    by_id = {g.id: g for g in games}
    assert by_id["abc"].is_operator_featured is True
    assert by_id["xyz"].is_operator_featured is False


def test_catalogue_loader_skips_tagging_on_featured_failure():
    """If get_featured_events raises, no Game gets tagged and no exception bubbles."""
    from app.services.catalogue_loader import _tag_operator_featured

    games = [_make_game("abc"), _make_game("xyz")]
    client = _StubFeaturedClient(raise_exc=RuntimeError("boom"))

    # Must not raise.
    asyncio.run(_tag_operator_featured(client, games))

    assert all(g.is_operator_featured is False for g in games)


def test_catalogue_loader_skips_tagging_on_empty_featured():
    """Empty featured list = no tagging, no exception."""
    from app.services.catalogue_loader import _tag_operator_featured

    games = [_make_game("abc"), _make_game("xyz")]
    client = _StubFeaturedClient(featured=[])

    asyncio.run(_tag_operator_featured(client, games))

    assert all(g.is_operator_featured is False for g in games)
