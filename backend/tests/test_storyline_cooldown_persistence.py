"""Tests for the SQLite-backed StorylineDetector cooldown
(PR fix/storyline-cooldown-sqlite, 2026-04-28).

The original implementation kept the per-(storyline_type, scope)
cooldown in module-level Python dicts inside
`app/engine/storyline_detector.py`. Container restarts wiped the dicts
and re-armed the cooldown; with 11 redeploys yesterday alone we
re-fired the expensive Haiku+web_search scout multiple times per day.
This file proves:

  1. CandidateStore CRUD round-trip (`get_storyline_cooldown`,
     `set_storyline_cooldown`, `get_storyline_cooldowns_bulk`,
     `clear_storyline_cooldown`).
  2. State survives a fresh CandidateStore instance against the same
     DB file (the container-restart simulation that motivated the PR).
  3. Schema migration is idempotent — `init()` twice in a row is safe.
  4. End-to-end: a StorylineDetector wired to a real (file-backed)
     CandidateStore short-circuits the second `detect()` call within
     the cooldown window without touching the LLM.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_storyline_cooldown_persistence.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.engine.storyline_detector import (
    StorylineDetector,
    _storyline_cooldown_key,
)
from app.models.news import StorylineItem, StorylineParticipant, StorylineType
from app.models.schemas import Game, Sport, Team
from app.services.candidate_store import CandidateStore


# ──────────────────────────────────────────────────────────────────────
# 1) CandidateStore CRUD round-trip
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_set_and_get_roundtrip():
    """A row written via set_storyline_cooldown comes back through
    get_storyline_cooldown with last_scout_at + last_result preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        scope_key = _storyline_cooldown_key(
            StorylineType.GOLDEN_BOOT, "cross-league",
        )
        ts = 1714000000.0
        result = {
            "id": "story_abc",
            "storyline_type": "golden_boot",
            "headline_hint": "Haaland and Watkins both playing",
            "participants": [],
            "detected_at": ts,
        }

        await store.set_storyline_cooldown(scope_key, ts, result)
        got = await store.get_storyline_cooldown(scope_key)
        assert got is not None
        assert got["last_scout_at"] == pytest.approx(ts)
        assert got["last_result"] is not None
        assert got["last_result"]["id"] == "story_abc"
        assert got["last_result"]["storyline_type"] == "golden_boot"

        # Idempotent overwrite — second set_storyline_cooldown wins.
        ts2 = ts + 60.0
        await store.set_storyline_cooldown(scope_key, ts2, None)
        got2 = await store.get_storyline_cooldown(scope_key)
        assert got2 is not None
        assert got2["last_scout_at"] == pytest.approx(ts2)
        assert got2["last_result"] is None  # miss-result memoized

        # Missing key returns None (no row exists).
        miss = await store.get_storyline_cooldown("nope|cross-league")
        assert miss is None


@pytest.mark.asyncio
async def test_cooldown_persists_across_store_instances():
    """Container-restart simulation. Open a store, write a cooldown
    row, close the connection (exit the `async with`), open a fresh
    CandidateStore instance against the same DB path — the row is still
    there. This is the core acceptance criterion: cooldown survives
    container restart, which is what the in-memory dict didn't do."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")

        store_a = CandidateStore(db_path)
        await store_a.init()
        scope_key = _storyline_cooldown_key(
            StorylineType.RELEGATION, "league=la liga",
        )
        await store_a.set_storyline_cooldown(
            scope_key, 1714000000.0,
            {"id": "story_xyz", "storyline_type": "relegation",
             "headline_hint": "", "participants": [], "detected_at": 1714000000.0},
        )

        # New CandidateStore against same file — simulates a container
        # restart with a Railway volume that survived the redeploy.
        store_b = CandidateStore(db_path)
        await store_b.init()
        got = await store_b.get_storyline_cooldown(scope_key)
        assert got is not None, (
            "cooldown row did not survive a fresh CandidateStore instance "
            "— this is the bug PR fix/storyline-cooldown-sqlite closes"
        )
        assert got["last_scout_at"] == pytest.approx(1714000000.0)
        assert got["last_result"]["id"] == "story_xyz"


@pytest.mark.asyncio
async def test_cooldown_idempotent_migration():
    """init() called twice on the same DB doesn't error and doesn't
    drop the storyline_cooldown table."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()
        scope_key = "golden_boot|cross-league"
        await store.set_storyline_cooldown(scope_key, 1714000000.0, None)
        # Second init — must not raise, must preserve the row.
        await store.init()
        got = await store.get_storyline_cooldown(scope_key)
        assert got is not None
        assert got["last_scout_at"] == pytest.approx(1714000000.0)


@pytest.mark.asyncio
async def test_cooldown_bulk_read_returns_only_requested_keys():
    """get_storyline_cooldowns_bulk returns only rows for keys we asked
    about, and silently omits keys with no row. Empty input → empty
    output (no SQL with empty IN clause)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        await store.set_storyline_cooldown("a|cross-league", 100.0, None)
        await store.set_storyline_cooldown("b|cross-league", 200.0, None)
        await store.set_storyline_cooldown("c|cross-league", 300.0, None)

        # Ask for two of the three plus one missing key.
        out = await store.get_storyline_cooldowns_bulk([
            "a|cross-league", "b|cross-league", "missing|cross-league",
        ])
        assert set(out.keys()) == {"a|cross-league", "b|cross-league"}
        assert out["a|cross-league"]["last_scout_at"] == pytest.approx(100.0)
        assert out["b|cross-league"]["last_scout_at"] == pytest.approx(200.0)

        # Empty input → empty output (no SQL injection from empty IN).
        empty = await store.get_storyline_cooldowns_bulk([])
        assert empty == {}


@pytest.mark.asyncio
async def test_cooldown_clear_removes_row():
    """clear_storyline_cooldown drops the row so the next read returns
    None — used by an /admin force-rescan endpoint in a follow-up PR."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        scope_key = "europe_chase|cross-league"
        await store.set_storyline_cooldown(scope_key, 1714000000.0, None)
        assert await store.get_storyline_cooldown(scope_key) is not None

        await store.clear_storyline_cooldown(scope_key)
        assert await store.get_storyline_cooldown(scope_key) is None

        # Idempotent — clearing a missing key is a no-op (no error).
        await store.clear_storyline_cooldown(scope_key)


# ──────────────────────────────────────────────────────────────────────
# 2) Detector integration — cooldown gate skips LLM within window
# ──────────────────────────────────────────────────────────────────────


def _fake_team(name: str) -> Team:
    return Team(
        id=f"t_{name.lower().replace(' ', '_')}",
        name=name,
        short_name=name[:3].upper(),
        color="#000000",
        sport=Sport.SOCCER,
    )


def _fake_game(home: str, away: str, league: str) -> Game:
    """Minimal Game stub for detect() — we only need home_team.name,
    away_team.name, broadcast (used as league label), and start_time."""
    return Game(
        id=f"g_{home}_{away}".lower().replace(" ", "_"),
        sport=Sport.SOCCER,
        home_team=_fake_team(home),
        away_team=_fake_team(away),
        start_time="01 May 20:00 UTC",
        broadcast=league,
    )


def _fake_anthropic_client_with_storyline() -> MagicMock:
    """Return a MagicMock AsyncAnthropic whose messages.create returns
    one tool_use block naming 2 participants. Records call_count so we
    can assert the cooldown gate skipped the second cycle."""
    client = MagicMock()

    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.name = "submit_storyline"
    tool_use_block.input = {
        "type": "golden_boot",
        "headline_hint": "Two strikers playing this weekend",
        "participants": [
            {"player_name": "Alpha", "team_name": "Arsenal", "extra": "10 goals"},
            {"player_name": "Bravo", "team_name": "Brighton", "extra": "9 goals"},
        ],
    }
    response = MagicMock()
    response.content = [tool_use_block]
    response.usage = MagicMock(
        input_tokens=900, output_tokens=200,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )

    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_detect_with_persistent_cooldown_skips_within_window():
    """End-to-end: wire a StorylineDetector to a real CandidateStore,
    call detect() twice within the cooldown window. The second call
    must not hit the LLM — the gate is loaded from SQLite even though
    we threw away the in-process state by building a brand-new
    detector instance for the second call (simulating a container
    restart)."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        games = {
            "g1": _fake_game("Arsenal", "Aston Villa", "Premier League"),
            "g2": _fake_game("Brighton", "Bournemouth", "Premier League"),
            "g3": _fake_game("Chelsea", "Crystal Palace", "Premier League"),
        }

        # First detector instance — fresh process, empty store.
        client_a = _fake_anthropic_client_with_storyline()
        det_a = StorylineDetector(
            client_a,
            min_participants=2,
            verify_enabled=False,  # no second LLM verify call to mock
            store=store,
        )
        # Enable only the primary type. Pre-decoupling this disabled
        # everything to suppress the side-channel expansion pass; with
        # main.py now iterating each type independently and detect()
        # gating on its own _type_enabled flag, the primary call needs
        # its flag true to fire. Keeps the LLM-hit assertion tight.
        det_a._type_enabled = {
            t: (t == StorylineType.GOLDEN_BOOT) for t in det_a._type_enabled
        }
        result_a = await det_a.detect(StorylineType.GOLDEN_BOOT, games)
        assert len(result_a) == 1
        assert result_a[0].storyline_type == StorylineType.GOLDEN_BOOT
        # Exactly one LLM call for the cross-league scout.
        assert client_a.messages.create.await_count == 1, (
            f"expected 1 LLM call, got {client_a.messages.create.await_count}"
        )

        # Verify the row landed in SQLite.
        scope_key = _storyline_cooldown_key(
            StorylineType.GOLDEN_BOOT, "cross-league",
        )
        row = await store.get_storyline_cooldown(scope_key)
        assert row is not None
        assert row["last_result"] is not None

        # Second detector instance — fresh process (container restart),
        # pointing at the same store. Within the 6h cooldown window the
        # cooldown gate must short-circuit and return the cached
        # storyline WITHOUT a new LLM call.
        client_b = _fake_anthropic_client_with_storyline()
        det_b = StorylineDetector(
            client_b,
            min_participants=2,
            verify_enabled=False,
            store=store,
        )
        det_b._type_enabled = {
            t: (t == StorylineType.GOLDEN_BOOT) for t in det_b._type_enabled
        }
        result_b = await det_b.detect(StorylineType.GOLDEN_BOOT, games)
        assert len(result_b) == 1, (
            "second detect() should return the cached storyline"
        )
        assert client_b.messages.create.await_count == 0, (
            "second detect() must not hit the LLM — cooldown gate failed "
            "to load from SQLite (this is the leak-vector regression test)"
        )
        # Same storyline_type and participants as the first call.
        assert result_b[0].storyline_type == StorylineType.GOLDEN_BOOT
        assert {p.team_name for p in result_b[0].participants} == {
            "Arsenal", "Brighton",
        }


@pytest.mark.asyncio
async def test_cooldown_key_format_matches_in_memory_tuple():
    """`_storyline_cooldown_key` is the migration contract. It must
    flatten the original tuple `(type.value, scope.lower())` to a
    string `f"{type.value}|{scope.lower()}"` so callers everywhere
    agree on what row to read/write. Spot-check the format."""
    k1 = _storyline_cooldown_key(StorylineType.GOLDEN_BOOT, "cross-league")
    assert k1 == "golden_boot|cross-league"

    k2 = _storyline_cooldown_key(StorylineType.RELEGATION, "league=La Liga")
    assert k2 == "relegation|league=la liga", "scope must be lowercased"

    k3 = _storyline_cooldown_key(StorylineType.HOME_FORTRESS, "")
    assert k3 == "home_fortress|"

    # Type and scope must combine deterministically.
    k4 = _storyline_cooldown_key(StorylineType.HOME_FORTRESS, "cross-league(expansion)")
    assert k4 == "home_fortress|cross-league(expansion)"
