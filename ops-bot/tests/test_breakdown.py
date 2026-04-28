"""Tests for /breakdown command formatting and PulseClient.cost_detail().

Tests in this file:
  1. format_breakdown renders the expected text from a canned detail dict.
  2. format_breakdown handles missing optional fields gracefully.
  3. format_breakdown includes the churn notice when republish > 3x unique.
  4. format_breakdown omits the churn notice when republish <= 3x unique.
  5. format_status includes the cards-in-feed line when cost_detail is provided.
  6. format_status omits the cards-in-feed line when cost_detail is None.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make ops_bot importable from tests/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ops_bot.formatting import format_breakdown, format_status


# ── Canned fixtures ────────────────────────────────────────────────────

SAMPLE_DETAIL = {
    "total_usd": 2.68,
    "total_calls": 52,
    "limit_usd": 3.0,
    "days": [{"date": "2026-04-28", "usd": 2.68, "calls": 52, "limit_usd": 3.0}],
    "by_kind": {
        "news_scout": {"usd": 0.57, "calls": 6},
        "rewrite": {"usd": 0.00, "calls": 0},
        "narrative_generator": {"usd": 0.10, "calls": 4},
        "combined_narrative": {"usd": 2.01, "calls": 42},
    },
    "cards_in_feed_now": 11,
    "unique_cards_published_today": 13,
    "republish_events_today": 63,
    "rewrite_cache_hits_today": 357,
}

DATE = "2026-04-28"


# ── 1. Normal render ───────────────────────────────────────────────────

def test_format_breakdown_header():
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "Daily breakdown — 2026-04-28" in out


def test_format_breakdown_total_line():
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "$2.68 / $3.00" in out
    assert "(89%)" in out


def test_format_breakdown_by_kind_section():
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "By kind:" in out
    assert "news_scout" in out
    assert "rewrite" in out


def test_format_breakdown_cache_hit_annotation():
    """Cache-hit count is appended to the rewrite row."""
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "357 cache hits" in out


def test_format_breakdown_cards_line():
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "11 in feed" in out
    assert "13 unique today" in out
    assert "63 publish events" in out


def test_format_breakdown_per_card_kpis():
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    # $/unique card: 2.68 / 13 = $0.21
    assert "$/unique card today" in out
    # $/card in feed: 2.68 / 11 = $0.24
    assert "$/card in feed" in out


def test_format_breakdown_churn_notice_present():
    """Churn notice appears when republish_events > 3x unique_cards."""
    # 63 > 3 * 13 = 39 — notice should appear.
    out = format_breakdown(SAMPLE_DETAIL, DATE)
    assert "boot churn from redeploys" in out


def test_format_breakdown_churn_notice_absent():
    """Churn notice is omitted when republish_events <= 3x unique_cards."""
    detail = {
        **SAMPLE_DETAIL,
        "republish_events_today": 20,  # 20 <= 3 * 13 = 39
    }
    out = format_breakdown(detail, DATE)
    assert "boot churn from redeploys" not in out


# ── 2. Missing optional fields ─────────────────────────────────────────

def test_format_breakdown_no_by_kind():
    """Renders gracefully when by_kind is absent."""
    detail = {**SAMPLE_DETAIL, "by_kind": None}
    out = format_breakdown(detail, DATE)
    assert "By kind: (no data)" in out


def test_format_breakdown_no_cards_fields():
    """Omits cards block and KPIs when card/publish fields are None."""
    detail = {
        **SAMPLE_DETAIL,
        "cards_in_feed_now": None,
        "unique_cards_published_today": None,
        "republish_events_today": None,
        "rewrite_cache_hits_today": None,
    }
    out = format_breakdown(detail, DATE)
    assert "Cards:" not in out
    assert "$/card" not in out
    assert "boot churn" not in out


def test_format_breakdown_zero_total_omits_kpis():
    """When total_usd is 0, $/card KPI lines are omitted."""
    detail = {**SAMPLE_DETAIL, "total_usd": 0.0}
    out = format_breakdown(detail, DATE)
    assert "$/unique card today" not in out
    assert "$/card in feed" not in out


# ── 3. format_status enrichment ────────────────────────────────────────

def test_format_status_with_cost_detail_adds_cards_line():
    """format_status appends cards-in-feed + $/card when cost_detail provided."""
    cost = {"total_usd": 2.68, "total_calls": 52, "limit_usd": 3.0}
    detail = {
        "total_usd": 2.68,
        "cards_in_feed_now": 11,
    }
    out = format_status(
        health={"ok": True},
        cost=cost,
        deployment=None,
        feed=None,
        engine_vars=None,
        cost_detail=detail,
    )
    assert "11 in feed" in out
    assert "/card" in out


def test_format_status_without_cost_detail_no_cards_line():
    """format_status does not include cards line when cost_detail is None."""
    cost = {"total_usd": 2.68, "total_calls": 52, "limit_usd": 3.0}
    out = format_status(
        health={"ok": True},
        cost=cost,
        deployment=None,
        feed=None,
        engine_vars=None,
        cost_detail=None,
    )
    assert "in feed" not in out


def test_format_status_cost_detail_zero_cards_omits_per_card():
    """When cards_in_feed_now is 0, per-card rate is omitted (no div-by-zero)."""
    detail = {"total_usd": 2.68, "cards_in_feed_now": 0}
    out = format_status(
        health={"ok": True},
        cost={"total_usd": 2.68, "total_calls": 10, "limit_usd": 3.0},
        deployment=None,
        feed=None,
        engine_vars=None,
        cost_detail=detail,
    )
    assert "/card" not in out
