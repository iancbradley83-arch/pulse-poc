"""
Tests for PulseClient.cost_detail() — calls /admin/cost.json?detail=1 and
returns the enriched payload, raising PulseError on failure.
"""
import json
from unittest.mock import patch, AsyncMock

import httpx
import pytest

from ops_bot.pulse_client import PulseClient, PulseError


@pytest.mark.asyncio
async def test_cost_detail_returns_payload_on_200():
    payload = {
        "total_usd": 2.78,
        "total_calls": 56,
        "limit_usd": 3.0,
        "days": [{"date": "2026-04-28", "usd": 2.78, "calls": 56, "limit_usd": 3.0}],
        "by_kind": {"news_scout": {"usd": 0.57, "calls": 6}},
        "cards_in_feed_now": 11,
        "unique_cards_published_today": 13,
        "republish_events_today": None,
        "rewrite_cache_hits_today": None,
    }
    client = PulseClient("https://pulse.example", admin_user="u", admin_pass="p")
    fake = AsyncMock()
    resp = httpx.Response(200, json=payload, request=httpx.Request("GET", "https://pulse.example/admin/cost.json"))
    fake.return_value = resp
    with patch.object(client._client, "get", fake):
        result = await client.cost_detail()
    assert result == payload
    # Confirm the request used ?detail=1 and basic auth.
    called_kwargs = fake.call_args.kwargs
    assert called_kwargs["params"] == {"detail": 1}
    assert called_kwargs["auth"] == ("u", "p")
    await client.close()


@pytest.mark.asyncio
async def test_cost_detail_raises_pulseerror_on_timeout():
    client = PulseClient("https://pulse.example")
    fake = AsyncMock(side_effect=httpx.TimeoutException("boom"))
    with patch.object(client._client, "get", fake):
        with pytest.raises(PulseError) as exc_info:
            await client.cost_detail()
    assert "unreachable" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_cost_detail_raises_pulseerror_on_404():
    client = PulseClient("https://pulse.example")
    resp = httpx.Response(404, json={"detail": "Not Found"}, request=httpx.Request("GET", "https://pulse.example/admin/cost.json"))
    fake = AsyncMock(return_value=resp)
    with patch.object(client._client, "get", fake):
        with pytest.raises(PulseError) as exc_info:
            await client.cost_detail()
    assert "404" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_cost_detail_raises_pulseerror_on_non_dict_response():
    client = PulseClient("https://pulse.example")
    resp = httpx.Response(200, json=["not", "a", "dict"], request=httpx.Request("GET", "https://pulse.example/admin/cost.json"))
    fake = AsyncMock(return_value=resp)
    with patch.object(client._client, "get", fake):
        with pytest.raises(PulseError) as exc_info:
            await client.cost_detail()
    assert "not an object" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_cost_detail_no_admin_auth_when_credentials_unset():
    client = PulseClient("https://pulse.example")  # no admin creds
    payload = {"total_usd": 0.0, "total_calls": 0, "limit_usd": 3.0, "days": [], "by_kind": {}}
    resp = httpx.Response(200, json=payload, request=httpx.Request("GET", "https://pulse.example/admin/cost.json"))
    fake = AsyncMock(return_value=resp)
    with patch.object(client._client, "get", fake):
        await client.cost_detail()
    called_kwargs = fake.call_args.kwargs
    assert "auth" not in called_kwargs
    await client.close()
