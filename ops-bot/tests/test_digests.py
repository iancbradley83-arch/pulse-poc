"""
Tests for digest formatting and scheduling helpers.

Covers:
  - format_digest renders all required lines for morning and evening digests.
  - Missing fields fall back gracefully (no KeyError/crash).
  - Yesterday's cost appears only in morning digest.
  - Active snoozes are rendered if present.
  - _seconds_until returns a positive value pointing to next HH:MM UTC.
  - _next_digest picks the soonest upcoming slot.
  - Digest fallback message sent when all fetches fail.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.formatting import format_digest
from ops_bot.digests import _seconds_until, _next_digest, DigestScheduler


# ---------------------------------------------------------------------------
# format_digest
# ---------------------------------------------------------------------------

_HEALTH_OK = {"ok": True}
_COST = {"total_usd": 1.23, "total_calls": 15, "limit_usd": 3.00, "days": []}
_COST_2DAYS = {
    "total_usd": 1.23,
    "total_calls": 15,
    "limit_usd": 3.00,
    "days": [
        {"date": "2026-04-28", "usd": 1.23, "calls": 15, "limit_usd": 3.00},
        {"date": "2026-04-27", "usd": 0.80, "calls": 10, "limit_usd": 3.00},
    ],
}
_DEPLOY = {
    "id": "dep-abc",
    "status": "SUCCESS",
    "createdAt": "2026-04-28T07:00:00Z",
    "commitHash": "abcdefg12345",
}
_COST_DETAIL = {"cards_in_feed_now": 42, "total_usd": 1.23}
_ENGINE_VARS = {
    "PULSE_RERUN_ENABLED": "true",
    "PULSE_NEWS_INGEST_ENABLED": "true",
    "PULSE_TIERED_FRESHNESS_ENABLED": "false",
}


def _morning(**overrides) -> str:
    kwargs = dict(
        digest_kind="morning",
        health=_HEALTH_OK,
        cost_today=_COST,
        cost_yesterday=_COST_2DAYS,
        deployment=_DEPLOY,
        cost_detail=_COST_DETAIL,
        engine_vars=_ENGINE_VARS,
        active_snoozes=None,
    )
    kwargs.update(overrides)
    return format_digest(**kwargs)


def _evening(**overrides) -> str:
    kwargs = dict(
        digest_kind="evening",
        health=_HEALTH_OK,
        cost_today=_COST,
        cost_yesterday=None,
        deployment=_DEPLOY,
        cost_detail=_COST_DETAIL,
        engine_vars=_ENGINE_VARS,
        active_snoozes=None,
    )
    kwargs.update(overrides)
    return format_digest(**kwargs)


def test_morning_header():
    assert "[ops-bot] morning digest" in _morning()


def test_evening_header():
    assert "[ops-bot] evening digest" in _evening()


def test_pulse_ok_shown():
    text = _morning()
    assert "Pulse: ok" in text


def test_pulse_down_shown():
    text = _morning(health={"ok": False})
    assert "Pulse: DOWN" in text


def test_pulse_unreachable_shown():
    text = _morning(health=None)
    assert "unreachable" in text.lower()


def test_cost_today_shown():
    text = _morning()
    assert "$1.23" in text
    assert "15 calls" in text


def test_morning_yesterday_cost_shown():
    text = _morning()
    # Yesterday row date appears.
    assert "2026-04-27" in text
    assert "$0.80" in text


def test_evening_no_yesterday_cost():
    text = _evening()
    # Yesterday's date must NOT appear in evening digest.
    assert "2026-04-27" not in text


def test_deploy_line():
    text = _morning()
    assert "SUCCESS" in text
    assert "abcdefg" in text


def test_cards_in_feed():
    text = _morning()
    assert "42" in text


def test_engine_switches():
    text = _morning()
    assert "rerun=on" in text
    assert "news=on" in text
    assert "storylines=off" in text


def test_active_snoozes_shown():
    snoozes = {"cost": {"expires_at": 0, "remaining_seconds": 3660}}
    text = _morning(active_snoozes=snoozes)
    assert "Snoozed:" in text
    assert "cost" in text


def test_no_snooze_line_when_empty():
    text = _morning(active_snoozes={})
    assert "Snoozed:" not in text


def test_missing_health_no_crash():
    """health=None should not raise."""
    text = _morning(health=None)
    assert "morning digest" in text


def test_missing_cost_no_crash():
    text = _morning(cost_today=None)
    assert "unavailable" in text.lower()


def test_missing_deploy_no_crash():
    text = _morning(deployment=None)
    assert "unavailable" in text.lower()


def test_missing_engine_vars_no_crash():
    text = _morning(engine_vars=None)
    assert "unavailable" in text.lower()


def test_missing_cost_detail_no_crash():
    """cost_detail=None means the cards-in-feed line is just absent."""
    text = _morning(cost_detail=None)
    # Should not raise and should still have the other lines.
    assert "morning digest" in text


# ---------------------------------------------------------------------------
# _seconds_until
# ---------------------------------------------------------------------------

def test_seconds_until_future_same_day():
    """Time in the future today returns a small positive delta."""
    now = datetime(2026, 4, 28, 8, 0, 0, tzinfo=timezone.utc)
    secs = _seconds_until(9, 0, now)
    assert 3590 < secs <= 3600


def test_seconds_until_past_today_wraps_to_tomorrow():
    """If target already passed today, wraps to next day."""
    now = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
    secs = _seconds_until(9, 0, now)
    # Should be ~23h away.
    assert 82000 < secs <= 86400


def test_seconds_until_always_positive():
    """_seconds_until is always positive regardless of current time."""
    for h in [0, 9, 22, 23]:
        for m in [0, 30]:
            now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
            assert _seconds_until(h, m, now) > 0


# ---------------------------------------------------------------------------
# _next_digest
# ---------------------------------------------------------------------------

def test_next_digest_picks_soonest():
    """Before 09:00, the next digest is the 09:00 slot."""
    now = datetime(2026, 4, 28, 8, 0, 0, tzinfo=timezone.utc)
    secs, h, m = _next_digest([(9, 0), (22, 0)], now)
    assert h == 9 and m == 0
    assert 3590 < secs <= 3600


def test_next_digest_between_times():
    """Between 09:00 and 22:00, the next digest is the 22:00 slot."""
    now = datetime(2026, 4, 28, 11, 0, 0, tzinfo=timezone.utc)
    secs, h, m = _next_digest([(9, 0), (22, 0)], now)
    assert h == 22 and m == 0


def test_next_digest_after_last_slot():
    """After 22:00, next is 09:00 next day."""
    now = datetime(2026, 4, 28, 23, 0, 0, tzinfo=timezone.utc)
    secs, h, m = _next_digest([(9, 0), (22, 0)], now)
    assert h == 9 and m == 0


# ---------------------------------------------------------------------------
# DigestScheduler — fallback on total failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_fallback_on_total_failure():
    """If all data fetches fail, a one-line fallback is broadcast."""
    from ops_bot.pulse_client import PulseError
    from ops_bot.railway_client import RailwayError

    pulse = MagicMock()
    pulse.health = AsyncMock(side_effect=PulseError("down"))
    pulse.cost = AsyncMock(side_effect=PulseError("down"))
    pulse.cost_detail = AsyncMock(side_effect=PulseError("down"))

    railway = MagicMock()
    railway.latest_deployment = AsyncMock(side_effect=RailwayError("down"))
    railway.variables = AsyncMock(side_effect=RailwayError("down"))

    sent: list = []

    bot = MagicMock()
    async def fake_send(chat_id, text, **kwargs):
        sent.append(text)
    bot.send_message = fake_send

    scheduler = DigestScheduler(bot, [12345], pulse, railway)
    await scheduler._send_digest("morning")

    assert len(sent) == 1
    assert "unreachable" in sent[0].lower()


@pytest.mark.asyncio
async def test_digest_renders_full_morning():
    """Happy-path: all data available, morning digest contains all sections."""
    from ops_bot.pulse_client import PulseError

    pulse = MagicMock()
    pulse.health = AsyncMock(return_value={"ok": True})
    pulse.cost = AsyncMock(return_value={
        "total_usd": 0.50,
        "total_calls": 5,
        "limit_usd": 3.0,
        "days": [
            {"date": "2026-04-28", "usd": 0.50, "calls": 5, "limit_usd": 3.0},
            {"date": "2026-04-27", "usd": 0.30, "calls": 3, "limit_usd": 3.0},
        ],
    })
    pulse.cost_detail = AsyncMock(return_value={
        "cards_in_feed_now": 10,
        "total_usd": 0.50,
    })

    railway = MagicMock()
    railway.latest_deployment = AsyncMock(return_value={
        "id": "dep-xyz",
        "status": "SUCCESS",
        "createdAt": "2026-04-28T06:00:00Z",
        "commitHash": "abc1234",
    })
    railway.variables = AsyncMock(return_value={
        "PULSE_RERUN_ENABLED": "true",
        "PULSE_NEWS_INGEST_ENABLED": "true",
        "PULSE_TIERED_FRESHNESS_ENABLED": "false",
    })

    sent: list = []
    bot = MagicMock()
    async def fake_send(chat_id, text, **kwargs):
        sent.append(text)
    bot.send_message = fake_send

    scheduler = DigestScheduler(bot, [12345], pulse, railway)
    await scheduler._send_digest("morning")

    assert len(sent) == 1
    text = sent[0]
    assert "morning digest" in text
    assert "Pulse: ok" in text
    assert "$0.50" in text
    assert "SUCCESS" in text
    assert "rerun=on" in text
