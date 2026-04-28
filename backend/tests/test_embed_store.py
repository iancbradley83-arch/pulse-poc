"""Tests for the embed-store CRUD round-trip on `CandidateStore`.

Proves the schema-drift discipline: every Embed field that lands in the
pydantic model also lands in the SQLite row and survives the round trip.
Mirrors the test pattern in `test_published_cards_snapshot.py`
(in-memory SQLite via tempfile, async pytest).

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_embed_store.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.candidate_store import CandidateStore


@pytest.mark.asyncio
async def test_create_and_get_round_trip():
    """create_embed → get_embed_by_slug / by_token preserves every field."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        embed = await store.create_embed(
            slug="apuesta-total",
            display_name="Apuesta Total (Peru)",
            allowed_origins=["*.apuestatotal.com.pe", "apuestatotal.pe", "localhost"],
            notes="seeded",
        )

        assert embed.slug == "apuesta-total"
        assert embed.display_name == "Apuesta Total (Peru)"
        assert embed.allowed_origins == [
            "*.apuestatotal.com.pe", "apuestatotal.pe", "localhost",
        ]
        assert embed.active is True
        assert len(embed.token) >= 32
        assert embed.created_at  # ISO-8601 UTC string

        by_slug = await store.get_embed_by_slug("apuesta-total")
        assert by_slug is not None
        assert by_slug.token == embed.token
        assert by_slug.allowed_origins == embed.allowed_origins
        assert by_slug.notes == "seeded"

        by_token = await store.get_embed_by_token(embed.token)
        assert by_token is not None
        assert by_token.slug == "apuesta-total"


@pytest.mark.asyncio
async def test_list_active_only_filters_soft_deleted():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        await store.create_embed(
            slug="alpha", display_name="A",
            allowed_origins=["a.com"],
        )
        await store.create_embed(
            slug="beta", display_name="B",
            allowed_origins=["b.com"],
        )
        await store.soft_delete_embed("beta")

        all_rows = await store.list_embeds(active_only=False)
        active_rows = await store.list_embeds(active_only=True)

        assert {r.slug for r in all_rows} == {"alpha", "beta"}
        assert {r.slug for r in active_rows} == {"alpha"}
        # Soft-deleted row still has active=False.
        beta = next(r for r in all_rows if r.slug == "beta")
        assert beta.active is False


@pytest.mark.asyncio
async def test_rotate_token_invalidates_old():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        original = await store.create_embed(
            slug="op-1", display_name="Op 1",
            allowed_origins=["op1.com"],
        )
        old_token = original.token

        rotated = await store.rotate_embed_token("op-1")

        assert rotated.token != old_token
        assert rotated.slug == "op-1"
        assert rotated.allowed_origins == ["op1.com"]

        # Old token now resolves to nothing.
        assert await store.get_embed_by_token(old_token) is None
        # New token resolves correctly.
        assert (await store.get_embed_by_token(rotated.token)).slug == "op-1"


@pytest.mark.asyncio
async def test_update_embed_patches_only_named_fields():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        await store.create_embed(
            slug="op-2", display_name="Initial",
            allowed_origins=["x.com"], notes="first",
        )

        updated = await store.update_embed(
            "op-2",
            display_name="Renamed",
            allowed_origins=["x.com", "y.com"],
        )

        assert updated.display_name == "Renamed"
        assert updated.allowed_origins == ["x.com", "y.com"]
        assert updated.notes == "first"  # unchanged
        assert updated.active is True

        # active flip via update_embed.
        toggled = await store.update_embed("op-2", active=False)
        assert toggled.active is False


@pytest.mark.asyncio
async def test_update_unknown_slug_raises():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        with pytest.raises(KeyError):
            await store.update_embed("nope", display_name="X")


@pytest.mark.asyncio
async def test_soft_delete_keeps_row():
    """soft_delete_embed flips active=0 but never DELETEs the row."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        await store.create_embed(
            slug="op-3", display_name="Op 3",
            allowed_origins=["op3.com"],
        )
        await store.soft_delete_embed("op-3")

        embed = await store.get_embed_by_slug("op-3")
        assert embed is not None
        assert embed.active is False


@pytest.mark.asyncio
async def test_init_is_idempotent_on_warm_boot():
    """Re-running init() against an existing DB does NOT clobber rows
    or duplicate-seed. Schema migration is additive only."""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "pulse.db")
        store = CandidateStore(path)
        await store.init()
        await store.create_embed(
            slug="warm", display_name="Warm",
            allowed_origins=["warm.com"],
        )

        # Second init — simulates a redeploy with the volume preserved.
        store2 = CandidateStore(path)
        await store2.init()
        rows = await store2.list_embeds()
        assert len(rows) == 1
        assert rows[0].slug == "warm"
