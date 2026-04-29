"""
Tests for WidgetAlerter — frontend-render health probe.
"""
import pytest
from unittest.mock import patch, AsyncMock

import ops_bot.snooze as _snooze
from ops_bot.widget_alerter import (
    WidgetAlerter,
    evaluate_widget_response,
    EXPECTED_TITLE,
    EXPECTED_MOUNT,
)


# ---------------------------------------------------------------------------
# evaluate_widget_response — pure logic
# ---------------------------------------------------------------------------

def _ok_body() -> str:
    return (
        "<!doctype html>\n"
        "<html><head>\n"
        f"<title>{EXPECTED_TITLE}</title>\n"
        "</head><body>\n"
        f'{EXPECTED_MOUNT}>\n'
        + "<p>" + ("filler " * 100) + "</p>\n"  # ensure >500 bytes
        "</body></html>\n"
    )


def test_eval_returns_none_for_healthy_response():
    assert evaluate_widget_response(200, "text/html; charset=utf-8", _ok_body()) is None


def test_eval_flags_non_200():
    assert "http 502" in evaluate_widget_response(502, "text/html", _ok_body())
    assert "http 404" in evaluate_widget_response(404, "text/html", _ok_body())


def test_eval_flags_non_html_content_type():
    msg = evaluate_widget_response(200, "application/json", _ok_body())
    assert "non-html" in msg


def test_eval_flags_missing_title_sentinel():
    body = _ok_body().replace(EXPECTED_TITLE, "Some Other Title")
    msg = evaluate_widget_response(200, "text/html", body)
    assert "title" in msg


def test_eval_flags_missing_mount_sentinel():
    body = _ok_body().replace(EXPECTED_MOUNT, "<div id=\"different\"")
    msg = evaluate_widget_response(200, "text/html", body)
    assert "mount-point" in msg


def test_eval_flags_short_body():
    short = (
        "<!doctype html><html><head>"
        f"<title>{EXPECTED_TITLE}</title></head><body>"
        f"{EXPECTED_MOUNT}></body></html>"
    )
    msg = evaluate_widget_response(200, "text/html", short)
    assert "short" in msg


# ---------------------------------------------------------------------------
# WidgetAlerter — alert flow
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_snooze():
    _snooze._snoozed.clear()
    yield
    _snooze._snoozed.clear()


def _make_alerter(probe_results, sent):
    """Build an alerter whose _probe returns probe_results in sequence."""
    async def fake_send(text, kb):
        sent.append((text, kb))

    a = WidgetAlerter(
        widget_url="https://example.test/",
        send_fn=fake_send,
        poll_interval=1,
        fail_threshold=2,
    )

    seq = list(probe_results)

    async def fake_probe():
        return seq.pop(0)

    a._probe = fake_probe
    return a


@pytest.mark.asyncio
async def test_single_failure_does_not_alert():
    sent = []
    a = _make_alerter(["timeout"], sent)
    await a._check_and_alert()
    assert sent == []


@pytest.mark.asyncio
async def test_two_consecutive_failures_alert():
    sent = []
    a = _make_alerter(["timeout", "http 502"], sent)
    await a._check_and_alert()
    await a._check_and_alert()
    assert len(sent) == 1
    text, kb = sent[0]
    assert "CRITICAL" in text
    assert "Pulse widget broken" in text
    assert kb is not None  # has [PREVIEW] [STATUS] [DISMISS]


@pytest.mark.asyncio
async def test_alert_does_not_repeat_until_recovery():
    sent = []
    a = _make_alerter(["timeout", "http 502", "http 502", "http 502"], sent)
    for _ in range(4):
        await a._check_and_alert()
    # Single CRITICAL alert across the four polls.
    criticals = [t for t, _ in sent if "CRITICAL" in t]
    assert len(criticals) == 1


@pytest.mark.asyncio
async def test_recovery_pushes_notice():
    sent = []
    # Fail 2x, then recover.
    a = _make_alerter(["timeout", "timeout", None], sent)
    await a._check_and_alert()
    await a._check_and_alert()
    await a._check_and_alert()
    # 1 CRITICAL + 1 recovery.
    assert len(sent) == 2
    assert "CRITICAL" in sent[0][0]
    assert "recovered" in sent[1][0]
    # Recovery has no inline keyboard.
    assert sent[1][1] is None


@pytest.mark.asyncio
async def test_snooze_suppresses_alert_but_state_advances():
    sent = []
    a = _make_alerter(["timeout", "timeout", "timeout"], sent)
    _snooze.snooze("frontend", 300)
    await a._check_and_alert()
    await a._check_and_alert()
    await a._check_and_alert()
    # No alert sent during snooze.
    assert sent == []
    # But state has advanced — alert_fired flagged so we don't re-fire on un-snooze.
    assert a._alert_fired is True


@pytest.mark.asyncio
async def test_recovery_only_when_alert_was_fired():
    """No recovery message if we never alerted in the first place."""
    sent = []
    a = _make_alerter(["timeout", None], sent)
    await a._check_and_alert()
    await a._check_and_alert()
    assert sent == []
