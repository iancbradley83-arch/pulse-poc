"""
Tests for format_breakdown — renders today's spend split by kind plus
card counts and per-card KPIs from the /admin/cost.json?detail=1 payload.
"""
from ops_bot.formatting import format_breakdown


def _detail(**overrides):
    base = {
        "total_usd": 2.6806,
        "total_calls": 52,
        "limit_usd": 3.0,
        "days": [
            {"date": "2026-04-28", "usd": 2.6806, "calls": 52, "limit_usd": 3.0},
        ],
        "by_kind": {
            "news_scout": {"usd": 0.5663, "calls": 6},
            "narrative_generator": {"usd": 0.10, "calls": 4},
            "rewrite": {"usd": 0.0, "calls": 0},
        },
        "cards_in_feed_now": 11,
        "unique_cards_published_today": 13,
        "republish_events_today": 63,
        "rewrite_cache_hits_today": 357,
    }
    base.update(overrides)
    return base


def test_breakdown_full_payload_renders_all_sections():
    text = format_breakdown(_detail())
    assert "Daily breakdown — 2026-04-28" in text
    assert "Total: $2.68 / $3.00  (89%) — 52 calls" in text
    assert "By kind:" in text
    assert "news_scout" in text
    assert "$0.5663" in text
    assert "11 in feed" in text
    assert "13 unique today" in text
    assert "63 publish events" in text
    assert "$/card in feed:      $0.2437" in text
    assert "$/unique card today: $0.2062" in text
    assert "Rewrite cache hits today: 357" in text


def test_breakdown_by_kind_sorted_descending_by_usd():
    text = format_breakdown(_detail())
    lines = text.split("\n")
    kind_lines = [l for l in lines if l.startswith("  ") and "$" in l]
    # news_scout ($0.5663) should appear before narrative_generator ($0.10)
    # which should appear before rewrite ($0.00)
    assert kind_lines[0].lstrip().startswith("news_scout")
    assert kind_lines[1].lstrip().startswith("narrative_generator")
    assert kind_lines[2].lstrip().startswith("rewrite")


def test_breakdown_empty_by_kind_shows_warmup_message():
    text = format_breakdown(_detail(by_kind={}))
    assert "By kind: (no per-kind data yet" in text


def test_breakdown_null_by_kind_shows_warmup_message():
    text = format_breakdown(_detail(by_kind=None))
    assert "By kind: (no per-kind data yet" in text


def test_breakdown_null_enrichment_fields_render_gracefully():
    text = format_breakdown(_detail(
        cards_in_feed_now=None,
        unique_cards_published_today=None,
        republish_events_today=None,
        rewrite_cache_hits_today=None,
    ))
    # Total + by_kind still render; card section is skipped when all null.
    assert "Total:" in text
    assert "By kind:" in text
    assert "in feed" not in text
    assert "$/card" not in text
    assert "Rewrite cache" not in text


def test_breakdown_partial_enrichment_only_shows_available_fields():
    text = format_breakdown(_detail(
        republish_events_today=None,
        rewrite_cache_hits_today=None,
    ))
    assert "11 in feed" in text
    assert "13 unique today" in text
    assert "publish events" not in text
    assert "Rewrite cache" not in text


def test_breakdown_zero_cards_in_feed_no_per_card_division():
    text = format_breakdown(_detail(cards_in_feed_now=0))
    assert "0 in feed" in text
    # No $/card line when feed is empty (avoid div by zero / meaningless number).
    assert "$/card in feed:" not in text


def test_breakdown_redeploy_churn_footnote_when_publishes_3x_unique():
    text = format_breakdown(_detail(
        republish_events_today=63,
        unique_cards_published_today=13,
    ))
    # 63 > 3 * 13 — should fire the footnote
    assert "heavy republish churn" in text


def test_breakdown_no_churn_footnote_when_low_republish_ratio():
    text = format_breakdown(_detail(
        republish_events_today=15,
        unique_cards_published_today=13,
    ))
    # 15 < 3 * 13 — no footnote
    assert "heavy republish churn" not in text


def test_breakdown_no_footnote_when_unique_cards_unknown():
    text = format_breakdown(_detail(
        republish_events_today=63,
        unique_cards_published_today=None,
    ))
    assert "heavy republish churn" not in text


def test_breakdown_handles_missing_total_calls():
    detail = _detail()
    detail.pop("total_calls", None)
    text = format_breakdown(detail)
    assert "0 calls" in text


def test_breakdown_handles_missing_days():
    detail = _detail()
    detail["days"] = []
    text = format_breakdown(detail)
    # Header lacks the date suffix when days is empty
    assert text.split("\n")[0] == "Daily breakdown"


def test_breakdown_zero_total_usd_skips_per_card_lines():
    text = format_breakdown(_detail(total_usd=0.0))
    assert "Total: $0.00" in text
    # When total spend is zero, $/card numbers would all be 0 — skip them.
    assert "$/card in feed:" not in text
    assert "$/unique card today:" not in text


def test_breakdown_handles_empty_kind_aggregate_dict():
    detail = _detail(by_kind={"news_scout": {}})
    text = format_breakdown(detail)
    assert "news_scout" in text
    assert "$0.0000" in text
