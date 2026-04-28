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
    return {"timestamp": ts, "severity": severity, "message": message}


@pytest.mark.asyncio
async def test_recent_logs_filters_warn_and_error():
    """Only WARNING and ERROR entries are returned; INFO/DEBUG are dropped."""
    client = RailwayClient("dummy-token")

    deployment_data = {
        "deployments": {
            "edges": [{"node": {"id": "dep-123", "status": "SUCCESS", "createdAt": "2026-04-28T00:00:00Z", "meta": {}}}]
        }
    }
    logs_data = {
        "deploymentLogs": [
            _make_entry("INFO", "app started"),
            _make_entry("WARNING", "high memory"),
            _make_entry("ERROR", "db connection failed"),
            _make_entry("DEBUG", "verbose debug"),
            _make_entry("WARN", "another warn"),
        ]
    }

    call_count = 0

    async def mock_query(query, variables=None):
        nonlocal call_count
        call_count += 1
        if "deployments" in query:
            return deployment_data
        return logs_data

    with patch.object(client, "_query", side_effect=mock_query):
        result = await client.recent_logs("proj", "svc", n=20)

    severities = {e["severity"] for e in result}
    assert "INFO" not in severities
    assert "DEBUG" not in severities
    assert "WARNING" in severities or "WARN" in severities
    assert "ERROR" in severities


@pytest.mark.asyncio
async def test_recent_logs_returns_at_most_n():
    client = RailwayClient("dummy-token")

    deployment_data = {
        "deployments": {
            "edges": [{"node": {"id": "dep-123", "status": "SUCCESS", "createdAt": "2026-04-28T00:00:00Z", "meta": {}}}]
        }
    }
    # 10 WARN entries
    logs_data = {
        "deploymentLogs": [_make_entry("WARNING", f"msg {i}") for i in range(10)]
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
    assert "2026-04-28 16:50:50" in text
