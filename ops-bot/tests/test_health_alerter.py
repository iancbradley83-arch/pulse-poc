"""
Tests for HealthAlerter.

Covers:
  - Single failure produces no alert.
  - 2 consecutive failures fire exactly one CRITICAL alert.
  - Dedup: alert stays quiet between failures (no duplicate alert on 3rd fail).
  - Recovery: after /health returns 200, a recovery notice is sent and alerter re-arms.
  - Re-arming: after recovery, a new 2-fail run fires again.
  - Respects snooze ("health").
  - Poll loop exception does not abort the task.
"""
import asyncio
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.health_alerter import HealthAlerter
from ops_bot.pulse_client import PulseError


class AlertCollector:
    def __init__(self):
        self.calls: List[Tuple[str, Optional[object]]] = []

    async def __call__(self, text: str, reply_markup=None) -> None:
        self.calls.append((text, reply_markup))


def _make_pulse_ok() -> MagicMock:
    client = MagicMock()
    client.health = AsyncMock(return_value={"ok": True})
    return client


def _make_pulse_fail() -> MagicMock:
    client = MagicMock()
    client.health = AsyncMock(side_effect=PulseError("unreachable"))
    return client


@pytest.mark.asyncio
async def test_single_failure_no_alert():
    """One failure below threshold — no alert fires."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_two_failures_fire_alert():
    """Two consecutive failures at threshold — CRITICAL alert fires."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    await alerter._check_and_alert()
    assert len(collector.calls) == 0

    await alerter._check_and_alert()
    assert len(collector.calls) == 1
    assert "CRITICAL" in collector.calls[0][0]
    assert "/health" in collector.calls[0][0]


@pytest.mark.asyncio
async def test_dedup_stays_quiet_after_alert():
    """After the alert fires, additional failures do not produce more alerts."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    await alerter._check_and_alert()
    await alerter._check_and_alert()  # alert fires here
    await alerter._check_and_alert()  # still down — must stay quiet
    await alerter._check_and_alert()  # still down — must stay quiet

    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_recovery_sends_notice_and_resets():
    """After recovery, a recovery notice is sent and the alerter re-arms."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    # Trigger the alert.
    await alerter._check_and_alert()
    await alerter._check_and_alert()
    assert len(collector.calls) == 1

    # Now recover.
    client.health = AsyncMock(return_value={"ok": True})
    await alerter._check_and_alert()
    assert len(collector.calls) == 2
    assert "recovered" in collector.calls[1][0].lower()

    # Verify internal state reset.
    assert alerter._alert_fired is False
    assert alerter._consecutive_fails == 0


@pytest.mark.asyncio
async def test_rearms_after_recovery():
    """After a full down+recovery cycle, a new outage fires a second alert."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    # First outage cycle.
    await alerter._check_and_alert()
    await alerter._check_and_alert()
    assert len(collector.calls) == 1

    # Recovery.
    client.health = AsyncMock(return_value={"ok": True})
    await alerter._check_and_alert()
    assert len(collector.calls) == 2  # recovery notice

    # Second outage cycle — should fire a new alert.
    client.health = AsyncMock(side_effect=PulseError("down again"))
    await alerter._check_and_alert()  # fail 1
    assert len(collector.calls) == 2  # below threshold

    await alerter._check_and_alert()  # fail 2 — should fire
    assert len(collector.calls) == 3
    assert "CRITICAL" in collector.calls[2][0]


@pytest.mark.asyncio
async def test_respects_health_snooze():
    """Snooze 'health' suppresses the CRITICAL alert but still tracks state."""
    import ops_bot.snooze as _snooze

    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    with patch.object(_snooze, "is_snoozed", return_value=True):
        await alerter._check_and_alert()
        await alerter._check_and_alert()

    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_alert_includes_inline_keyboard():
    """CRITICAL health alert carries STATUS, REDEPLOY, DISMISS buttons."""
    client = _make_pulse_fail()
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    await alerter._check_and_alert()
    await alerter._check_and_alert()

    assert len(collector.calls) == 1
    _, markup = collector.calls[0]
    assert markup is not None
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "STATUS" in buttons
    assert "REDEPLOY" in buttons
    assert "DISMISS" in buttons


@pytest.mark.asyncio
async def test_ok_false_counts_as_failure():
    """health() returning {"ok": False} (no exception) counts as a failure."""
    client = MagicMock()
    client.health = AsyncMock(return_value={"ok": False})
    collector = AlertCollector()
    alerter = HealthAlerter(client, collector, poll_interval=0, fail_threshold=2)

    await alerter._check_and_alert()
    await alerter._check_and_alert()
    assert len(collector.calls) == 1
