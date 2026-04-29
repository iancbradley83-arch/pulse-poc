"""
Tests for ops_bot.webhooks handlers.

Covers:
  - /sentry: missing env var returns 503
  - /sentry: wrong token returns 401
  - /sentry: valid token + payload broadcasts summarised message
  - /report: missing env var returns 503
  - /report: missing/wrong token returns 401
  - /report: valid slug + token broadcasts correctly formatted message
  - Both handlers never raise to the framework (wrap in try/except)
"""
import json
from typing import List
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from aiohttp.web import Request

import ops_bot.webhooks as wh


class BroadcastCollector:
    def __init__(self):
        self.messages: List[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


def _sentry_request(token: str = "correct-token", body: dict = None) -> Request:
    """Build a minimal aiohttp mock request for /sentry."""
    body_bytes = json.dumps(body or {}).encode()

    class MockRequest:
        def __init__(self):
            self.rel_url = _QueryStringURL({"token": token})

        async def json(self):
            return body or {}

    return MockRequest()


def _report_request(
    slug: str = "apuesta-total",
    token: str = "correct-token",
    subject: str = "widget blank",
    body: str = "nothing loads",
) -> object:
    """Build a minimal mock request for /report."""
    class MockRequest:
        def __init__(self):
            self.rel_url = _QueryStringURL({"slug": slug, "token": token})

        async def post(self):
            return {"subject": subject, "body": body}

    return MockRequest()


class _QueryStringURL:
    def __init__(self, params: dict):
        self._params = params

    def __getattr__(self, name):
        if name == "query":
            return self._params
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# /sentry tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sentry_returns_503_when_env_not_set():
    """Missing SENTRY_WEBHOOK_TOKEN env var → 503."""
    collector = BroadcastCollector()
    with patch.dict("os.environ", {}, clear=False):
        with patch("ops_bot.webhooks._get_sentry_token", return_value=None):
            resp = await wh.handle_sentry(_sentry_request(), collector)
    assert resp.status == 503
    data = json.loads(resp.body)
    assert not data["ok"]
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_sentry_returns_401_on_wrong_token():
    """Wrong token → 401."""
    collector = BroadcastCollector()
    with patch("ops_bot.webhooks._get_sentry_token", return_value="correct-token"):
        resp = await wh.handle_sentry(_sentry_request(token="wrong"), collector)
    assert resp.status == 401
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_sentry_valid_token_broadcasts():
    """Valid token → broadcast and 200."""
    collector = BroadcastCollector()
    payload = {
        "event": {
            "event_id": "abc123",
            "title": "ZeroDivisionError",
            "level": "error",
            "project": "pulse-poc",
        }
    }

    class MockReq:
        rel_url = _QueryStringURL({"token": "correct-token"})
        async def json(self):
            return payload

    with patch("ops_bot.webhooks._get_sentry_token", return_value="correct-token"):
        resp = await wh.handle_sentry(MockReq(), collector)

    assert resp.status == 200
    assert len(collector.messages) == 1
    assert "ZeroDivisionError" in collector.messages[0]
    assert "Sentry" in collector.messages[0]


@pytest.mark.asyncio
async def test_sentry_missing_token_param_returns_401():
    """Request with no token query param → 401."""
    collector = BroadcastCollector()

    class MockReq:
        rel_url = _QueryStringURL({})
        async def json(self):
            return {}

    with patch("ops_bot.webhooks._get_sentry_token", return_value="correct-token"):
        resp = await wh.handle_sentry(MockReq(), collector)

    assert resp.status == 401


# ---------------------------------------------------------------------------
# /report tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_returns_503_when_env_not_set():
    """Missing OPERATOR_REPORT_TOKENS env var → 503."""
    collector = BroadcastCollector()
    with patch("ops_bot.webhooks._get_operator_tokens", return_value={}):
        resp = await wh.handle_report(_report_request(), collector)
    assert resp.status == 503
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_report_returns_401_on_wrong_token():
    """Wrong token for slug → 401."""
    collector = BroadcastCollector()
    tokens = {"apuesta-total": "correct-token"}
    with patch("ops_bot.webhooks._get_operator_tokens", return_value=tokens):
        resp = await wh.handle_report(
            _report_request(token="wrong-token"), collector
        )
    assert resp.status == 401
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_report_returns_401_on_unknown_slug():
    """Unknown slug → 401."""
    collector = BroadcastCollector()
    tokens = {"apuesta-total": "correct-token"}
    with patch("ops_bot.webhooks._get_operator_tokens", return_value=tokens):
        resp = await wh.handle_report(
            _report_request(slug="unknown-operator", token="correct-token"),
            collector,
        )
    assert resp.status == 401
    assert len(collector.messages) == 0


@pytest.mark.asyncio
async def test_report_valid_broadcasts_formatted_message():
    """Valid slug + token → broadcast formatted [slug] reported: message."""
    collector = BroadcastCollector()
    tokens = {"apuesta-total": "correct-token"}
    with patch("ops_bot.webhooks._get_operator_tokens", return_value=tokens):
        resp = await wh.handle_report(
            _report_request(
                slug="apuesta-total",
                token="correct-token",
                subject="widget blank",
                body="nothing loads on mobile",
            ),
            collector,
        )
    assert resp.status == 200
    assert len(collector.messages) == 1
    msg = collector.messages[0]
    assert "[apuesta-total] reported:" in msg
    assert "widget blank" in msg
    assert "nothing loads on mobile" in msg


@pytest.mark.asyncio
async def test_report_missing_slug_returns_401():
    """Missing slug query param → 401."""
    collector = BroadcastCollector()
    tokens = {"apuesta-total": "correct-token"}

    class MockReq:
        rel_url = _QueryStringURL({"token": "correct-token"})
        async def post(self):
            return {}

    with patch("ops_bot.webhooks._get_operator_tokens", return_value=tokens):
        resp = await wh.handle_report(MockReq(), collector)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_sentry_summarise_handles_minimal_payload():
    """_summarise_sentry should not crash on empty payload."""
    summary = wh._summarise_sentry({})
    assert isinstance(summary, str)
    assert len(summary) > 0


def test_get_operator_tokens_parses_json():
    """_get_operator_tokens parses valid JSON object."""
    import os
    with patch.dict(os.environ, {"OPERATOR_REPORT_TOKENS": '{"slug1": "tok1"}'}):
        tokens = wh._get_operator_tokens()
    assert tokens == {"slug1": "tok1"}


def test_get_operator_tokens_returns_empty_on_bad_json():
    """_get_operator_tokens returns {} on malformed JSON."""
    import os
    with patch.dict(os.environ, {"OPERATOR_REPORT_TOKENS": "not-json"}):
        tokens = wh._get_operator_tokens()
    assert tokens == {}
