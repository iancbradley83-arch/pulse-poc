"""Tests for PulseClient.cost_detail() — the ?detail=1 consumer.

Tests:
  1. cost_detail() calls the right URL with detail=1.
  2. cost_detail() normalises a well-formed response.
  3. cost_detail() handles a response missing by_kind gracefully.
  4. cost_detail() raises PulseError on network failure.
  5. cost_detail() raises PulseError on HTTP error status.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ops_bot.pulse_client import PulseClient, PulseError


# ── Helpers ────────────────────────────────────────────────────────────

def _make_client() -> PulseClient:
    return PulseClient(
        base_url="https://pulse.example.com",
        admin_user="admin",
        admin_pass="secret",
    )


def _mock_response(json_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


SAMPLE_DETAIL_RESPONSE = {
    "total_usd": 2.68,
    "total_calls": 52,
    "limit_usd": 3.0,
    "days": [{"date": "2026-04-28", "usd": 2.68, "calls": 52, "limit_usd": 3.0}],
    "by_kind": {
        "news_scout": {"usd": 0.57, "calls": 6},
        "rewrite": {"usd": 0.00, "calls": 0},
    },
    "cards_in_feed_now": 11,
    "unique_cards_published_today": 13,
    "republish_events_today": 63,
    "rewrite_cache_hits_today": 357,
}


# ── Tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cost_detail_calls_correct_url():
    client = _make_client()
    mock_resp = _mock_response(SAMPLE_DETAIL_RESPONSE)

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)) as mock_get:
        await client.cost_detail()

    call_kwargs = mock_get.call_args
    url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("url", "")
    if not url:
        # positional
        url = mock_get.call_args.args[0]
    assert url.endswith("/admin/cost.json"), f"Expected /admin/cost.json, got {url}"

    params = mock_get.call_args.kwargs.get("params", {})
    assert params.get("detail") == 1, f"Expected detail=1 in params, got {params}"


@pytest.mark.asyncio
async def test_cost_detail_normalises_response():
    client = _make_client()
    mock_resp = _mock_response(SAMPLE_DETAIL_RESPONSE)

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        result = await client.cost_detail()

    assert result["total_usd"] == pytest.approx(2.68)
    assert result["total_calls"] == 52
    assert result["limit_usd"] == pytest.approx(3.0)
    assert isinstance(result["by_kind"], dict)
    assert "news_scout" in result["by_kind"]
    assert result["cards_in_feed_now"] == 11
    assert result["unique_cards_published_today"] == 13
    assert result["republish_events_today"] == 63
    assert result["rewrite_cache_hits_today"] == 357


@pytest.mark.asyncio
async def test_cost_detail_missing_by_kind():
    """Missing by_kind key → empty dict in normalised result, no crash."""
    client = _make_client()
    data = {**SAMPLE_DETAIL_RESPONSE, "by_kind": None}
    mock_resp = _mock_response(data)

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        result = await client.cost_detail()

    assert result["by_kind"] == {}


@pytest.mark.asyncio
async def test_cost_detail_raises_on_timeout():
    import httpx
    client = _make_client()

    with patch.object(
        client._client,
        "get",
        new=AsyncMock(side_effect=httpx.TimeoutException("timeout")),
    ):
        with pytest.raises(PulseError, match="unreachable"):
            await client.cost_detail()


@pytest.mark.asyncio
async def test_cost_detail_raises_on_http_error():
    import httpx
    client = _make_client()
    mock_resp = _mock_response({}, status_code=500)

    with patch.object(client._client, "get", new=AsyncMock(return_value=mock_resp)):
        with pytest.raises(PulseError, match="http 500"):
            await client.cost_detail()
