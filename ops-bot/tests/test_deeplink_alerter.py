"""
Tests for DeeplinkAlerter.

Covers:
  - Fewer than DEEPLINK_FAIL_THRESHOLD failures: no alert.
  - >= DEEPLINK_FAIL_THRESHOLD failures: alert fires.
  - Dedup: same failure mode on same day fires only once.
  - Day rollover resets dedup; alert can fire again.
  - Snooze suppresses alert.
  - Feed fetch failure: no crash, no alert.
"""
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.deeplink_alerter import DeeplinkAlerter, DEEPLINK_FAIL_THRESHOLD, DEEPLINK_SAMPLE_SIZE
from ops_bot.pulse_client import PulseError


def _make_cards(deep_links: List[Optional[str]]) -> List[dict]:
    return [
        {"id": f"card{i:04d}", "deep_link": url}
        for i, url in enumerate(deep_links)
    ]


def _make_pulse_client(deep_links: List[Optional[str]]) -> MagicMock:
    client = MagicMock()
    cards = _make_cards(deep_links)
    client.feed = AsyncMock(return_value={"count": len(cards), "cards": cards})
    return client


class AlertCollector:
    def __init__(self):
        self.calls = []

    async def __call__(self, text: str, reply_markup=None) -> None:
        self.calls.append((text, reply_markup))


@pytest.mark.asyncio
async def test_fewer_than_threshold_no_alert():
    """2/5 failures should not fire an alert (threshold is 3)."""
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    # Patch _head_status: 2 fail (None/404) and 3 succeed.
    statuses = [None, 404, 200, 200, 200]
    side_effects = iter(statuses)

    async def mock_head(url):
        return next(side_effects)

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head):
        await alerter._check_and_alert()

    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_three_failures_fires_alert():
    """3/5 failures should fire exactly one alert."""
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    statuses = [None, 404, 500, 200, 200]
    side_effects = iter(statuses)

    async def mock_head(url):
        return next(side_effects)

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head):
        await alerter._check_and_alert()

    assert len(collector.calls) == 1
    assert "3/5" in collector.calls[0][0]
    assert "WARN" in collector.calls[0][0]


@pytest.mark.asyncio
async def test_dedup_fires_only_once_per_day():
    """Same failure mode on same day should fire at most once."""
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    # All fail.
    async def mock_head_fail(url):
        return 500

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head_fail):
        await alerter._check_and_alert()
        await alerter._check_and_alert()

    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_day_rollover_resets_dedup():
    """After day rollover the alert should be eligible to fire again."""
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    async def mock_head_fail(url):
        return 500

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head_fail):
        await alerter._check_and_alert()

    assert len(collector.calls) == 1

    # Simulate day rollover.
    alerter._current_day = "2099-01-01"
    alerter._fired = set()

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head_fail):
        await alerter._check_and_alert()

    assert len(collector.calls) == 2


@pytest.mark.asyncio
async def test_snooze_suppresses_alert():
    """When deeplink is snoozed, no alert should fire."""
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    async def mock_head_fail(url):
        return 500

    with (
        patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head_fail),
        patch("ops_bot.deeplink_alerter._snooze.is_snoozed", return_value=True),
    ):
        await alerter._check_and_alert()

    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_feed_error_no_crash():
    """PulseError during feed fetch should not crash or fire alert."""
    client = MagicMock()
    client.feed = AsyncMock(side_effect=PulseError("unreachable"))
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    # Must not raise.
    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_no_deep_links_in_feed_no_alert():
    """Feed with cards but no deep_links should produce no alert."""
    cards = [{"id": f"c{i}", "hook_type": "injury"} for i in range(5)]
    client = MagicMock()
    client.feed = AsyncMock(return_value={"count": 5, "cards": cards})
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_alert_has_preview_button():
    """Alert message should include an inline keyboard with PREVIEW button."""
    from aiogram.types import InlineKeyboardMarkup
    urls = [f"https://example.com/{i}" for i in range(5)]
    client = _make_pulse_client(urls)
    collector = AlertCollector()
    alerter = DeeplinkAlerter(client, collector, poll_interval=0)

    async def mock_head_fail(url):
        return 500

    with patch("ops_bot.deeplink_alerter._head_status", side_effect=mock_head_fail):
        await alerter._check_and_alert()

    assert len(collector.calls) == 1
    _, markup = collector.calls[0]
    assert isinstance(markup, InlineKeyboardMarkup)
    button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "PREVIEW" in button_texts
