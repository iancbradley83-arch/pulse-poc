"""
Tests for /embed formatting and PulseClient.embeds() behaviour.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from ops_bot.formatting import format_embed
from ops_bot.pulse_client import PulseClient, PulseError


# ---------------------------------------------------------------------------
# format_embed rendering
# ---------------------------------------------------------------------------

def _embed(last_served=None, active=True):
    e = {
        "slug": "apuesta-total",
        "token": "y8t7fcGtazIgYVZBp-mZcPMG0mK-pIF0eXyLTRoI9D0",
        "allowed_origins": ["*.apuestatotal.com.pe", "localhost"],
        "theme_overrides": {"primary": "#ff0000"},
        "created_at": "2026-04-28T12:03:58+00:00",
        "active": active,
    }
    if last_served:
        e["last_served_at"] = last_served
    return e


def test_embed_renders_slug():
    text = format_embed(_embed())
    assert "apuesta-total" in text


def test_embed_token_scrubbed():
    embed = _embed()
    token = embed["token"]
    text = format_embed(embed)
    # First 8 chars present, rest replaced with ***
    assert token[:8] in text
    assert token[8:] not in text
    assert "***" in text


def test_embed_domains_listed():
    text = format_embed(_embed())
    assert "*.apuestatotal.com.pe" in text
    assert "localhost" in text


def test_embed_theme_override_count():
    text = format_embed(_embed())
    assert "Theme overrides: 1" in text


def test_embed_no_theme_overrides():
    e = _embed()
    e["theme_overrides"] = {}
    text = format_embed(e)
    assert "Theme overrides: 0" in text


def test_embed_created_at_shown():
    text = format_embed(_embed())
    assert "Created:" in text


def test_embed_last_served_shown_when_present():
    text = format_embed(_embed(last_served="2026-04-28T15:00:00+00:00"))
    assert "Last served:" in text


def test_embed_last_served_absent_when_not_set():
    text = format_embed(_embed(last_served=None))
    assert "Last served:" not in text


def test_embed_active_yes():
    text = format_embed(_embed(active=True))
    assert "Active: yes" in text


def test_embed_active_no():
    text = format_embed(_embed(active=False))
    assert "Active: no" in text


# ---------------------------------------------------------------------------
# PulseClient.embeds() — 404 path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embeds_404_raises_pulse_error():
    client = PulseClient("https://example.com")
    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        with pytest.raises(PulseError) as exc_info:
            await client.embeds()
        assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_embeds_ok_returns_data():
    client = PulseClient("https://example.com")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"embeds": [{"slug": "test"}]})

    with patch.object(client._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        result = await client.embeds()
    assert result["embeds"][0]["slug"] == "test"


@pytest.mark.asyncio
async def test_embeds_timeout_raises_pulse_error():
    client = PulseClient("https://example.com")
    with patch.object(
        client._client, "get", new_callable=AsyncMock,
        side_effect=httpx.TimeoutException("timeout")
    ):
        with pytest.raises(PulseError) as exc_info:
            await client.embeds()
        assert "unreachable" in str(exc_info.value)
