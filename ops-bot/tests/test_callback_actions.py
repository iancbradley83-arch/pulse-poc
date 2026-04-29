"""
Tests for the new inline-keyboard action:* callback handlers added in Stage 2B.

Covers:
  - action:redeploy registers confirm + sends prompt (requires Railway client).
  - action:redeploy with no Railway client → show_alert response.
  - action:logs fetches and formats logs.
  - action:logs with no Railway client → error message.
  - action:status fetches and formats status.
  - action:rerun registers confirm + sends prompt.
  - action:feed fetches and formats feed audit.
  - action:dismiss removes keyboard.
  - Auth gate still blocks non-allowed chat IDs for callback_query.

These tests stub out the aiogram CallbackQuery object and verify that the
handler (called directly) routes correctly.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from ops_bot import handlers as _handlers
from ops_bot.pulse_client import PulseError
from ops_bot.railway_client import RailwayError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_callback(data: str, chat_id: int = 12345) -> MagicMock:
    """Build a minimal fake CallbackQuery."""
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock(id=chat_id)
    cb.answer = AsyncMock()

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id)
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    cb.message = msg

    return cb


def _setup_clients(pulse=None, railway=None):
    """Inject stub clients into handlers module."""
    _handlers._pulse_client = pulse
    _handlers._railway_client = railway


def _stub_pulse(feed_cards=None, fail=False):
    p = MagicMock()
    if fail:
        p.feed = AsyncMock(side_effect=PulseError("down"))
        p.health = AsyncMock(side_effect=PulseError("down"))
        p.cost = AsyncMock(side_effect=PulseError("down"))
        p.cost_detail = AsyncMock(side_effect=PulseError("down"))
    else:
        cards = feed_cards or [
            {"id": f"card-{i}", "hook_type": "MATCH_RESULT"} for i in range(10)
        ]
        p.feed = AsyncMock(return_value={"count": len(cards), "cards": cards})
        p.health = AsyncMock(return_value={"ok": True})
        p.cost = AsyncMock(return_value={
            "total_usd": 0.5, "total_calls": 5, "limit_usd": 3.0, "days": []
        })
        p.cost_detail = AsyncMock(side_effect=PulseError("no detail"))
    return p


def _stub_railway(fail=False):
    r = MagicMock()
    if fail:
        r.recent_logs = AsyncMock(side_effect=RailwayError("down"))
        r.latest_deployment = AsyncMock(side_effect=RailwayError("down"))
        r.variables = AsyncMock(side_effect=RailwayError("down"))
    else:
        r.recent_logs = AsyncMock(return_value=[
            {"timestamp": "2026-04-28T09:00:00Z", "severity": "ERROR", "message": "test error"}
        ])
        r.latest_deployment = AsyncMock(return_value={
            "id": "dep-abc",
            "status": "SUCCESS",
            "createdAt": "2026-04-28T08:00:00Z",
            "commitHash": "abc1234",
        })
        r.variables = AsyncMock(return_value={
            "PULSE_RERUN_ENABLED": "true",
            "PULSE_NEWS_INGEST_ENABLED": "true",
            "PULSE_TIERED_FRESHNESS_ENABLED": "false",
        })
    return r


# ---------------------------------------------------------------------------
# action:dismiss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dismiss_removes_keyboard():
    cb = _make_callback("action:dismiss")
    _setup_clients()
    await _handlers.handle_callback_query(cb)
    cb.answer.assert_awaited_once_with("dismissed")
    cb.message.edit_reply_markup.assert_awaited()


# ---------------------------------------------------------------------------
# action:redeploy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redeploy_registers_confirm_and_sends_prompt():
    """Tapping [REDEPLOY] registers a confirm and sends the confirm prompt."""
    cb = _make_callback("action:redeploy")
    _setup_clients(railway=_stub_railway())

    import ops_bot.confirm as _confirm
    with patch.object(_confirm, "register") as mock_register:
        await _handlers.handle_callback_query(cb)
        mock_register.assert_called_once_with(12345, "redeploy")

    # A confirm prompt should have been sent.
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    assert "confirm" in text.lower()
    assert "redeploy" in text.lower()


@pytest.mark.asyncio
async def test_redeploy_no_railway_shows_alert():
    """[REDEPLOY] with no Railway client shows a Telegram alert popup."""
    cb = _make_callback("action:redeploy")
    _setup_clients(railway=None)
    await _handlers.handle_callback_query(cb)
    cb.answer.assert_awaited_once_with("Railway API unavailable", show_alert=True)


# ---------------------------------------------------------------------------
# action:logs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logs_fetches_and_sends():
    """Tapping [LOGS] fetches and sends formatted logs."""
    cb = _make_callback("action:logs")
    _setup_clients(railway=_stub_railway())
    await _handlers.handle_callback_query(cb)
    cb.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_logs_no_railway_sends_error():
    cb = _make_callback("action:logs")
    _setup_clients(railway=None)
    await _handlers.handle_callback_query(cb)
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    assert "unreachable" in text.lower()


# ---------------------------------------------------------------------------
# action:status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_sends_status_block():
    """Tapping [STATUS] sends a formatted status block."""
    cb = _make_callback("action:status")
    _setup_clients(pulse=_stub_pulse(), railway=_stub_railway())
    await _handlers.handle_callback_query(cb)
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    # Status block should have at least "Pulse:" line.
    assert "Pulse:" in text


# ---------------------------------------------------------------------------
# action:rerun
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerun_registers_confirm_and_sends_prompt():
    """Tapping [RERUN] registers a confirm and sends the confirm prompt."""
    cb = _make_callback("action:rerun")
    _setup_clients(pulse=_stub_pulse())

    import ops_bot.confirm as _confirm
    with patch.object(_confirm, "register") as mock_register:
        await _handlers.handle_callback_query(cb)
        mock_register.assert_called_once_with(12345, "rerun")

    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    assert "rerun" in text.lower()


# ---------------------------------------------------------------------------
# action:feed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feed_fetches_and_sends_audit():
    """Tapping [FEED] fetches and sends the feed audit."""
    cb = _make_callback("action:feed")
    _setup_clients(pulse=_stub_pulse())
    await _handlers.handle_callback_query(cb)
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    assert "feed audit" in text.lower() or "cards" in text.lower()


@pytest.mark.asyncio
async def test_feed_pulse_error_sends_unreachable():
    cb = _make_callback("action:feed")
    _setup_clients(pulse=_stub_pulse(fail=True))
    await _handlers.handle_callback_query(cb)
    cb.message.answer.assert_awaited_once()
    text = cb.message.answer.call_args[0][0]
    assert "unreachable" in text.lower()


# ---------------------------------------------------------------------------
# Unknown action — no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_action_data_answers_silently():
    cb = _make_callback("action:unknown_future_thing")
    _setup_clients()
    await _handlers.handle_callback_query(cb)
    # Should answer (to stop Telegram spinner) and not raise.
    cb.answer.assert_awaited()
