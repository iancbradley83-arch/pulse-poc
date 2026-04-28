"""
Tests for AllowlistMiddleware.

Covers:
  - Allowlisted chat ID passes through to handler.
  - Non-allowlisted chat ID is rejected; handler not called.
  - Rejection sends "not authorised" reply (best-effort).
  - Rejection logs the unauthorised chat ID at INFO level.
  - Empty allowlist rejects all.
"""
import logging
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops_bot.auth import AllowlistMiddleware
from aiogram.types import Message, Chat, User


def _make_message(chat_id: int) -> MagicMock:
    """Build a minimal mock Message with a given chat.id."""
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = chat_id
    msg.answer = AsyncMock()
    return msg


async def _dummy_handler(event, data):
    return "called"


@pytest.mark.asyncio
async def test_allowlisted_chat_passes_through():
    mw = AllowlistMiddleware([123, 456])
    msg = _make_message(123)
    result = await mw(_dummy_handler, msg, {})
    assert result == "called"


@pytest.mark.asyncio
async def test_second_allowlisted_chat_passes_through():
    mw = AllowlistMiddleware([123, 456])
    msg = _make_message(456)
    result = await mw(_dummy_handler, msg, {})
    assert result == "called"


@pytest.mark.asyncio
async def test_non_allowlisted_chat_is_rejected():
    mw = AllowlistMiddleware([123, 456])
    msg = _make_message(999)
    result = await mw(_dummy_handler, msg, {})
    # Handler must NOT have been called.
    assert result is None


@pytest.mark.asyncio
async def test_rejection_sends_not_authorised_message():
    mw = AllowlistMiddleware([123, 456])
    msg = _make_message(999)
    await mw(_dummy_handler, msg, {})
    msg.answer.assert_awaited_once_with("not authorised. your chat id has been logged.")


@pytest.mark.asyncio
async def test_rejection_logs_chat_id(caplog):
    mw = AllowlistMiddleware([123, 456])
    msg = _make_message(999)
    with caplog.at_level(logging.INFO, logger="ops_bot.auth"):
        await mw(_dummy_handler, msg, {})
    assert any("unauthorised chat: 999" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_all():
    mw = AllowlistMiddleware([])
    msg = _make_message(123)
    result = await mw(_dummy_handler, msg, {})
    assert result is None


@pytest.mark.asyncio
async def test_empty_allowlist_logs_chat_id(caplog):
    mw = AllowlistMiddleware([])
    msg = _make_message(42)
    with caplog.at_level(logging.INFO, logger="ops_bot.auth"):
        await mw(_dummy_handler, msg, {})
    assert any("unauthorised chat: 42" in r.message for r in caplog.records)
