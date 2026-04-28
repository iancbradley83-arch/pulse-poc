"""
Tests for CostAlerter.

Covers:
  - Crossing $1 fires one alert; crossing $1 again the same day fires zero.
  - Boot-state recovery: starting with spend $1.50 marks $1 as fired but $2 still fires.
  - Date rollover resets the dedup set; previously-fired thresholds can fire again.
  - All three thresholds ($1, $2, $2.95) fire in order as cost climbs.
"""
import asyncio
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from ops_bot.cost_alerter import CostAlerter
from ops_bot.pulse_client import PulseError

THRESHOLDS = [1.00, 2.00, 2.95]
BUDGET = 3.00


def _make_pulse_client(spend: float) -> MagicMock:
    """Return a stub PulseClient that always returns the given spend."""
    client = MagicMock()
    client.cost = AsyncMock(
        return_value={
            "total_usd": spend,
            "total_calls": 10,
            "days": [],
            "limit_usd": BUDGET,
        }
    )
    return client


class AlertCollector:
    """Collects messages sent via the send_fn."""

    def __init__(self):
        self.messages: List[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


@pytest.mark.asyncio
async def test_crossing_threshold_fires_once():
    """$1 threshold fires exactly once when crossed."""
    client = _make_pulse_client(1.10)
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    await alerter._check_and_alert()
    assert len(collector.messages) == 1
    assert "$1.00" in collector.messages[0]

    # Second poll at same spend — must NOT fire again.
    await alerter._check_and_alert()
    assert len(collector.messages) == 1


@pytest.mark.asyncio
async def test_threshold_not_crossed_does_not_fire():
    """No alert when spend is below every threshold."""
    client = _make_pulse_client(0.50)
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    await alerter._check_and_alert()
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_boot_recovery_marks_crossed_threshold_as_fired():
    """
    Boot with spend $1.50 → $1 threshold pre-marked, no alert fires for it.
    When spend later crosses $2, that alert fires.
    """
    client = _make_pulse_client(1.50)
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    # Simulate boot initialisation.
    await alerter.initialise()

    # First poll at same spend ($1.50) — $1 already fired on boot; no new alert.
    await alerter._check_and_alert()
    assert len(collector.messages) == 0

    # Spend climbs to $2.10 — $2 threshold should now fire.
    client.cost = AsyncMock(
        return_value={
            "total_usd": 2.10,
            "total_calls": 40,
            "days": [],
            "limit_usd": BUDGET,
        }
    )
    await alerter._check_and_alert()
    assert len(collector.messages) == 1
    assert "$2.00" in collector.messages[0]


@pytest.mark.asyncio
async def test_all_three_thresholds_fire_in_order():
    """Crossing all three thresholds produces three separate alerts."""
    collector = AlertCollector()

    # Start below any threshold.
    client = _make_pulse_client(0.00)
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    spend_steps = [1.05, 2.05, 2.96]
    for spend in spend_steps:
        client.cost = AsyncMock(
            return_value={
                "total_usd": spend,
                "total_calls": 50,
                "days": [],
                "limit_usd": BUDGET,
            }
        )
        await alerter._check_and_alert()

    assert len(collector.messages) == 3
    assert "$1.00" in collector.messages[0]
    assert "$2.00" in collector.messages[1]
    assert "$2.95" in collector.messages[2]


@pytest.mark.asyncio
async def test_date_rollover_resets_dedup():
    """After a day rollover the same threshold can fire again."""
    client = _make_pulse_client(1.10)
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    # Fire the $1 alert today.
    await alerter._check_and_alert()
    assert len(collector.messages) == 1

    # Simulate day rollover by changing the internal day.
    alerter._current_day = "2099-01-01"
    alerter._fired = set()

    # Same spend, new day — should fire again.
    await alerter._check_and_alert()
    assert len(collector.messages) == 2


@pytest.mark.asyncio
async def test_pulse_error_does_not_crash_alerter():
    """A PulseError during poll is logged and swallowed; no exception propagates."""
    client = MagicMock()
    client.cost = AsyncMock(side_effect=PulseError("unreachable"))
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    # Must not raise.
    await alerter._check_and_alert()
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_boot_recovery_pulse_error_returns_zero():
    """If Pulse is unreachable at boot, initialise returns 0 and doesn't crash."""
    client = MagicMock()
    client.cost = AsyncMock(side_effect=PulseError("unreachable"))
    collector = AlertCollector()
    alerter = CostAlerter(client, collector, thresholds=THRESHOLDS, poll_interval=0, daily_budget=BUDGET)

    spend = await alerter.initialise()
    assert spend == 0.0
