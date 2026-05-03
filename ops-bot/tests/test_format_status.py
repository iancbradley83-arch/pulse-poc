"""Tests for ops-bot status display banner (PR #126).

Verifies the visual treatment for degraded / failed states:
  * Top banner prepended when Pulse OR Railway unreachable
  * "(unavailable)" lines prefixed with ⚠️ when the cause is Railway
  * Bottom warning text is action-oriented (points at env-var scope)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ops_bot.formatting import format_status


def _ok_health():
    return {"ok": True}


def _ok_cost():
    return {"total_usd": 1.91, "total_calls": 71, "limit_usd": 3.0}


def _ok_feed():
    return {"count": 28}


def _ok_engine_vars():
    return {
        "PULSE_RERUN_ENABLED": "true",
        "PULSE_NEWS_INGEST_ENABLED": "true",
        "PULSE_TIERED_FRESHNESS_ENABLED": "true",
    }


def _ok_deployment():
    return {
        "status": "SUCCESS", "commitHash": "abc1234567",
        "createdAt": "2026-05-03T15:00:00Z",
    }


# ── Healthy state — no banner ────────────────────────────────────────


def test_healthy_state_no_banner():
    out = format_status(
        health=_ok_health(), cost=_ok_cost(), deployment=_ok_deployment(),
        feed=_ok_feed(), engine_vars=_ok_engine_vars(),
    )
    # No banner emoji at the top
    first_line = out.splitlines()[0]
    assert "🚨" not in first_line and "⚠️" not in first_line
    assert first_line == "Pulse: ok"
    # No bottom warning lines
    assert "unreachable" not in out


# ── Railway-only degraded ────────────────────────────────────────────


def test_railway_unreachable_shows_top_banner():
    out = format_status(
        health=_ok_health(), cost=_ok_cost(),
        deployment=None,  # Railway down → no deployment
        feed=_ok_feed(),
        engine_vars=None,  # Railway down → no engine vars
        railway_unreachable=True,
    )
    lines = out.splitlines()
    # Banner appears as first line
    assert "⚠️ DEGRADED" in lines[0]
    assert "Railway ops blind" in lines[0]
    # Pulse status still visible after banner + blank line
    assert any(l == "Pulse: ok" for l in lines)


def test_railway_unreachable_marks_deploy_engine_lines():
    out = format_status(
        health=_ok_health(), cost=_ok_cost(),
        deployment=None, feed=_ok_feed(), engine_vars=None,
        railway_unreachable=True,
    )
    assert "Deploy: ⚠️ (unavailable)" in out
    assert "Engine: ⚠️ (unavailable)" in out


def test_railway_unreachable_action_oriented_bottom():
    out = format_status(
        health=_ok_health(), cost=_ok_cost(),
        deployment=None, feed=_ok_feed(), engine_vars=None,
        railway_unreachable=True,
    )
    # Bottom warning points at the recurring root cause
    assert "RAILWAY_API_TOKEN scope" in out


def test_unavailable_without_railway_unreachable_no_warning_emoji():
    """If deployment is None for some other reason but Railway IS
    reachable (rare), we don't slap a ⚠️ on the line."""
    out = format_status(
        health=_ok_health(), cost=_ok_cost(),
        deployment=None, feed=_ok_feed(),
        engine_vars=_ok_engine_vars(),
        railway_unreachable=False,
    )
    assert "Deploy: (unavailable)" in out
    assert "Deploy: ⚠️" not in out


# ── Pulse-only critical ──────────────────────────────────────────────


def test_pulse_unreachable_shows_critical_banner():
    out = format_status(
        health=None, cost=None, deployment=_ok_deployment(),
        feed=None, engine_vars=_ok_engine_vars(),
        pulse_unreachable=True,
    )
    first_line = out.splitlines()[0]
    assert first_line.startswith("🚨 CRITICAL")
    assert "Pulse unreachable" in first_line


# ── Both unreachable ─────────────────────────────────────────────────


def test_both_unreachable_combined_critical_banner():
    out = format_status(
        health=None, cost=None, deployment=None,
        feed=None, engine_vars=None,
        pulse_unreachable=True, railway_unreachable=True,
    )
    first_line = out.splitlines()[0]
    assert first_line.startswith("🚨 CRITICAL")
    assert "Pulse + Railway both unreachable" in first_line
    # Both bottom warning lines present
    assert "RAILWAY_API_TOKEN scope" in out
    assert "pulse-poc service health" in out


# ── Banner positioning ──────────────────────────────────────────────


def test_banner_followed_by_blank_line_then_pulse():
    """Banner separates from data with a blank line for readability."""
    out = format_status(
        health=_ok_health(), cost=_ok_cost(),
        deployment=None, feed=_ok_feed(), engine_vars=None,
        railway_unreachable=True,
    )
    lines = out.splitlines()
    assert "DEGRADED" in lines[0]
    assert lines[1] == ""
    assert lines[2] == "Pulse: ok"
