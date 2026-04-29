"""
Tests for ops_bot.incidents module.

Covers:
  - start without title raises ValueError
  - note before start raises NoOpenIncident
  - full lifecycle: start → note → close
  - close without open incident raises NoOpenIncident
  - get_open returns None when no incident open
  - render_markdown produces correct headings and timeline
  - slug derivation from title + date
  - multiple chats are independent
"""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone

import ops_bot.incidents as inc


def _clear_all():
    """Remove all open incidents between tests."""
    inc._open.clear()


@pytest.fixture(autouse=True)
def clean_state():
    _clear_all()
    yield
    _clear_all()


def test_start_without_title_raises():
    with pytest.raises(ValueError):
        inc.start(chat_id=1, title="")


def test_start_whitespace_title_raises():
    with pytest.raises(ValueError):
        inc.start(chat_id=1, title="   ")


def test_note_before_start_raises():
    with pytest.raises(inc.NoOpenIncident):
        inc.note(chat_id=1, text="some note")


def test_close_before_start_raises():
    with pytest.raises(inc.NoOpenIncident):
        inc.close(chat_id=1)


def test_full_lifecycle():
    """start → note → close returns a closed IncidentLog with correct fields."""
    slug = inc.start(chat_id=42, title="Cost spike")
    assert "cost-spike" in slug
    assert inc.get_open(42) is not None

    inc.note(chat_id=42, text="cost crossed $2")
    log_open = inc.get_open(42)
    assert any(e.kind == "note" for e in log_open.timeline)

    log = inc.close(chat_id=42)
    assert log.closed_at is not None
    assert inc.get_open(42) is None

    # Timeline should contain: start, note, close entries.
    kinds = [e.kind for e in log.timeline]
    assert "start" in kinds
    assert "note" in kinds
    assert "close" in kinds


def test_get_open_returns_none_when_not_started():
    assert inc.get_open(chat_id=99) is None


def test_multiple_chats_independent():
    """Two different chat IDs each get their own incident."""
    slug1 = inc.start(chat_id=1, title="Chat 1 incident")
    slug2 = inc.start(chat_id=2, title="Chat 2 incident")
    assert slug1 != slug2
    log1 = inc.close(chat_id=1)
    assert log1.chat_id == 1
    # Chat 2 should still have an open incident.
    assert inc.get_open(2) is not None


def test_render_markdown_structure():
    """render_markdown should produce correct markdown headings."""
    slug = inc.start(chat_id=1, title="Test incident")
    inc.note(chat_id=1, text="note about something")
    log = inc.close(chat_id=1)

    md = inc.render_markdown(log)

    assert "# Test incident" in md
    assert "## Timeline" in md
    assert "## Resolution" in md
    assert "note: note about something" in md
    assert "start:" in md
    assert "close" in md


def test_render_markdown_duration():
    """render_markdown should include a Duration line."""
    slug = inc.start(chat_id=1, title="Duration test")
    log = inc.close(chat_id=1)
    md = inc.render_markdown(log)
    assert "Duration:" in md


def test_slug_includes_date():
    """Slug should begin with a YYYY-MM-DD date."""
    import re
    slug = inc.start(chat_id=1, title="Something happened")
    assert re.match(r"^\d{4}-\d{2}-\d{2}-", slug)


def test_append_alert_no_op_when_no_incident():
    """append_alert should not raise when no incident is open."""
    inc.append_alert(chat_id=999, text="some alert")  # should not raise


def test_append_alert_adds_to_timeline():
    """append_alert during an open incident should add an alert entry."""
    inc.start(chat_id=1, title="Alert capture test")
    inc.append_alert(chat_id=1, text="deploy FAILED commit abc1234")
    log_open = inc.get_open(1)
    assert any(e.kind == "alert" for e in log_open.timeline)
    log = inc.close(chat_id=1)
    kinds = [e.kind for e in log.timeline]
    assert "alert" in kinds
