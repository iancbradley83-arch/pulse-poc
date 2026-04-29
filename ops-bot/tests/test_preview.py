"""
Tests for preview.build_preview().

Covers:
  - Empty feed returns a sensible message (no crash).
  - Cards rendered with top 5 by relevance_score descending.
  - Deep_link HEAD status labels (200 ok, 404 fail, timeout).
"""
import asyncio
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.preview import build_preview, _card_preview_block, _head_status


def _make_pulse_client(cards: list) -> MagicMock:
    client = MagicMock()
    client.feed = AsyncMock(return_value={"count": len(cards), "cards": cards})
    return client


def _make_card(
    cid: str = "abc12345",
    hook: str = "injury",
    relevance: float = 4.0,
    deep_link: str = "https://example.com/slip",
    narrative: str = "Test narrative",
) -> dict:
    return {
        "id": cid,
        "hook_type": hook,
        "relevance_score": relevance,
        "deep_link": deep_link,
        "narrative_hook": narrative,
        "game": {
            "home_team": {"name": "Arsenal"},
            "away_team": {"name": "Chelsea"},
            "league": {"name": "Premier League"},
        },
    }


@pytest.mark.asyncio
async def test_empty_feed_returns_sensible_message():
    """Empty feed should return a non-crashing message."""
    client = _make_pulse_client([])
    result = await build_preview(client)
    assert "empty" in result.lower() or "feed" in result.lower()


@pytest.mark.asyncio
async def test_preview_renders_top_5_by_relevance():
    """Top 5 by relevance_score should be included; card outside top 5 excluded."""
    cards = [
        _make_card(cid=f"card{i:04d}", relevance=float(i)) for i in range(8)
    ]
    # card0007 has relevance 7.0 (top); card0000 has 0.0 (should be excluded).
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=200)):
        result = await build_preview(_make_pulse_client(cards))
    assert "card0007" in result
    assert "card0006" in result
    assert "card0000" not in result


@pytest.mark.asyncio
async def test_preview_2xx_deep_link_shows_200():
    """HEAD: 200 status should appear for working deep_link."""
    cards = [_make_card()]
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=200)):
        result = await build_preview(_make_pulse_client(cards))
    assert "200" in result


@pytest.mark.asyncio
async def test_preview_non_2xx_deep_link_shows_exclamation():
    """Non-2xx HEAD status should be flagged with (!)."""
    cards = [_make_card()]
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=404)):
        result = await build_preview(_make_pulse_client(cards))
    assert "404" in result
    assert "(!)" in result


@pytest.mark.asyncio
async def test_preview_timeout_deep_link_shows_timeout():
    """HEAD timeout (None return) should show 'timeout' label."""
    cards = [_make_card()]
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=None)):
        result = await build_preview(_make_pulse_client(cards))
    assert "timeout" in result


@pytest.mark.asyncio
async def test_preview_card_without_deep_link():
    """Card with no deep_link should not crash; shows '(none)'."""
    cards = [_make_card(deep_link="")]
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=200)):
        result = await build_preview(_make_pulse_client(cards))
    assert "none" in result


@pytest.mark.asyncio
async def test_preview_footer_shows_total_card_count():
    """Footer should include total cards in feed."""
    cards = [_make_card(cid=f"c{i:04d}", relevance=float(i)) for i in range(3)]
    with patch("ops_bot.preview._head_status", new=AsyncMock(return_value=200)):
        result = await build_preview(_make_pulse_client(cards))
    assert "3" in result


@pytest.mark.asyncio
async def test_preview_pulse_error_returns_error_message():
    """PulseError should return a user-readable error string, not crash."""
    from ops_bot.pulse_client import PulseError
    client = MagicMock()
    client.feed = AsyncMock(side_effect=PulseError("unreachable"))
    result = await build_preview(client)
    assert "could not fetch" in result.lower() or "unreachable" in result.lower()


def test_card_preview_block_formats_correctly():
    """_card_preview_block should include id, hook, relevance, and narrative."""
    card = _make_card(cid="abc12345", hook="injury", relevance=4.45)
    block = _card_preview_block(card, 200)
    assert "[abc12345]" in block
    assert "injury" in block
    assert "4.45" in block
    assert "200" in block
