"""
Tests for /logs — recent_logs severity filtering, empty result, Railway unreachable.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ops_bot.railway_client import RailwayClient, RailwayError
from ops_bot.formatting import format_logs


# ---------------------------------------------------------------------------
# recent_logs filtering
# ---------------------------------------------------------------------------

def _make_entry(severity, message="test message", ts="2026-04-28T16:50:50Z"):
    """
    Note: filter now parses the Python log level out of `message` text
    (Railway tags everything as severity=error from stderr — we ignore
    that field). Tests that want to be filtered IN must include the
    word WARNING / ERROR / CRITICAL / Traceback in the message body.
    """
    return {"timestamp": ts, "severity": severity, "message": message}


@pytest.mark.asyncio
async def test_recent_logs_filters_warn_and_error():
    """Only entries whose message body contains a Python WARN/ERROR level pass."""
    client = RailwayClient("dummy-token")

    deployment_data = {
        "deployments": {
            "edges": [{"node": {"id": "dep-123", "status": "SUCCESS", "createdAt": "2026-04-28T00:00:00Z", "meta": {}}}]
        }
    }
    # Messages must contain the level token internally; severity field is ignored.
    logs_data = {
        "deploymentLogs": [
            _make_entry("error", "INFO httpx: GET /health 200"),
            _make_entry("error", "WARNING pulse: high memory"),
            _make_entry("error", "ERROR db connection failed"),
            _make_entry("error", "DEBUG verbose debug"),
            _make_entry("error", "WARN another warn"),
            _make_entry("error", "Traceback (most recent call last):"),
        ]
    }

    async def mock_query(query, variables=None):
        if "deployments" in query:
            return deployment_data
        return logs_data

    with patch.object(client, "_query", side_effect=mock_query):
        result = await client.recent_logs("proj", "svc", n=20)

    severities = {e["severity"] for e in result}
    assert "INFO" not in severities  # INFO httpx line filtered out
    assert "DEBUG" not in severities
    assert "WARNING" in severities  # both WARNING and WARN are normalised to WARNING
    assert "ERROR" in severities  # both ERROR and Traceback normalise to ERROR


@pytest.mark.asyncio
async def test_recent_logs_returns_at_most_n():
    client = RailwayClient("dummy-token")

    deployment_data = {
        "deployments": {
            "edges": [{"node": {"id": "dep-123", "status": "SUCCESS", "createdAt": "2026-04-28T00:00:00Z", "meta": {}}}]
        }
    }
    # 10 WARNING entries (must include the level token in the message body)
    logs_data = {
        "deploymentLogs": [_make_entry("error", f"WARNING msg {i}") for i in range(10)]
    }

    async def mock_query(query, variables=None):
        if "deployments" in query:
            return deployment_data
        return logs_data

    with patch.object(client, "_query", side_effect=mock_query):
        result = await client.recent_logs("proj", "svc", n=3)

    assert len(result) <= 3


@pytest.mark.asyncio
async def test_recent_logs_empty_when_no_warn_error():
    client = RailwayClient("dummy-token")

    deployment_data = {
        "deployments": {
            "edges": [{"node": {"id": "dep-123", "status": "SUCCESS", "createdAt": "2026-04-28T00:00:00Z", "meta": {}}}]
        }
    }
    logs_data = {
        "deploymentLogs": [_make_entry("INFO", "all good"), _make_entry("DEBUG", "verbose")]
    }

    async def mock_query(query, variables=None):
        if "deployments" in query:
            return deployment_data
        return logs_data

    with patch.object(client, "_query", side_effect=mock_query):
        result = await client.recent_logs("proj", "svc", n=20)

    assert result == []


@pytest.mark.asyncio
async def test_recent_logs_railway_unreachable_raises():
    client = RailwayClient("dummy-token")

    async def mock_query(query, variables=None):
        raise RailwayError("unreachable")

    with patch.object(client, "_query", side_effect=mock_query):
        with pytest.raises(RailwayError):
            await client.recent_logs("proj", "svc", n=20)


# ---------------------------------------------------------------------------
# format_logs
# ---------------------------------------------------------------------------

def test_format_logs_empty():
    text = format_logs([], n=20)
    assert "no warn/error" in text


def test_format_logs_renders_entries():
    entries = [
        {"timestamp": "2026-04-28T16:50:50Z", "severity": "WARNING", "message": "alert fire failed"},
        {"timestamp": "2026-04-28T17:14:21Z", "severity": "ERROR", "message": "crash occurred"},
    ]
    text = format_logs(entries, n=20)
    assert "last 2 warn/error" in text
    assert "WARNING" in text
    assert "ERROR" in text
    assert "alert fire failed" in text
    assert "crash occurred" in text


def test_format_logs_timestamp_formatted():
    entries = [
        {"timestamp": "2026-04-28T16:50:50Z", "severity": "WARNING", "message": "test"},
    ]
    text = format_logs(entries, n=20)
    # New format: HH:MM:SS only (Railway timestamp date is implicit, the day
    # is the same as the latest deployment).
    assert "16:50:50" in text


def test_format_logs_strips_inner_python_prefix():
    """Compact rendering: Python log lines have their own timestamp+level prefix.
    The bot strips that to avoid duplicating it after the Railway HH:MM:SS + level."""
    entries = [
        {
            "timestamp": "2026-04-28T16:50:50Z",
            "severity": "WARNING",
            "message": "2026-04-28 16:50:50,041 WARNING app.services.cost_tracker: alert fire failed",
        }
    ]
    text = format_logs(entries, n=20)
    # The timestamp+level prefix is stripped. The substantive message is preserved.
    assert "alert fire failed" in text
    # The duplicated inner timestamp is not present in the rendered line.
    rendered_line = [l for l in text.split("\n") if "alert fire failed" in l][0]
    assert "2026-04-28" not in rendered_line
