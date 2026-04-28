"""Tests for the published_cards lifecycle (created_at + TTL sweep).

Proves PR `fix/published-cards-storage` invariants:

  1. `created_at` is stamped on FIRST insert and PRESERVED across
     subsequent INSERT OR REPLACE calls (the rehydrate path bumps
     `snapshotted_at` on every redeploy — `created_at` must not move).
  2. `delete_expired_published_cards` drops only rows whose `expires_at`
     is non-null AND in the past. NULL-expiry rows persist.
  3. `count_unique_published_cards_since` filters on `created_at`, so
     the `unique_cards_published_today` admin field is HONEST and not
     polluted by rehydrate-restamping.
  4. The migration probe in `init()` is idempotent and backfills
     `created_at` for pre-migration rows.

No live LLM, no Rogue HTTP — pure SQLite round-trips against a temp DB.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_published_cards_lifecycle.py -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
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


def _mk_game(game_id: str = "g1") -> Game:
    return Game(
        id=game_id,
        sport=Sport.SOCCER,
        home_team=Team(id="h", name="Home FC", short_name="HOM", color="#fff", sport=Sport.SOCCER),
        away_team=Team(id="a", name="Away FC", short_name="AWY", color="#000", sport=Sport.SOCCER),
        status=GameStatus.SCHEDULED,
    )


def _mk_card(card_id: str, headline: str = "Test card") -> Card:
    return Card(
        id=card_id,
        card_type=CardType.PRE_MATCH,
        game=_mk_game(),
        narrative_hook="hook",
        headline=headline,
        bet_type="single",
    )


async def _read_created_at(db_path: str, card_id: str) -> float | None:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT created_at FROM published_cards WHERE card_id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return float(row[0]) if row[0] is not None else None


async def _read_snapshotted_at(db_path: str, card_id: str) -> float | None:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT snapshotted_at FROM published_cards WHERE card_id = ?",
            (card_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return float(row[0]) if row[0] is not None else None


@pytest.mark.asyncio
async def test_created_at_set_on_first_insert():
    """A fresh upsert stamps `created_at` ≈ now."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        before = time.time()
        c = _mk_card("card-A")
        await store.upsert_published_card(
            card_id=c.id,
            snapshot_json=c.model_dump_json(),
        )
        after = time.time()

        created = await _read_created_at(db_path, "card-A")
        assert created is not None, "created_at must be populated"
        assert before <= created <= after, (
            f"created_at {created} not in [{before}, {after}]"
        )


@pytest.mark.asyncio
async def test_created_at_preserved_on_replace():
    """Re-upserting an existing card_id MUST NOT change created_at,
    even though snapshotted_at moves forward.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        c = _mk_card("card-B", "v1")
        await store.upsert_published_card(
            card_id=c.id,
            snapshot_json=c.model_dump_json(),
        )
        first_created = await _read_created_at(db_path, "card-B")
        first_snap = await _read_snapshotted_at(db_path, "card-B")
        assert first_created is not None

        # Sleep enough that even on a fast clock the timestamps differ.
        await asyncio.sleep(0.05)

        c.headline = "v2"
        await store.upsert_published_card(
            card_id=c.id,
            snapshot_json=c.model_dump_json(),
        )
        second_created = await _read_created_at(db_path, "card-B")
        second_snap = await _read_snapshotted_at(db_path, "card-B")

        assert second_created == first_created, (
            "created_at must be preserved across upserts"
        )
        # snapshotted_at SHOULD advance — that's the rehydrate-ordering
        # signal we deliberately keep moving.
        assert second_snap > first_snap


@pytest.mark.asyncio
async def test_delete_expired_drops_old_rows():
    """Seed rows with mixed expires_at; only past-expiry rows are dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        now = time.time()

        past = _mk_card("past", "expired-2h-ago")
        await store.upsert_published_card(
            card_id=past.id,
            snapshot_json=past.model_dump_json(),
            expires_at=now - 7200,  # 2h ago
        )
        future = _mk_card("future", "expires-in-1h")
        await store.upsert_published_card(
            card_id=future.id,
            snapshot_json=future.model_dump_json(),
            expires_at=now + 3600,  # 1h ahead
        )
        nullexp = _mk_card("nullexp", "no-expiry")
        await store.upsert_published_card(
            card_id=nullexp.id,
            snapshot_json=nullexp.model_dump_json(),
            expires_at=None,
        )

        dropped = await store.delete_expired_published_cards(now)
        assert dropped == 1

        rows = await store.list_published_cards(limit=10)
        ids = {r[0] for r in rows}
        assert "past" not in ids
        assert "future" in ids
        assert "nullexp" in ids


@pytest.mark.asyncio
async def test_count_unique_published_cards_since_filters_on_created_at():
    """Cards created yesterday are NOT counted when since=today_midnight,
    even though their snapshotted_at gets bumped to "now" on a redeploy
    rehydrate (simulated here via direct UPDATE).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()

        now = time.time()
        yesterday = now - 86400 - 3600  # 25h ago, definitely yesterday
        today_midnight = now - 600  # 10 min ago — anchors "today"

        old = _mk_card("old", "yesterday's card")
        await store.upsert_published_card(
            card_id=old.id,
            snapshot_json=old.model_dump_json(),
        )
        # Backdate created_at to simulate yesterday's first insert.
        # Bump snapshotted_at to "now" to simulate a redeploy rehydrate
        # (this is the original bug — snapshotted_at lies after a redeploy).
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE published_cards "
                "SET created_at = ?, snapshotted_at = ? WHERE card_id = ?",
                (yesterday, now, "old"),
            )
            await db.commit()

        new = _mk_card("new", "today's card")
        await store.upsert_published_card(
            card_id=new.id,
            snapshot_json=new.model_dump_json(),
        )

        count = await store.count_unique_published_cards_since(today_midnight)
        assert count == 1, (
            f"only today's card should count; got {count} (the redeploy-restamp bug)"
        )


@pytest.mark.asyncio
async def test_migration_probe_idempotent():
    """Running init() twice on a DB that already has `created_at` must
    be a no-op (no errors, schema unchanged)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(db_path)
        await store.init()
        # Second call must succeed without error.
        await store.init()

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("PRAGMA table_info(published_cards)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
        # `created_at` exists exactly once (probe didn't try to ALTER again).
        assert "created_at" in cols
        # Column count is the expected post-migration set.
        assert cols == {
            "card_id",
            "candidate_id",
            "snapshot_json",
            "snapshotted_at",
            "expires_at",
            "bet_type",
            "storyline_id",
            "created_at",
        }


@pytest.mark.asyncio
async def test_migration_backfill_existing_rows():
    """A pre-migration `published_cards` row (no created_at column) is
    backfilled with `created_at = snapshotted_at` after init() runs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pulse.db")

        # Hand-build a pre-migration table: same shape EXCEPT no
        # created_at column. Insert one row so the backfill has work.
        old_snap_ts = time.time() - 12345.0
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
                    storyline_id TEXT
                )
                """
            )
            await db.execute(
                "INSERT INTO published_cards "
                "(card_id, candidate_id, snapshot_json, snapshotted_at, "
                "expires_at, bet_type, storyline_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("legacy", None, "{}", old_snap_ts, None, "single", None),
            )
            await db.commit()

        store = CandidateStore(db_path)
        await store.init()

        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT created_at, snapshotted_at FROM published_cards "
                "WHERE card_id = ?",
                ("legacy",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        created, snap = float(row[0]), float(row[1])
        assert created == snap == pytest.approx(old_snap_ts, abs=1e-3), (
            f"backfill must set created_at = snapshotted_at; got "
            f"created={created} snap={snap}"
        )
