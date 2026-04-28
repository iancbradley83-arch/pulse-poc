"""
Tests for feed_audit module: build_feed_summary and pagination helpers.
"""
import pytest

from ops_bot.feed_audit import build_feed_summary, get_page, PAGE_SIZE


# ---------------------------------------------------------------------------
# build_feed_summary
# ---------------------------------------------------------------------------

def test_empty_feed():
    summary = build_feed_summary([])
    assert summary["total"] == 0
    assert summary["by_hook_type"] == []
    assert summary["by_league"] == []
    assert summary["missing_price"] == 0
    assert summary["suspended"] == 0
    assert summary["avg_relevance"] is None


def _make_card(
    hook_type="tactical",
    league="Premier League",
    total_odds=2.10,
    suspended=False,
    relevance_score=0.85,
    legs=None,
):
    game = {"league": {"name": league}}
    card = {
        "id": "abc12345",
        "hook_type": hook_type,
        "game": game,
        "total_odds": total_odds,
        "suspended": suspended,
        "relevance_score": relevance_score,
    }
    if legs is not None:
        card["legs"] = legs
    return card


def test_full_feed_basic():
    cards = [
        _make_card("tactical", "Premier League", 2.10),
        _make_card("injury", "La Liga", 1.90),
        _make_card("tactical", "Premier League", None),  # missing price
    ]
    summary = build_feed_summary(cards)
    assert summary["total"] == 3
    # hook_type counts
    hook_dict = dict(summary["by_hook_type"])
    assert hook_dict["tactical"] == 2
    assert hook_dict["injury"] == 1
    # league counts
    league_dict = dict(summary["by_league"])
    assert league_dict["Premier League"] == 2
    assert league_dict["La Liga"] == 1
    assert summary["missing_price"] == 1
    assert summary["suspended"] == 0
    assert summary["avg_relevance"] == 0.85


def test_missing_price_via_null_total_odds():
    cards = [_make_card(total_odds=None)]
    summary = build_feed_summary(cards)
    assert summary["missing_price"] == 1


def test_missing_price_via_null_leg_price():
    legs = [{"price": None, "selection": "Win"}]
    cards = [_make_card(total_odds=2.10, legs=legs)]
    summary = build_feed_summary(cards)
    assert summary["missing_price"] == 1


def test_no_missing_price_when_all_set():
    legs = [{"price": 1.85, "selection": "Win"}]
    cards = [_make_card(total_odds=1.85, legs=legs)]
    summary = build_feed_summary(cards)
    assert summary["missing_price"] == 0


def test_suspended_count():
    cards = [
        _make_card(suspended=True),
        _make_card(suspended=False),
        _make_card(suspended=True),
    ]
    summary = build_feed_summary(cards)
    assert summary["suspended"] == 2


def test_avg_relevance():
    cards = [
        _make_card(relevance_score=0.80),
        _make_card(relevance_score=0.90),
    ]
    summary = build_feed_summary(cards)
    assert summary["avg_relevance"] == 0.85


def test_by_hook_type_sorted_desc():
    cards = [
        _make_card("injury"),
        _make_card("tactical"),
        _make_card("tactical"),
        _make_card("tactical"),
        _make_card("injury"),
        _make_card("news"),
    ]
    summary = build_feed_summary(cards)
    counts = [count for _, count in summary["by_hook_type"]]
    assert counts == sorted(counts, reverse=True)


def test_by_league_top5():
    """Only top 5 leagues returned."""
    leagues = ["L1", "L2", "L3", "L4", "L5", "L6"]
    # L6 has 1 card (least), others have 2 each
    cards = []
    for i, l in enumerate(leagues):
        count = 1 if i == 5 else 2
        for _ in range(count):
            cards.append(_make_card(league=l))
    summary = build_feed_summary(cards)
    assert len(summary["by_league"]) == 5
    # L6 with count=1 should NOT be in top 5 since all others have 2
    names = [n for n, _ in summary["by_league"]]
    assert "L6" not in names


# ---------------------------------------------------------------------------
# get_page
# ---------------------------------------------------------------------------

def _make_cards(n: int):
    return [{"id": f"card{i:04d}"} for i in range(n)]


def test_page_1_of_3():
    cards = _make_cards(13)
    page_cards, total = get_page(cards, 1)
    assert total == 3
    assert len(page_cards) == PAGE_SIZE
    assert page_cards[0]["id"] == "card0000"


def test_last_page_truncated():
    cards = _make_cards(13)  # pages: 5, 5, 3
    page_cards, total = get_page(cards, 3)
    assert total == 3
    assert len(page_cards) == 3
    assert page_cards[0]["id"] == "card0010"


def test_page_out_of_range():
    cards = _make_cards(5)
    page_cards, total = get_page(cards, 99)
    assert page_cards == []
    assert total == 1  # 5 cards = 1 page


def test_page_zero_out_of_range():
    cards = _make_cards(5)
    page_cards, total = get_page(cards, 0)
    assert page_cards == []


def test_empty_feed_pagination():
    page_cards, total = get_page([], 1)
    assert page_cards == []
    assert total == 1  # max(1, ...) so we don't show 0 pages
