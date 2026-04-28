"""
Tests for /feed page <n> formatting via format_feed_page.
"""
import pytest

from ops_bot.formatting import format_feed_page


def _cards(n):
    return [
        {
            "id": f"abcdefgh{i:02d}",
            "hook_type": "tactical",
            "game": {"league": {"name": "Premier League"}},
            "narrative_hook": f"Narrative for card {i}",
            "total_odds": 2.10 + i * 0.01,
        }
        for i in range(n)
    ]


def test_page_1_of_n_shows_footer():
    cards = _cards(10)
    text = format_feed_page(cards[:5], page=1, total_pages=2, total_cards=10)
    assert "page 1 of 2" in text
    assert "10 cards total" in text


def test_last_page_partial():
    cards = _cards(3)
    text = format_feed_page(cards, page=2, total_pages=2, total_cards=8)
    assert "page 2 of 2" in text
    assert "8 cards total" in text
    # 3 card rows + empty line + footer
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) == 4  # 3 cards + footer


def test_out_of_range_page_message():
    text = format_feed_page([], page=99, total_pages=2, total_cards=10)
    assert "no such page" in text
    assert "2 page" in text


def test_card_row_contains_id_prefix():
    cards = _cards(1)
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # id is abcdefgh00 — first 8 chars = abcdefgh
    assert "abcdefgh" in text


def test_narrative_truncated_at_60():
    long_narrative = "A" * 80
    cards = [
        {
            "id": "abc12345",
            "hook_type": "news",
            "game": {"league": {"name": "La Liga"}},
            "narrative_hook": long_narrative,
            "total_odds": 1.95,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # Should contain truncated narrative with ellipsis
    assert "..." in text
    # The full 80-char string should not appear
    assert long_narrative not in text
