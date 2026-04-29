"""
Tests for DeployAlerter.

Covers:
  - FAILED transition fires exactly one alert.
  - Dedup: same deployment_id + status does not fire twice.
  - CRASHED transition fires.
  - Boot-state recovery: if already FAILED on boot, no spam.
  - Recovery re-arms: second failure on a NEW deployment ID fires again.
  - Respects snooze ("deploy").
  - Ignores non-terminal statuses (BUILDING, SUCCESS).
  - Poll loop exception is logged and does not abort the task.
"""
import asyncio
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.deploy_alerter import DeployAlerter
from ops_bot.railway_client import RailwayError

BUDGET = 3.00


def _make_railway_client(
    dep_id: str = "dep-abc",
    status: str = "SUCCESS",
    commit: str = "abcdefg",
) -> MagicMock:
    client = MagicMock()
    client.latest_deployment = AsyncMock(
        return_value={
            "id": dep_id,
            "status": status,
            "createdAt": "2026-04-28T09:00:00Z",
            "commitHash": commit,
        }
    )
    return client


class AlertCollector:
    def __init__(self):
        self.calls: List[Tuple[str, Optional[object]]] = []

    async def __call__(self, text: str, reply_markup=None) -> None:
        self.calls.append((text, reply_markup))


@pytest.mark.asyncio
async def test_failed_transition_fires_once():
    """A FAILED deployment triggers exactly one alert."""
    client = _make_railway_client(dep_id="dep-001", status="FAILED")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 1
    assert "FAILED" in collector.calls[0][0]

    # Second poll at same deployment/status — must NOT fire again.
    await alerter._check_and_alert()
    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_crashed_transition_fires():
    """A CRASHED deployment triggers an alert."""
    client = _make_railway_client(dep_id="dep-002", status="CRASHED")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 1
    assert "CRASHED" in collector.calls[0][0]


@pytest.mark.asyncio
async def test_success_does_not_fire():
    """SUCCESS status produces no alert."""
    client = _make_railway_client(dep_id="dep-003", status="SUCCESS")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_boot_recovery_marks_failed_as_fired():
    """
    If deployment is already FAILED at boot, initialise() marks it as fired.
    A subsequent poll at the same status must NOT re-alert.
    """
    client = _make_railway_client(dep_id="dep-004", status="FAILED")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    await alerter.initialise()

    # First poll — already fired at boot, no alert.
    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_new_deployment_id_re_arms():
    """
    A new deployment ID with FAILED should fire even if the previous
    deployment was already FAILED (new ID = new event).
    """
    collector = AlertCollector()

    # First deployment: already known FAILED.
    client = _make_railway_client(dep_id="dep-old", status="FAILED")
    alerter = DeployAlerter(client, collector, poll_interval=0)
    await alerter.initialise()  # marks dep-old:FAILED as fired

    # New deployment with a different ID also fails.
    client.latest_deployment = AsyncMock(
        return_value={
            "id": "dep-new",
            "status": "FAILED",
            "createdAt": "2026-04-28T10:00:00Z",
            "commitHash": "1234567",
        }
    )
    await alerter._check_and_alert()
    assert len(collector.calls) == 1
    assert "FAILED" in collector.calls[0][0]


@pytest.mark.asyncio
async def test_respects_deploy_snooze():
    """Snooze 'deploy' suppresses the alert."""
    import ops_bot.snooze as _snooze

    client = _make_railway_client(dep_id="dep-snooze", status="FAILED")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    with patch.object(_snooze, "is_snoozed", return_value=True):
        await alerter._check_and_alert()

    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_railway_error_does_not_crash():
    """RailwayError during poll is logged and swallowed; no exception propagates."""
    client = MagicMock()
    client.latest_deployment = AsyncMock(side_effect=RailwayError("unreachable"))
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    # Must not raise.
    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_alert_includes_inline_keyboard():
    """FAILED alert carries an InlineKeyboardMarkup with REDEPLOY, LOGS, DISMISS."""
    client = _make_railway_client(dep_id="dep-kb", status="FAILED", commit="abc1234")
    collector = AlertCollector()
    alerter = DeployAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 1
    _, markup = collector.calls[0]
    assert markup is not None
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "REDEPLOY" in buttons
    assert "LOGS" in buttons
    assert "DISMISS" in buttons
