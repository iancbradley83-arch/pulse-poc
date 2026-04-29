"""
Tests for ops_bot.playbook — fetch, parse, lookup.

The fetcher is mocked; we don't hit GitHub raw in tests.
"""
import pytest
from unittest.mock import patch, AsyncMock

import ops_bot.playbook as pb


_SAMPLE_DOC = """\
# Pulse operator playbook

Intro paragraph.

## Coverage matrix

| Scenario | OK |
|---|---|
| Cost | yes |

## Scenario: cost ladder alert

Symptom: bot pushed CRITICAL on cost.

First move: tap [BREAKDOWN] or send /breakdown.

## Scenario: Pulse health 5xx

Symptom: /health failing for 2+ minutes.

First move: tap [STATUS] then /logs 30.

## Learning loop

Process for capturing incidents.
"""


@pytest.fixture(autouse=True)
def _reset_cache():
    pb._cached_text = None
    pb._cached_at = 0.0
    yield


@pytest.mark.asyncio
async def test_list_topics_returns_all_section_headings():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        topics = await pb.list_topics()
    assert topics == [
        "Coverage matrix",
        "Scenario: cost ladder alert",
        "Scenario: Pulse health 5xx",
        "Learning loop",
    ]


@pytest.mark.asyncio
async def test_list_topics_returns_none_on_fetch_error():
    async def _raise():
        raise RuntimeError("github down")
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(side_effect=RuntimeError("github down"))):
        topics = await pb.list_topics()
    assert topics is None


@pytest.mark.asyncio
async def test_lookup_single_heading_match_returns_section():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        result = await pb.lookup("cost ladder")
    assert "Scenario: cost ladder alert" in result
    assert "tap [BREAKDOWN]" in result
    # Should not include other sections.
    assert "Pulse health" not in result


@pytest.mark.asyncio
async def test_lookup_case_insensitive():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        result_lower = await pb.lookup("cost ladder")
        result_upper = await pb.lookup("COST LADDER")
        result_mixed = await pb.lookup("Cost Ladder")
    assert result_lower == result_upper == result_mixed


@pytest.mark.asyncio
async def test_lookup_body_match_finds_section_via_keyword():
    """e.g. /playbook BREAKDOWN finds the cost section via body content."""
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        result = await pb.lookup("/breakdown")
    assert "Scenario: cost ladder alert" in result


@pytest.mark.asyncio
async def test_lookup_multiple_matches_lists_names():
    doc = _SAMPLE_DOC + "\n\n## Scenario: cost telemetry\n\nMore cost stuff.\n"
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=doc)):
        result = await pb.lookup("cost")
    assert "multiple matches" in result
    assert "Scenario: cost ladder alert" in result
    assert "Scenario: cost telemetry" in result


@pytest.mark.asyncio
async def test_lookup_no_match_lists_available():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        result = await pb.lookup("zomg-not-a-topic")
    assert "no section matching" in result
    assert "Coverage matrix" in result
    assert "Learning loop" in result


@pytest.mark.asyncio
async def test_lookup_returns_error_string_on_fetch_failure():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(side_effect=RuntimeError("playbook unavailable: timeout"))):
        result = await pb.lookup("cost")
    assert "playbook unavailable" in result


@pytest.mark.asyncio
async def test_lookup_strips_topic_whitespace():
    with patch.object(pb, "_fetch_playbook", new=AsyncMock(return_value=_SAMPLE_DOC)):
        result_padded = await pb.lookup("  cost ladder  ")
        result_clean = await pb.lookup("cost ladder")
    assert result_padded == result_clean
