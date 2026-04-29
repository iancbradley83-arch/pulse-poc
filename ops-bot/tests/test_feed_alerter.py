"""
Tests for FeedAlerter.

Covers:
  - <5 cards triggers low-card alert.
  - >80% same hook_type triggers hook-collapse alert.
  - Daily dedup: each condition fires at most once per UTC day.
  - Day rollover resets the dedup set.
  - Respects snooze ("feed").
  - PulseError during poll is swallowed.
  - Alert includes inline keyboard with FEED, RERUN, DISMISS.
  - Healthy feed (>=5 cards, varied hooks) fires no alerts.
"""
import asyncio
from datetime import date
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.feed_alerter import FeedAlerter
from ops_bot.pulse_client import PulseError


class AlertCollector:
    def __init__(self):
        self.calls: List[Tuple[str, Optional[object]]] = []

    async def __call__(self, text: str, reply_markup=None) -> None:
        self.calls.append((text, reply_markup))


def _make_feed_client(cards: list) -> MagicMock:
    client = MagicMock()
    client.feed = AsyncMock(return_value={"count": len(cards), "cards": cards})
    return client


def _cards(n: int, hook: str = "MATCH_RESULT") -> list:
    return [{"id": f"card-{i}", "hook_type": hook} for i in range(n)]


def _varied_cards(n: int) -> list:
    hooks = ["MATCH_RESULT", "PLAYER_PROP", "ANTEPOST", "NEXT_GOAL", "BOTH_TEAMS"]
    return [
        {"id": f"card-{i}", "hook_type": hooks[i % len(hooks)]}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_low_card_count_triggers_alert():
    """Feed with 3 varied-hook cards (< 5) fires exactly a low-card WARN alert."""
    # Use varied hooks so hook-collapse does NOT also fire — isolates the low-card condition.
    cards = [
        {"id": "card-0", "hook_type": "MATCH_RESULT"},
        {"id": "card-1", "hook_type": "PLAYER_PROP"},
        {"id": "card-2", "hook_type": "ANTEPOST"},
    ]
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    low_card_calls = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls) == 1
    assert "3 cards" in low_card_calls[0][0]
    assert "WARN" in low_card_calls[0][0]


@pytest.mark.asyncio
async def test_low_card_count_dedup():
    """Low-card alert fires only once per day (varied hooks so only low-card fires)."""
    cards = [
        {"id": "card-0", "hook_type": "MATCH_RESULT"},
        {"id": "card-1", "hook_type": "PLAYER_PROP"},
        {"id": "card-2", "hook_type": "ANTEPOST"},
    ]
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    low_card_calls_first = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls_first) == 1

    await alerter._check_and_alert()
    low_card_calls_second = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls_second) == 1  # no new low-card alert


@pytest.mark.asyncio
async def test_hook_collapse_triggers_alert():
    """100% same hook type (10 cards, all MATCH_RESULT) fires a collapse alert."""
    client = _make_feed_client(_cards(10, hook="MATCH_RESULT"))
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    # May also fire low-card if count < 5, but here count = 10, so only collapse.
    collapse_alerts = [c for c in collector.calls if "collapse" in c[0].lower() or "diversity" in c[0].lower()]
    assert len(collapse_alerts) >= 1
    assert "MATCH_RESULT" in collapse_alerts[0][0]


@pytest.mark.asyncio
async def test_hook_collapse_dedup():
    """Hook-collapse alert fires only once per day."""
    client = _make_feed_client(_cards(10, hook="MATCH_RESULT"))
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    initial = len(collector.calls)
    await alerter._check_and_alert()
    assert len(collector.calls) == initial  # no new alerts


@pytest.mark.asyncio
async def test_healthy_feed_no_alert():
    """5+ cards with varied hooks produces no alert."""
    client = _make_feed_client(_varied_cards(10))
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_day_rollover_resets_dedup():
    """After a day rollover, the low-card alert can fire again.

    Use varied hooks so only the low-card condition fires (not hook collapse).
    """
    cards = [
        {"id": "card-0", "hook_type": "MATCH_RESULT"},
        {"id": "card-1", "hook_type": "PLAYER_PROP"},
        {"id": "card-2", "hook_type": "ANTEPOST"},
    ]
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    low_card_calls = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls) == 1

    # Simulate day rollover.
    alerter._current_day = "2099-01-01"
    alerter._fired = set()

    await alerter._check_and_alert()
    low_card_calls_after = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls_after) == 2


@pytest.mark.asyncio
async def test_respects_feed_snooze():
    """Snooze 'feed' suppresses all feed alerts."""
    import ops_bot.snooze as _snooze

    # 2 uniform-hook cards would trigger both conditions — snooze should suppress both.
    client = _make_feed_client(_cards(2, "MATCH_RESULT"))
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    with patch.object(_snooze, "is_snoozed", return_value=True):
        await alerter._check_and_alert()

    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_pulse_error_does_not_crash():
    """PulseError during poll is swallowed; no exception propagates."""
    client = MagicMock()
    client.feed = AsyncMock(side_effect=PulseError("unreachable"))
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    # Must not raise.
    await alerter._check_and_alert()
    assert len(collector.calls) == 0


@pytest.mark.asyncio
async def test_alert_includes_inline_keyboard():
    """Low-card alert carries FEED, RERUN, DISMISS buttons."""
    # Use varied hooks to isolate just the low-card alert.
    cards = [
        {"id": "card-0", "hook_type": "MATCH_RESULT"},
        {"id": "card-1", "hook_type": "PLAYER_PROP"},
    ]
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    low_card_calls = [c for c in collector.calls if "cards (<5)" in c[0]]
    assert len(low_card_calls) == 1
    _, markup = low_card_calls[0]
    assert markup is not None
    buttons = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "FEED" in buttons
    assert "RERUN" in buttons
    assert "DISMISS" in buttons


@pytest.mark.asyncio
async def test_exactly_80_pct_hook_no_alert():
    """Exactly 80% (not >80%) of one hook type does NOT trigger collapse."""
    # 8 out of 10 = 80% — threshold is STRICTLY greater than 80.
    cards = _cards(8, "MATCH_RESULT") + _cards(2, "PLAYER_PROP")
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    collapse = [c for c in collector.calls if "collapse" in c[0].lower() or "diversity" in c[0].lower()]
    assert len(collapse) == 0


@pytest.mark.asyncio
async def test_over_80_pct_hook_fires():
    """81% of one hook type DOES trigger collapse."""
    cards = _cards(9, "MATCH_RESULT") + _cards(1, "PLAYER_PROP")  # 90%
    client = _make_feed_client(cards)
    collector = AlertCollector()
    alerter = FeedAlerter(client, collector, poll_interval=0)

    await alerter._check_and_alert()
    collapse = [c for c in collector.calls if "collapse" in c[0].lower() or "diversity" in c[0].lower()]
    assert len(collapse) == 1
