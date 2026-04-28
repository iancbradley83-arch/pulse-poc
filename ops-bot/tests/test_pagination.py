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
    assert "10 cards" in text


def test_last_page_partial():
    cards = _cards(3)
    text = format_feed_page(cards, page=2, total_pages=2, total_cards=8)
    assert "page 2 of 2" in text
    assert "8 cards" in text


def test_out_of_range_page_message():
    text = format_feed_page([], page=99, total_pages=2, total_cards=10)
    assert "no such page" in text
    assert "2 page" in text


def test_card_block_contains_id_prefix():
    cards = _cards(1)
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # id is abcdefgh00 — first 8 chars = abcdefgh, wrapped in [ ]
    assert "[abcdefgh]" in text


def test_narrative_truncated_at_word_boundary():
    long_narrative = (
        "Saka starts in Madrid; Arsenal's width reopens against a "
        "back four that has conceded twice in the past three games "
        "across all competitions, with substitutions tactical."
    )
    cards = [
        {
            "id": "abc12345",
            "hook_type": "news",
            "game": {
                "home_team": "Arsenal",
                "away_team": "Real Madrid",
                "league": {"name": "Champions League"},
            },
            "narrative_hook": long_narrative,
            "total_odds": 1.95,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    # Should contain ellipsis (…) marker on truncation
    assert "…" in text
    # The full long narrative should not appear verbatim
    assert long_narrative not in text


def test_block_includes_game_and_league_lines():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "tactical",
            "game": {
                "home_team": "PSG",
                "away_team": "Bayern Munich",
                "league": {"name": "Champions League"},
            },
            "narrative_hook": "Short narrative",
            "total_odds": 4.10,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "PSG vs Bayern Munich" in text
    assert "Champions League" in text


def test_no_price_renders_as_no_price():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "news",
            "game": {"home_team": "Arsenal", "away_team": "Real Madrid"},
            "narrative_hook": "test",
            "total_odds": None,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "no price" in text


def test_suspended_card_flagged():
    cards = [
        {
            "id": "abc12345",
            "hook_type": "tactical",
            "game": {"home_team": "Arsenal", "away_team": "Real Madrid"},
            "narrative_hook": "test",
            "total_odds": 2.0,
            "suspended": True,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "[SUSPENDED]" in text


def test_block_extracts_team_short_name_from_dict():
    """Regression: /api/feed gives home_team as a dict {short_name, color},
    not a string. _team_name must extract short_name (or name) — the dict
    literal must NEVER appear."""
    cards = [
        {
            "id": "abc12345",
            "hook_type": "injury",
            "game": {
                "home_team": {"short_name": "AM", "color": "#CB3524"},
                "away_team": {"short_name": "ARS", "color": "#EF0107"},
            },
            "narrative_hook": "test",
            "total_odds": 4.45,
        }
    ]
    text = format_feed_page(cards, page=1, total_pages=1, total_cards=1)
    assert "AM vs ARS" in text
    assert "short_name" not in text
    assert "color" not in text
    assert "#CB3524" not in text


def test_pagination_footer_uses_underscore_form():
    """Telegram only treats /cards_2 (underscore) as a tappable command;
    /cards 2 (space) does not pass the arg through tap. Footer must use _N."""
    cards = [{"id": "abc12345", "hook_type": "x", "game": {}, "narrative_hook": "n", "total_odds": 1.5}]
    text = format_feed_page(cards, page=2, total_pages=5, total_cards=25)
    assert "/cards_1" in text
    assert "/cards_3" in text
    # No legacy space-arg form
    assert "/cards 1" not in text
    assert "/cards 3" not in text
