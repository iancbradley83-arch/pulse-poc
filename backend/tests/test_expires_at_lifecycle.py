"""Tests for the published_cards `expires_at` lifecycle.

Proves PR `fix/expires-at-backfill` invariants:

  1. Every snapshot insert via `feed.add_prematch_card(...)` lands with a
     non-NULL `expires_at`. Primary path = kickoff + post-kickoff TTL,
     fallback = now + PULSE_CARD_TTL_SECONDS when kickoff is missing or
     unparseable.
  2. Re-snapshotting a card whose kickoff has shifted updates the row's
     `expires_at` (does NOT preserve the stale one). `created_at` is
     still preserved (PR #92 invariant).
  3. The one-shot `init()` backfill stamps `expires_at` for legacy rows
     with NULL `expires_at`, deriving from `snapshot_json.game.start_time`
     when parseable, or COALESCE(created_at, snapshotted_at) + 21600s
     when not. Idempotent — re-running updates 0 rows.
  4. Malformed `snapshot_json` does NOT crash the backfill; the row gets
     the safe fallback.

No live LLM, no Rogue HTTP — pure SQLite + asyncio against a temp DB.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_expires_at_lifecycle.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.models.schemas import (
    Card, CardType, Game, GameStatus, Sport, Team,
)
from app.services.candidate_store import CandidateStore
from app.services.feed_manager import FeedManager


def _mk_game(*, start_time: str = "", game_id: str = "g1") -> Game:
    return Game(
        id=game_id,
        sport=Sport.SOCCER,
        home_team=Team(id="h", name="Home FC", short_name="HOM", color="#fff", sport=Sport.SOCCER),
        away_team=Team(id="a", name="Away FC", short_name="AWY", color="#000", sport=Sport.SOCCER),
        status=GameStatus.SCHEDULED,
        start_time=start_time,
    )


def _mk_card(card_id: str, *, start_time: str = "", headline: str = "Test card") -> Card:
    return Card(
        id=card_id,
        card_type=CardType.PRE_MATCH,
        game=_mk_game(start_time=start_time),
        narrative_hook="hook",
        headline=headline,
        bet_type="single",
    )


def _kickoff_str(dt: datetime) -> str:
    """Format `dt` (UTC) into the catalogue_loader display shape:
    "23 Apr 20:00 UTC" — what `Game.start_time` looks like in prod."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%d %b %H:%M UTC").lstrip("0")


async def _read_expires_at(db_path: str, card_id: str) -> "float | None":
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT expires_at FROM published_cards WHERE card_id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


async def _read_created_at(db_path: str, card_id: str) -> "float | None":
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT created_at FROM published_cards WHERE card_id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


# ── 1. Insert-time stamp: kickoff present ─────────────────────────────────


@pytest.mark.asyncio
async def test_expires_at_set_from_kickoff_on_insert(monkeypatch):
    """A card with a parseable `game.start_time` lands with
    expires_at ≈ kickoff_epoch + PULSE_POSTKICKOFF_TTL_SECONDS."""
    monkeypatch.setenv("PULSE_POSTKICKOFF_TTL_SECONDS", "3600")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        feed = FeedManager(store=store)

        # Kickoff = roughly T+30min from now. Use the catalogue display
        # format so the parser recognizes it.
        kickoff_dt = datetime.now(timezone.utc) + timedelta(minutes=30)
        kickoff_str = _kickoff_str(kickoff_dt)
        kickoff_epoch = kickoff_dt.replace(microsecond=0).timestamp()

        c = _mk_card("card-K", start_time=kickoff_str)
        feed.add_prematch_card(c)

        # Fire-and-forget snapshot — yield until it lands.
        for _ in range(20):
            await asyncio.sleep(0.05)
            exp = await _read_expires_at(db_path, "card-K")
            if exp is not None:
                break

        exp = await _read_expires_at(db_path, "card-K")
        assert exp is not None, "expires_at must be stamped on insert"
        # Allow ±2min tolerance — strptime drops seconds, kickoff_epoch
        # is the truncated minute, so they should agree to within a minute
        # plus our normal scheduling slack.
        expected = kickoff_epoch + 3600
        assert abs(exp - expected) < 120, (
            f"expires_at {exp} should be ≈ kickoff+3600 ({expected}); "
            f"diff={exp - expected:.1f}s"
        )


# ── 2. Insert-time stamp: kickoff missing → fallback to now+TTL ──────────


@pytest.mark.asyncio
async def test_expires_at_falls_back_when_kickoff_missing(monkeypatch):
    """A card with empty `game.start_time` lands with
    expires_at ≈ now + PULSE_CARD_TTL_SECONDS (default 21600s = 6h)."""
    monkeypatch.setenv("PULSE_CARD_TTL_SECONDS", "21600")
    monkeypatch.setenv("PULSE_POSTKICKOFF_TTL_SECONDS", "3600")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        feed = FeedManager(store=store)

        c = _mk_card("card-NoKO", start_time="")  # no kickoff
        before = time.time()
        feed.add_prematch_card(c)
        after = time.time()

        for _ in range(20):
            await asyncio.sleep(0.05)
            exp = await _read_expires_at(db_path, "card-NoKO")
            if exp is not None:
                break

        exp = await _read_expires_at(db_path, "card-NoKO")
        assert exp is not None, "expires_at must be stamped even without kickoff"
        # exp should be in [before+21600, after+21600+1s slack].
        assert before + 21600 - 1 <= exp <= after + 21600 + 1, (
            f"expires_at {exp} not in expected fallback window "
            f"[{before+21600}, {after+21600}]"
        )


# ── 3. Re-snapshot updates expires_at when kickoff shifts ─────────────────


@pytest.mark.asyncio
async def test_expires_at_updates_when_kickoff_shifts(monkeypatch):
    """Re-snapshotting the same card with a NEW kickoff bumps expires_at
    to the new value. created_at is preserved (PR #92 invariant)."""
    monkeypatch.setenv("PULSE_POSTKICKOFF_TTL_SECONDS", "3600")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        feed = FeedManager(store=store)

        # First insert: kickoff at T+30min.
        ko1 = datetime.now(timezone.utc) + timedelta(minutes=30)
        c = _mk_card("card-shift", start_time=_kickoff_str(ko1))
        feed.add_prematch_card(c)
        for _ in range(20):
            await asyncio.sleep(0.05)
            exp1 = await _read_expires_at(db_path, "card-shift")
            if exp1 is not None:
                break
        created_first = await _read_created_at(db_path, "card-shift")
        assert exp1 is not None
        assert created_first is not None

        await asyncio.sleep(0.05)

        # Second insert (rescout): kickoff slipped to T+3h.
        ko2 = datetime.now(timezone.utc) + timedelta(hours=3)
        c.game.start_time = _kickoff_str(ko2)
        feed.add_prematch_card(c)
        # Wait for the new write to land — exp should change.
        for _ in range(40):
            await asyncio.sleep(0.05)
            exp2 = await _read_expires_at(db_path, "card-shift")
            if exp2 is not None and abs(exp2 - exp1) > 60:
                break

        exp2 = await _read_expires_at(db_path, "card-shift")
        created_second = await _read_created_at(db_path, "card-shift")
        assert exp2 is not None
        assert exp2 > exp1 + 3600, (
            f"expires_at should move forward by ~2.5h; got {exp2 - exp1:.1f}s"
        )
        # PR #92 invariant: created_at must NOT move on re-upsert.
        assert created_second == created_first, (
            "created_at must be preserved across upserts"
        )


# ── 4. Backfill is idempotent ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_idempotent(monkeypatch):
    """Running init() a second time after the backfill has stamped every
    row updates 0 rows — backfill is gated on `WHERE expires_at IS NULL`.
    """
    monkeypatch.setenv("PULSE_POSTKICKOFF_TTL_SECONDS", "3600")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")

        # Hand-build a published_cards table with an existing NULL-expiry
        # row, mimicking today's prod state. Use the post-PR-#92 shape so
        # the created_at migration is a no-op and only the expires_at
        # backfill has work.
        snap_ts = time.time() - 7200  # 2h ago
        kickoff_str = _kickoff_str(datetime.now(timezone.utc) + timedelta(hours=2))
        snap_doc = {
            "id": "legacy",
            "game": {"start_time": kickoff_str},
        }
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE published_cards (
                    card_id TEXT PRIMARY KEY,
                    candidate_id TEXT,
                    snapshot_json TEXT NOT NULL,
                    snapshotted_at REAL NOT NULL,
                    expires_at REAL,
                    bet_type TEXT,
                    storyline_id TEXT,
                    created_at REAL
                )
                """
            )
            await db.execute(
                "INSERT INTO published_cards "
                "(card_id, candidate_id, snapshot_json, snapshotted_at, "
                "expires_at, bet_type, storyline_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    None,
                    json.dumps(snap_doc),
                    snap_ts,
                    None,
                    "single",
                    None,
                    snap_ts,
                ),
            )
            await db.commit()

        store = CandidateStore(db_path)
        await store.init()

        first = await _read_expires_at(db_path, "legacy")
        assert first is not None, "first init() must backfill expires_at"

        # Re-init: backfill query is `WHERE expires_at IS NULL`, so the
        # already-stamped row should NOT be re-touched. We assert by
        # reading the value before and after.
        await store.init()
        second = await _read_expires_at(db_path, "legacy")
        assert second == first, "second init() must not modify expires_at"


# ── 5. Backfill survives malformed snapshot_json ──────────────────────────


@pytest.mark.asyncio
async def test_backfill_handles_malformed_snapshot_json(monkeypatch):
    """A row whose snapshot_json is not valid JSON falls back to
    COALESCE(created_at, snapshotted_at) + PULSE_CARD_TTL_SECONDS.
    Backfill must NOT crash."""
    monkeypatch.setenv("PULSE_CARD_TTL_SECONDS", "21600")
    monkeypatch.setenv("PULSE_POSTKICKOFF_TTL_SECONDS", "3600")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")

        anchor_ts = time.time() - 3600  # 1h ago
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE published_cards (
                    card_id TEXT PRIMARY KEY,
                    candidate_id TEXT,
                    snapshot_json TEXT NOT NULL,
                    snapshotted_at REAL NOT NULL,
                    expires_at REAL,
                    bet_type TEXT,
                    storyline_id TEXT,
                    created_at REAL
                )
                """
            )
            await db.execute(
                "INSERT INTO published_cards "
                "(card_id, candidate_id, snapshot_json, snapshotted_at, "
                "expires_at, bet_type, storyline_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "broken",
                    None,
                    "this is { not :: valid JSON",
                    anchor_ts,
                    None,
                    "single",
                    None,
                    anchor_ts,
                ),
            )
            await db.commit()

        store = CandidateStore(db_path)
        # Must not raise.
        await store.init()

        exp = await _read_expires_at(db_path, "broken")
        assert exp is not None, "malformed JSON row must still get a fallback expiry"
        expected = anchor_ts + 21600
        assert abs(exp - expected) < 1, (
            f"malformed-JSON fallback should be anchor+21600 ({expected}); got {exp}"
        )
