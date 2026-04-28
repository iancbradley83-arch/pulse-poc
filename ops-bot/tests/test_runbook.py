"""
Tests for runbook.py — section parsing, substring match, multiple matches,
no match, and fetch failure.
"""
import pytest
from unittest.mock import patch, AsyncMock

import ops_bot.runbook as runbook_mod


SAMPLE_RUNBOOK = """\
# pulse-poc Runbook

Intro paragraph.

## Constants

Some constants here.

## Common tasks

### Verify a deploy

Run curl.

## Incident playbook

### Site returns 502

Check logs.
"""


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the module-level cache before each test."""
    runbook_mod._cached_text = None
    runbook_mod._cached_at = 0.0
    yield
    runbook_mod._cached_text = None
    runbook_mod._cached_at = 0.0


async def _fake_fetch():
    return SAMPLE_RUNBOOK


@pytest.mark.asyncio
async def test_single_match_returns_section():
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=SAMPLE_RUNBOOK)):
        result = await runbook_mod.lookup("constants")
    assert "## Constants" in result
    assert "Some constants here" in result


@pytest.mark.asyncio
async def test_case_insensitive_match():
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=SAMPLE_RUNBOOK)):
        result = await runbook_mod.lookup("CONSTANTS")
    assert "## Constants" in result


@pytest.mark.asyncio
async def test_substring_match():
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=SAMPLE_RUNBOOK)):
        result = await runbook_mod.lookup("502")
    assert "502" in result
    assert "Check logs" in result


@pytest.mark.asyncio
async def test_multiple_matches_lists_section_names():
    # "task" matches "Common tasks"; won't match others exactly
    # Use a topic matching multiple headings
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=SAMPLE_RUNBOOK)):
        result = await runbook_mod.lookup("play")  # matches "Incident playbook"
    # Only one match — should return content, not list
    assert "## Incident playbook" in result

    # Now test a topic matching > 1 heading
    multi_runbook = SAMPLE_RUNBOOK + "\n## playbook extras\n\nMore info.\n"
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=multi_runbook)):
        result = await runbook_mod.lookup("playbook")
    assert "multiple matches" in result
    assert "Incident playbook" in result
    assert "playbook extras" in result


@pytest.mark.asyncio
async def test_no_match_lists_available_topics():
    with patch.object(runbook_mod, "_fetch_runbook", new=AsyncMock(return_value=SAMPLE_RUNBOOK)):
        result = await runbook_mod.lookup("nonexistent-topic-xyz")
    assert "no section matching" in result
    assert "Constants" in result
    assert "Common tasks" in result
    assert "Incident playbook" in result


@pytest.mark.asyncio
async def test_fetch_failure_returns_error_message():
    with patch.object(
        runbook_mod, "_fetch_runbook",
        new=AsyncMock(side_effect=RuntimeError("runbook unavailable: connection refused"))
    ):
        result = await runbook_mod.lookup("anything")
    assert "unavailable" in result


@pytest.mark.asyncio
async def test_parse_sections_handles_empty():
    sections = runbook_mod._parse_sections("")
    assert sections == []


@pytest.mark.asyncio
async def test_parse_sections_returns_correct_headings():
    sections = runbook_mod._parse_sections(SAMPLE_RUNBOOK)
    headings = [h for h, _ in sections]
    assert "Constants" in headings
    assert "Common tasks" in headings
    assert "Incident playbook" in headings
