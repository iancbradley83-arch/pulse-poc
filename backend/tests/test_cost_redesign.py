"""Tests for the cost-aware redesign (PR fix/all-haiku-and-cost-tripwire).

What this file proves:

  1. **All-Haiku.** Every active LLM call site uses `claude-haiku-4-5`
     (no Sonnet aliases) and every cacheable system prompt block ships
     with `cache_control` set.
  2. **Rewrite cache key.** `_cache_key` no longer includes `total_odds`
     — equivalent calls with different prices produce the same hash.
  3. **CostTracker.** `can_spend` returns False when projected + today
     exceeds budget × 0.99; `record_call` accumulates correctly;
     pricing math from a fake `usage` block lines up with the env
     coefficients.
  4. **No live LLM calls.** Every test mocks `AsyncAnthropic` with a
     fake client that records arguments and returns a canned response.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_cost_redesign.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.engine import narrative_rewriter as nr_mod
from app.engine import combined_narrative_author as cna_mod
from app.engine import storyline_detector as sd_mod
from app.services import news_ingester as ni_mod
from app.services.cost_tracker import CostTracker
from app.services.candidate_store import CandidateStore


# ──────────────────────────────────────────────────────────────────────
# 1) All-Haiku grep tests
# ──────────────────────────────────────────────────────────────────────

# Files that may legitimately reference older models (legacy comments,
# telemetry-bucket names, the un-imported narrative_generator). Active
# call sites must not.
ACTIVE_FILES = [
    _ROOT / "app" / "engine" / "narrative_rewriter.py",
    _ROOT / "app" / "engine" / "combined_narrative_author.py",
    _ROOT / "app" / "engine" / "storyline_detector.py",
    _ROOT / "app" / "services" / "news_ingester.py",
]


def test_no_sonnet_in_active_call_sites():
    """No active LLM call site references a Sonnet model alias."""
    pattern = re.compile(r"claude-[\w-]*sonnet[\w-]*", re.IGNORECASE)
    offenders: list[tuple[str, int, str]] = []
    for f in ACTIVE_FILES:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            # Skip commentary lines that historically reference Sonnet.
            if line.lstrip().startswith("#") or '"""' in line:
                continue
            if pattern.search(line):
                offenders.append((f.name, i, line.strip()))
    assert not offenders, f"Sonnet lingering: {offenders}"


def test_default_model_kwarg_is_haiku():
    """Constructor defaults all use claude-haiku-4-5 — the engine
    falls back to Haiku even if the env var is unset on Railway."""
    import inspect
    pairs = [
        (nr_mod.NarrativeRewriter, "model"),
        (cna_mod.CombinedNarrativeAuthor, "model"),
        (sd_mod.StorylineDetector, "model"),
    ]
    for cls, kw in pairs:
        sig = inspect.signature(cls.__init__)
        default = sig.parameters[kw].default
        assert default == "claude-haiku-4-5", (
            f"{cls.__name__}.{kw} default = {default!r}; expected 'claude-haiku-4-5'"
        )


# ──────────────────────────────────────────────────────────────────────
# 2) Cache-control on stable system prompts
# ──────────────────────────────────────────────────────────────────────

def test_cache_control_present_in_voice_brief_call():
    """NarrativeRewriter passes cache_control on the system prompt."""
    src = (_ROOT / "app" / "engine" / "narrative_rewriter.py").read_text()
    # The system block must declare cache_control (ephemeral or 1h).
    assert re.search(
        r'system=\[\{[^}]*"cache_control"\s*:\s*\{"type":\s*"ephemeral"',
        src, re.DOTALL,
    ), "narrative_rewriter messages.create missing cache_control"


def test_cache_control_present_in_combined_narrative_call():
    """CombinedNarrativeAuthor uses ephemeral 1h cache (low frequency)."""
    src = (_ROOT / "app" / "engine" / "combined_narrative_author.py").read_text()
    assert re.search(
        r'system=\[\{[^}]*"cache_control"\s*:\s*\{"type":\s*"ephemeral"\s*,\s*"ttl"\s*:\s*"1h"',
        src, re.DOTALL,
    ), "combined_narrative_author missing 1h cache_control"


def test_cache_control_present_in_news_ingester_call():
    """NewsIngester uses ephemeral cache on the SCOUT system prompt."""
    src = (_ROOT / "app" / "services" / "news_ingester.py").read_text()
    assert re.search(
        r'system=\[\{[^}]*"cache_control"\s*:\s*\{"type":\s*"ephemeral"',
        src, re.DOTALL,
    ), "news_ingester messages.create missing cache_control"


# ──────────────────────────────────────────────────────────────────────
# 3) Rewrite cache key — total_odds removed
# ──────────────────────────────────────────────────────────────────────

def test_rewrite_cache_key_excludes_total_odds():
    """Calls that differ ONLY in total_odds must share a cache key.

    Before the redesign, the cache key included total_odds, so SSE
    pricing ticks busted the cache every cycle. The fix keys on the
    thesis only — bet_type, hook_type, headline, leg market identities,
    and news mentions.
    """
    from app.models.schemas import CardLeg

    base_legs = [
        CardLeg(
            selection_id="sel-1",
            label="Arsenal to win",
            odds=2.50,
            market_label="1X2",
            market_id="m1",
            event_id="e1",
        ),
        CardLeg(
            selection_id="sel-2",
            label="Over 2.5 Goals",
            odds=1.85,
            market_label="Total Goals",
            market_id="m2",
            event_id="e1",
        ),
    ]
    k1 = nr_mod._cache_key(
        bet_type="bb",
        hook_type="injury",
        headline="Palmer doubtful — Chelsea's only spark walks out",
        legs=base_legs,
        news_mentions=["Cole Palmer", "Chelsea"],
    )
    k2 = nr_mod._cache_key(
        bet_type="bb",
        hook_type="injury",
        headline="Palmer doubtful — Chelsea's only spark walks out",
        legs=base_legs,
        news_mentions=["chelsea", "Cole Palmer"],  # casing + order
    )
    assert k1 == k2, "Cache key must be order/case insensitive on mentions"

    k3 = nr_mod._cache_key(
        bet_type="bb",
        hook_type="injury",
        headline="DIFFERENT — opponents smell blood",
        legs=base_legs,
        news_mentions=["Cole Palmer", "Chelsea"],
    )
    assert k1 != k3, "Different headlines must produce different keys"


def test_rewrite_cache_key_signature_no_total_odds():
    """Static guarantee: the keyword `total_odds` is no longer a parameter
    of `_cache_key`. Prevents the bug from regressing via a future PR
    that 'helpfully' restores it."""
    import inspect
    sig = inspect.signature(nr_mod._cache_key)
    assert "total_odds" not in sig.parameters, (
        "total_odds must NOT be in _cache_key signature"
    )


# ──────────────────────────────────────────────────────────────────────
# 4) CostTracker — budget gating + accumulation
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
async def cost_tracker_factory():
    """Yield a factory that builds a CostTracker on a temp SQLite DB.

    Each tracker gets its own DB so tests run in parallel safely. The
    fixture cleans up the file on teardown.
    """
    created: list[str] = []

    async def _make(*, budget: float = 3.0, **rates: float) -> CostTracker:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False, prefix="pulse_cost_test_"
        )
        tmp.close()
        created.append(tmp.name)
        store = CandidateStore(tmp.name)
        await store.init()
        return CostTracker(store=store, daily_budget_usd=budget, **rates)

    yield _make

    for path in created:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_can_spend_blocks_above_budget(cost_tracker_factory):
    """When today_total = $2.55 and projected = $0.50 on a $3 budget,
    can_spend returns False (3.05 > 3.00 × 0.99 = 2.97)."""
    tracker = await cost_tracker_factory(budget=3.0)
    await tracker.record_call(model="claude-haiku-4-5", kind="test", cost_usd=2.55)
    assert (await tracker.today_total_usd()) == pytest.approx(2.55, rel=1e-6)
    assert await tracker.can_spend(0.50) is False


@pytest.mark.asyncio
async def test_can_spend_allows_below_budget(cost_tracker_factory):
    """Healthy budget headroom returns True."""
    tracker = await cost_tracker_factory(budget=3.0)
    await tracker.record_call(model="claude-haiku-4-5", kind="test", cost_usd=0.50)
    assert await tracker.can_spend(0.50) is True


@pytest.mark.asyncio
async def test_record_call_accumulates(cost_tracker_factory):
    """Sequential record_call invocations sum into the daily total."""
    tracker = await cost_tracker_factory(budget=3.0)
    for _ in range(3):
        await tracker.record_call(
            model="claude-haiku-4-5", kind="news_scout", cost_usd=0.40,
        )
    total = await tracker.today_total_usd()
    assert total == pytest.approx(1.20, rel=1e-6)


@pytest.mark.asyncio
async def test_snapshot_shape(cost_tracker_factory):
    """`snapshot()` returns the keys consumed by /admin/rerun/status."""
    tracker = await cost_tracker_factory(budget=3.0)
    snap = await tracker.snapshot()
    assert set(snap.keys()) >= {
        "day_utc", "total_usd", "budget_usd", "remaining_usd",
        "calls", "percent_used",
    }
    assert snap["budget_usd"] == pytest.approx(3.0)
    assert snap["total_usd"] == pytest.approx(0.0)


def test_estimate_haiku_call_math():
    """Pre-call estimate uses input + output × rates + websearch addon."""
    tracker = CostTracker(
        store=MagicMock(),
        daily_budget_usd=3.0,
        haiku_input_per_mtoken=1.0,
        haiku_output_per_mtoken=5.0,
        websearch_per_call_usd=0.025,
    )
    # 1k input + 1k output, no websearch.
    cost = tracker.estimate_haiku_call(
        input_tokens=1000, max_output_tokens=1000, web_search=False,
    )
    assert cost == pytest.approx(0.001 + 0.005, rel=1e-6)
    # With 5 websearch calls: + $0.125
    cost_ws = tracker.estimate_haiku_call(
        input_tokens=1000, max_output_tokens=1000,
        web_search=True, web_search_calls=5,
    )
    assert cost_ws == pytest.approx(0.001 + 0.005 + 5 * 0.025, rel=1e-6)


def test_cost_from_usage_handles_cache_read():
    """cache_read tokens use the discounted rate."""
    tracker = CostTracker(
        store=MagicMock(),
        daily_budget_usd=3.0,
        haiku_input_per_mtoken=1.0,
        haiku_output_per_mtoken=5.0,
        haiku_cache_read_per_mtoken=0.10,
        haiku_cache_write_5m_per_mtoken=1.25,
        websearch_per_call_usd=0.025,
    )
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 2000,  # most of system prompt cache-hit
        "cache_creation_input_tokens": 0,
    }
    cost = tracker.cost_from_usage(usage, web_search=False)
    expected = (
        100 * 1.0 / 1e6
        + 50 * 5.0 / 1e6
        + 2000 * 0.10 / 1e6
    )
    assert cost == pytest.approx(expected, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────
# 5) Caller short-circuits when budget is exhausted (mocked client)
# ──────────────────────────────────────────────────────────────────────

class _FakeClient:
    """Mock AsyncAnthropic; records every messages.create call."""
    def __init__(self):
        self.messages = MagicMock()
        self.messages.create = AsyncMock(return_value=MagicMock(
            content=[],
            usage=MagicMock(
                input_tokens=10, output_tokens=10,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
        ))


@pytest.mark.asyncio
async def test_rewriter_short_circuits_when_over_budget(cost_tracker_factory):
    """When the tripwire trips, NarrativeRewriter returns None and never
    calls Anthropic."""
    from app.models.news import (
        CandidateCard, NewsItem, HookType, BetType, CandidateStatus,
    )

    tracker = await cost_tracker_factory(budget=0.01)
    # Push spend above budget.
    await tracker.record_call(model="claude-haiku-4-5", kind="setup", cost_usd=0.02)

    fake = _FakeClient()
    rewriter = nr_mod.NarrativeRewriter(
        client=fake, model="claude-haiku-4-5", cost_tracker=tracker,
        store=None, cache_enabled=False,
    )
    news = NewsItem(
        id="n1",
        fixture_id="f1",
        source="test",
        source_name="test",
        headline="raw headline",
        summary="raw summary",
        hook_type=HookType.OTHER,
        confidence=0.8,
    )
    cand = CandidateCard(
        id="c1",
        fixture_id="f1",
        bet_type=BetType.SINGLE,
        hook_type=HookType.OTHER,
        score=0.9,
        status=CandidateStatus.DRAFT,
        market_id="m1",
        selection_ids=["sel-1"],
    )
    out = await rewriter.rewrite(
        news=news, market=None, game=None, candidate=cand,
        legs=None, total_odds=None,
    )
    assert out is None
    assert fake.messages.create.await_count == 0, (
        "messages.create must NOT fire when budget is exhausted"
    )
