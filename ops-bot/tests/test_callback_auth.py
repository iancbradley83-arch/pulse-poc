"""
Tests for AllowlistMiddleware applied to CallbackQuery updates.

Covers:
  - Allowlisted callback_query passes through to handler.
  - Non-allowlisted callback_query is rejected; handler not called.
  - Rejection answers the callback silently ("not authorised").
  - Rejection logs the chat ID at INFO.
  - Chat ID extracted from callback.message.chat.id (primary path).
  - Chat ID fallback to from_user.id when message is None (inline mode).
"""
import logging
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from aiogram.types import CallbackQuery, Chat, Message, User
from ops_bot.auth import AllowlistMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_callback(chat_id: int) -> MagicMock:
    """Build a mock CallbackQuery where the message.chat.id is chat_id."""
    cq = MagicMock(spec=CallbackQuery)
    cq.data = "action:pause"

    msg = MagicMock(spec=Message)
    chat = MagicMock(spec=Chat)
    chat.id = chat_id
    msg.chat = chat
    cq.message = msg

    user = MagicMock(spec=User)
    user.id = chat_id
    cq.from_user = user

    cq.answer = AsyncMock()
    return cq


def _make_callback_no_message(user_id: int) -> MagicMock:
    """Build a mock CallbackQuery with no .message (inline-mode fallback)."""
    cq = MagicMock(spec=CallbackQuery)
    cq.data = "action:dismiss"
    cq.message = None

    user = MagicMock(spec=User)
    user.id = user_id
    cq.from_user = user

    cq.answer = AsyncMock()
    return cq


async def _dummy_handler(event: Any, data: Dict[str, Any]) -> str:
    return "called"


# ---------------------------------------------------------------------------
# Allowlisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowlisted_callback_passes_through():
    mw = AllowlistMiddleware([123, 456])
    cq = _make_callback(123)
    result = await mw(_dummy_handler, cq, {})
    assert result == "called"


@pytest.mark.asyncio
async def test_second_allowlisted_callback_passes_through():
    mw = AllowlistMiddleware([123, 456])
    cq = _make_callback(456)
    result = await mw(_dummy_handler, cq, {})
    assert result == "called"


# ---------------------------------------------------------------------------
# Rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_allowlisted_callback_is_rejected():
    mw = AllowlistMiddleware([123])
    cq = _make_callback(999)
    result = await mw(_dummy_handler, cq, {})
    assert result is None


@pytest.mark.asyncio
async def test_rejected_callback_answers_silently():
    mw = AllowlistMiddleware([123])
    cq = _make_callback(999)
    await mw(_dummy_handler, cq, {})
    cq.answer.assert_awaited_once_with("not authorised")


@pytest.mark.asyncio
async def test_rejected_callback_logs_chat_id(caplog):
    mw = AllowlistMiddleware([123])
    cq = _make_callback(777)
    with caplog.at_level(logging.INFO, logger="ops_bot.auth"):
        await mw(_dummy_handler, cq, {})
    assert any("unauthorised chat: 777" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_allowlist_rejects_callback():
    mw = AllowlistMiddleware([])
    cq = _make_callback(123)
    result = await mw(_dummy_handler, cq, {})
    assert result is None


# ---------------------------------------------------------------------------
# Inline-mode fallback (no .message)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_no_message_uses_from_user_id_allowed():
    mw = AllowlistMiddleware([42])
    cq = _make_callback_no_message(42)
    result = await mw(_dummy_handler, cq, {})
    assert result == "called"


@pytest.mark.asyncio
async def test_callback_no_message_uses_from_user_id_rejected():
    mw = AllowlistMiddleware([42])
    cq = _make_callback_no_message(99)
    result = await mw(_dummy_handler, cq, {})
    assert result is None


# ---------------------------------------------------------------------------
# chat.id is used (not message.id)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_uses_chat_id_not_message_id():
    """
    Prior bug: the generic fallback walked attr="message" and returned
    message.id instead of message.chat.id. This test ensures the correct
    path is taken for CallbackQuery.
    """
    mw = AllowlistMiddleware([500])
    cq = _make_callback(500)
    # message.id is different from chat.id.
    cq.message.id = 999999
    result = await mw(_dummy_handler, cq, {})
    assert result == "called"
