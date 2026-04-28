"""Tests for the per-kind cost telemetry restored in PR feat/cost-by-kind-telemetry.

Covers:

  1. CandidateStore.add_daily_cost_by_kind / get_daily_cost_by_kind round-trip.
  2. CostTracker.record_call writes BOTH the daily total row AND the
     per-kind bucket.
  3. Per-kind write failure does NOT break the daily total path; a
     warning is logged and execution continues.
  4. The existing daily-total path is unchanged (no regression).
  5. The `_kind_override` contextvar (boot-scout path) overrides the
     bucket label without touching the total.

No live LLM calls. Each test uses a temp SQLite DB.
"""
from __future__ import annotations

import logging
import os
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

from app.services.candidate_store import CandidateStore
from app.services.cost_tracker import (
    CostTracker,
    today_utc,
    set_kind_override,
    reset_kind_override,
)


@pytest.fixture
async def store_factory():
    """Yield a factory producing fresh, initialised CandidateStores on
    temp SQLite DBs. Cleans up files on teardown.
    """
    created: list[str] = []

    async def _make() -> CandidateStore:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False, prefix="pulse_bykind_test_",
        )
        tmp.close()
        created.append(tmp.name)
        store = CandidateStore(tmp.name)
        await store.init()
        return store

    yield _make

    for path in created:
        try:
            os.unlink(path)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────
# 1) Round-trip: write 3 different kinds, read them back
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_and_get_by_kind_roundtrip(store_factory):
    """Three writes across three buckets sum into the right rows; a
    second write into the same bucket aggregates."""
    store = await store_factory()
    day = "2026-04-28"

    await store.add_daily_cost_by_kind(day, "news_scout", 0.40)
    await store.add_daily_cost_by_kind(day, "rewrite", 0.10)
    await store.add_daily_cost_by_kind(day, "boot_scout", 2.11, calls_delta=46)
    # Aggregation: a second news_scout call on the same day stacks.
    await store.add_daily_cost_by_kind(day, "news_scout", 0.17, calls_delta=2)

    out = await store.get_daily_cost_by_kind(day)
    assert set(out.keys()) == {"news_scout", "rewrite", "boot_scout"}
    assert out["news_scout"]["usd"] == pytest.approx(0.57, rel=1e-6)
    assert out["news_scout"]["calls"] == 3        # 1 + 2
    assert out["rewrite"]["usd"] == pytest.approx(0.10, rel=1e-6)
    assert out["rewrite"]["calls"] == 1
    assert out["boot_scout"]["usd"] == pytest.approx(2.11, rel=1e-6)
    assert out["boot_scout"]["calls"] == 46

    # A different day reads empty.
    other = await store.get_daily_cost_by_kind("2026-04-27")
    assert other == {}


# ──────────────────────────────────────────────────────────────────────
# 2) record_call writes BOTH rows
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_call_writes_both_rows():
    """`record_call` MUST invoke both `add_daily_cost` AND
    `add_daily_cost_by_kind`. Stub store records arguments."""
    stub_store = MagicMock()
    stub_store.add_daily_cost = AsyncMock(return_value=None)
    stub_store.add_daily_cost_by_kind = AsyncMock(return_value=None)
    # today_total_usd path may read this; harmless 0.
    stub_store.get_daily_cost_total = AsyncMock(return_value=0.0)

    tracker = CostTracker(store=stub_store, daily_budget_usd=3.0)
    await tracker.record_call(
        model="claude-haiku-4-5", kind="news_scout", cost_usd=0.42,
    )

    stub_store.add_daily_cost.assert_awaited_once()
    stub_store.add_daily_cost_by_kind.assert_awaited_once()
    args = stub_store.add_daily_cost_by_kind.await_args
    # Positional args: (utc_date, kind, usd); kwarg: calls_delta=1
    assert args.args[1] == "news_scout"
    assert args.args[2] == pytest.approx(0.42, rel=1e-6)
    assert args.kwargs.get("calls_delta", 1) == 1


# ──────────────────────────────────────────────────────────────────────
# 3) by-kind failure does NOT break the daily total
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_by_kind_failure_does_not_break_total(caplog):
    """If `add_daily_cost_by_kind` raises, the total still got written
    and a warning is logged. Engine path must continue."""
    stub_store = MagicMock()
    stub_store.add_daily_cost = AsyncMock(return_value=None)
    stub_store.add_daily_cost_by_kind = AsyncMock(
        side_effect=RuntimeError("simulated by-kind failure"),
    )
    stub_store.get_daily_cost_total = AsyncMock(return_value=0.0)

    tracker = CostTracker(store=stub_store, daily_budget_usd=3.0)
    with caplog.at_level(logging.WARNING):
        # Should NOT raise.
        await tracker.record_call(
            model="claude-haiku-4-5", kind="rewrite", cost_usd=0.07,
        )

    # Total path was still hit.
    stub_store.add_daily_cost.assert_awaited_once()
    # Warning logged for the by-kind failure.
    assert any(
        "by-kind write failed" in rec.message
        for rec in caplog.records
    ), f"Expected by-kind failure warning; got {[r.message for r in caplog.records]}"


# ──────────────────────────────────────────────────────────────────────
# 4) No regression on the existing daily-total path
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_daily_total_regression(store_factory):
    """Full roundtrip via the existing total path still works after the
    by-kind addition. `/admin/cost` page reads from
    `get_daily_cost_total` + `get_daily_cost_history`; both must be
    unchanged."""
    store = await store_factory()
    tracker = CostTracker(store=store, daily_budget_usd=3.0)

    for _ in range(3):
        await tracker.record_call(
            model="claude-haiku-4-5", kind="news_scout", cost_usd=0.20,
        )

    total = await tracker.today_total_usd()
    assert total == pytest.approx(0.60, rel=1e-6)

    history = await store.get_daily_cost_history(days=7)
    # Today's row should be present + match the total.
    today_rows = [r for r in history if r["date"] == today_utc()]
    assert len(today_rows) == 1
    assert today_rows[0]["accumulated_usd"] == pytest.approx(0.60, rel=1e-6)
    assert today_rows[0]["calls"] == 3


# ──────────────────────────────────────────────────────────────────────
# 5) record_call end-to-end through the real store writes BOTH
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_call_end_to_end_writes_buckets(store_factory):
    """Three calls with three different kinds → 3 buckets, summed
    correctly. Mirrors acceptance criterion 3."""
    store = await store_factory()
    tracker = CostTracker(store=store, daily_budget_usd=3.0)

    await tracker.record_call(
        model="claude-haiku-4-5", kind="news_scout", cost_usd=0.30,
    )
    await tracker.record_call(
        model="claude-haiku-4-5", kind="rewrite", cost_usd=0.05,
    )
    await tracker.record_call(
        model="claude-haiku-4-5", kind="boot_scout", cost_usd=1.20,
    )

    buckets = await store.get_daily_cost_by_kind(today_utc())
    assert set(buckets.keys()) == {"news_scout", "rewrite", "boot_scout"}
    assert buckets["news_scout"]["usd"] == pytest.approx(0.30, rel=1e-6)
    assert buckets["rewrite"]["usd"] == pytest.approx(0.05, rel=1e-6)
    assert buckets["boot_scout"]["usd"] == pytest.approx(1.20, rel=1e-6)
    # Total path agrees.
    total = await store.get_daily_cost_total(today_utc())
    assert total == pytest.approx(1.55, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────
# 6) Boot-scout contextvar override
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kind_override_contextvar_overrides_bucket(store_factory):
    """When `set_kind_override("boot_scout")` is active, a
    `kind="news_scout"` call routes into the `boot_scout` bucket. The
    daily total is unchanged either way."""
    store = await store_factory()
    tracker = CostTracker(store=store, daily_budget_usd=3.0)

    token = set_kind_override("boot_scout")
    try:
        # The NewsIngester would naturally pass kind="news_scout"; the
        # override re-buckets it as boot_scout.
        await tracker.record_call(
            model="claude-haiku-4-5", kind="news_scout", cost_usd=0.40,
        )
    finally:
        reset_kind_override(token)

    # Subsequent call without override stays as news_scout.
    await tracker.record_call(
        model="claude-haiku-4-5", kind="news_scout", cost_usd=0.20,
    )

    buckets = await store.get_daily_cost_by_kind(today_utc())
    assert buckets.get("boot_scout", {}).get("usd") == pytest.approx(0.40, rel=1e-6)
    assert buckets.get("news_scout", {}).get("usd") == pytest.approx(0.20, rel=1e-6)
    # Total is the sum either way.
    total = await store.get_daily_cost_total(today_utc())
    assert total == pytest.approx(0.60, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────
# 7) Empty / falsy kind defaults to "unknown" instead of writing NULL
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_kind_defaults_to_unknown(store_factory):
    """Defensive: a call site that forgets to pass `kind` (or passes "")
    lands in the `unknown` bucket — never NULL."""
    store = await store_factory()
    tracker = CostTracker(store=store, daily_budget_usd=3.0)

    await tracker.record_call(
        model="claude-haiku-4-5", kind="", cost_usd=0.05,
    )
    buckets = await store.get_daily_cost_by_kind(today_utc())
    assert "unknown" in buckets
    assert buckets["unknown"]["usd"] == pytest.approx(0.05, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────
# 8) Dead alerts import is gone
# ──────────────────────────────────────────────────────────────────────

def test_no_dead_alerts_import_in_cost_tracker():
    """The `from alerts import warn` line and the `_fire_alert` helper
    are dead — Pulse-ops-bot owns the budget alert ladder now. Make sure
    they don't reappear in a future revert."""
    src = (_ROOT / "app" / "services" / "cost_tracker.py").read_text()
    assert "from alerts import" not in src, (
        "Dead `from alerts import` reference resurfaced — pulse-ops-bot "
        "owns the alert ladder; cost_tracker should not re-import the "
        "local-only `alerts` package."
    )
    assert "_fire_alert" not in src, (
        "Dead `_fire_alert` helper resurfaced; remove it again."
    )
    # The replacement marker comment should be present.
    assert "pulse-ops-bot now owns the alert ladder" in src
