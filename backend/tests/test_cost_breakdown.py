"""Tests for the cost breakdown (feat/cost-breakdown-1-5).

What this file proves:
  1. add_daily_cost_by_kind upserts correctly.
  2. get_daily_cost_by_kind returns the expected shape.
  3. CostTracker.record_call writes to daily_cost_by_kind via the store.
  4. CostTracker.today_by_kind returns the expected dict.
  5. increment_daily_counter / get_daily_counter round-trips.
  6. CostTracker.record_rewrite_cache_hit increments the rewrite_cache_hits counter.
  7. CostTracker.today_counters returns the expected dict.
  8. count_unique_cards_published_today counts correctly.

Run with:
    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_cost_breakdown.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.cost_tracker import CostTracker, today_utc
from app.services.candidate_store import CandidateStore


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
async def store_factory():
    """Yield a factory that creates a fresh CandidateStore on a temp DB."""
    created: list[str] = []

    async def _make() -> CandidateStore:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False, prefix="pulse_breakdown_test_",
        )
        tmp.close()
        created.append(tmp.name)
        s = CandidateStore(tmp.name)
        await s.init()
        return s

    yield _make

    for path in created:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
async def tracker_factory(store_factory):
    """Yield a factory that returns a CostTracker on a fresh store."""
    async def _make(budget: float = 3.0) -> tuple[CostTracker, CandidateStore]:
        s = await store_factory()
        t = CostTracker(store=s, daily_budget_usd=budget)
        return t, s

    return _make


# ── 1. add_daily_cost_by_kind ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_daily_cost_by_kind_basic(store_factory):
    store = await store_factory()
    date = today_utc()
    await store.add_daily_cost_by_kind(date, "news_scout", 0.30, calls_delta=2)
    result = await store.get_daily_cost_by_kind(date)
    assert "news_scout" in result
    assert result["news_scout"]["usd"] == pytest.approx(0.30, rel=1e-6)
    assert result["news_scout"]["calls"] == 2


@pytest.mark.asyncio
async def test_add_daily_cost_by_kind_accumulates(store_factory):
    store = await store_factory()
    date = today_utc()
    await store.add_daily_cost_by_kind(date, "rewrite", 0.10, calls_delta=1)
    await store.add_daily_cost_by_kind(date, "rewrite", 0.05, calls_delta=1)
    result = await store.get_daily_cost_by_kind(date)
    assert result["rewrite"]["usd"] == pytest.approx(0.15, rel=1e-6)
    assert result["rewrite"]["calls"] == 2


@pytest.mark.asyncio
async def test_add_daily_cost_by_kind_multiple_kinds(store_factory):
    store = await store_factory()
    date = today_utc()
    await store.add_daily_cost_by_kind(date, "news_scout", 0.50)
    await store.add_daily_cost_by_kind(date, "rewrite", 0.20)
    await store.add_daily_cost_by_kind(date, "storyline_scout", 0.00)
    result = await store.get_daily_cost_by_kind(date)
    assert len(result) == 3
    assert result["news_scout"]["usd"] == pytest.approx(0.50)
    assert result["rewrite"]["usd"] == pytest.approx(0.20)
    assert result["storyline_scout"]["usd"] == pytest.approx(0.0)


# ── 2. CostTracker.record_call writes by-kind ─────────────────────────

@pytest.mark.asyncio
async def test_record_call_writes_by_kind(tracker_factory):
    tracker, store = await tracker_factory()
    await tracker.record_call(model="claude-haiku-4-5", kind="news_scout", cost_usd=0.40)
    by_kind = await store.get_daily_cost_by_kind(today_utc())
    assert "news_scout" in by_kind
    assert by_kind["news_scout"]["usd"] == pytest.approx(0.40, rel=1e-6)
    assert by_kind["news_scout"]["calls"] == 1


@pytest.mark.asyncio
async def test_today_by_kind_matches_store(tracker_factory):
    tracker, store = await tracker_factory()
    await tracker.record_call(model="claude-haiku-4-5", kind="rewrite", cost_usd=0.05)
    await tracker.record_call(model="claude-haiku-4-5", kind="rewrite", cost_usd=0.03)
    await tracker.record_call(model="claude-haiku-4-5", kind="news_scout", cost_usd=0.12)
    by_kind = await tracker.today_by_kind()
    assert by_kind["rewrite"]["usd"] == pytest.approx(0.08, rel=1e-5)
    assert by_kind["rewrite"]["calls"] == 2
    assert by_kind["news_scout"]["usd"] == pytest.approx(0.12, rel=1e-6)
    assert by_kind["news_scout"]["calls"] == 1


# ── 3. Daily counters ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_increment_and_get_daily_counter(store_factory):
    store = await store_factory()
    date = today_utc()
    await store.increment_daily_counter(date, "rewrite_cache_hits", 5)
    await store.increment_daily_counter(date, "rewrite_cache_hits", 3)
    val = await store.get_daily_counter(date, "rewrite_cache_hits")
    assert val == 8


@pytest.mark.asyncio
async def test_get_daily_counter_missing_returns_zero(store_factory):
    store = await store_factory()
    val = await store.get_daily_counter(today_utc(), "nonexistent_counter")
    assert val == 0


@pytest.mark.asyncio
async def test_record_rewrite_cache_hit(tracker_factory):
    tracker, store = await tracker_factory()
    await tracker.record_rewrite_cache_hit()
    await tracker.record_rewrite_cache_hit()
    await tracker.record_rewrite_cache_hit()
    counters = await tracker.today_counters()
    assert counters["rewrite_cache_hits"] == 3


@pytest.mark.asyncio
async def test_record_republish_event(tracker_factory):
    tracker, store = await tracker_factory()
    for _ in range(5):
        await tracker.record_republish_event()
    counters = await tracker.today_counters()
    assert counters["republish_events"] == 5


@pytest.mark.asyncio
async def test_today_counters_shape(tracker_factory):
    tracker, _ = await tracker_factory()
    counters = await tracker.today_counters()
    assert set(counters.keys()) >= {"rewrite_cache_hits", "republish_events"}


# ── 4. count_unique_cards_published_today ─────────────────────────────

@pytest.mark.asyncio
async def test_count_unique_cards_published_today(store_factory):
    store = await store_factory()
    now = time.time()

    # Publish two distinct cards.
    await store.upsert_published_card(
        card_id="card-1",
        candidate_id="cand-1",
        snapshot_json='{"id":"card-1"}',
        expires_at=None,
        bet_type="single",
        storyline_id=None,
    )
    await store.upsert_published_card(
        card_id="card-2",
        candidate_id=None,
        snapshot_json='{"id":"card-2"}',
        expires_at=None,
        bet_type="bb",
        storyline_id=None,
    )

    date_str = today_utc()
    count = await store.count_unique_cards_published_today(date_str)
    assert count == 2


@pytest.mark.asyncio
async def test_count_unique_cards_published_today_dedupes(store_factory):
    """Re-publishing the same card_id does not inflate the count."""
    store = await store_factory()

    await store.upsert_published_card(
        card_id="card-1",
        candidate_id="cand-1",
        snapshot_json='{"id":"card-1","v":1}',
        expires_at=None,
        bet_type="single",
        storyline_id=None,
    )
    await store.upsert_published_card(
        card_id="card-1",
        candidate_id="cand-1",
        snapshot_json='{"id":"card-1","v":2}',
        expires_at=None,
        bet_type="single",
        storyline_id=None,
    )

    count = await store.count_unique_cards_published_today(today_utc())
    assert count == 1
