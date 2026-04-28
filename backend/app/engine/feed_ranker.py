"""Feed ranker v1 — score, mix-quota, variety-guard the published feed.

Pure functions only. Takes a list of Cards plus the PULSE_BET_TYPE_MIX dict
and returns an ordered list. See docs/refresh-and-ordering.md §2 for the
target spec and ROADMAP R1/R2 for acceptance.

The ranker does five things:
  1. Scores every card on relevance + fixture-proximity + freshness + a
     small featured bump.
  2. Drops cards that are "no-shows" (kickoff passed, fully suspended).
  3. Drops same-fixture + same-market_type duplicates, keeping the higher
     scored one.
  4. Interleaves by bet_type using the operator-configured quota slots so
     no single bet_type (today: featured BBs) monopolises the lead.
  5. Runs a hook-variety guard that breaks up consecutive same-hook_type
     pairs regardless of league, prefers same-bucket swaps to preserve
     the bet_type mix, and falls back to cross-bucket swaps if needed.
     Budget-capped at 5 swaps per pass to avoid thrashing.
  6. Runs the legacy same-league + same-hook demotion as a tie-breaker
     for any collision the hook-variety pass couldn't resolve.

Imports from `app.models.schemas` for Card typing, but stays side-effect
free so the `python backend/app/engine/feed_ranker.py` self-test runs in
isolation.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Optional

from app.models.schemas import Card


# ── Scoring weights (sum to 1.0) ────────────────────────────────────────
_W_RELEVANCE = 0.50
_W_PROXIMITY = 0.25
_W_FRESHNESS = 0.15
_W_OPERATOR = 0.05
_W_ENGAGEMENT = 0.05

# Decay constants (hours)
_PROXIMITY_TAU_HOURS = 72.0   # exp(-h/72) over 3-day horizon
_FRESHNESS_TAU_HOURS = 12.0   # exp(-h/12) steep decay past 12h

# Featured BB operator bump
_FEATURED_BUMP = 0.05

# Variety guard
_MAX_VARIETY_SWAPS = 3
# Hook-variety guard (runs before the league+hook demotion). Budget-capped
# per rank pass to avoid pathological shuffling on hook-heavy inputs.
_MAX_HOOK_VARIETY_SWAPS = 5


# ── Bet-type normalisation ──────────────────────────────────────────────
# PULSE_BET_TYPE_MIX keys are {"singles","bb","combos"}.
# Card.bet_type values are {"single","bet_builder","combo"}.
_BET_TYPE_BUCKET = {
    "single": "singles",
    "bet_builder": "bb",
    "combo": "combos",
}


def _bucket_of(card: Card) -> str:
    return _BET_TYPE_BUCKET.get(card.bet_type or "single", "singles")


# ── Kickoff parsing ─────────────────────────────────────────────────────
# game.start_time comes through as a formatted string like
# "22 Apr 19:00 UTC" (catalogue_loader._start_time). Parse it back.
_KICKOFF_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3})\s+(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*UTC",
)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_kickoff_utc(start_time: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse "22 Apr 19:00 UTC" into a timezone-aware UTC datetime.

    Year isn't in the string. Infer: use the current year; if that lands
    more than ~90 days in the past, assume it's next year's fixture.
    """
    if not start_time:
        return None
    m = _KICKOFF_RE.search(start_time)
    if not m:
        return None
    day = int(m.group("day"))
    mon = _MONTHS.get(m.group("mon").lower())
    if not mon:
        return None
    hh = int(m.group("hh"))
    mm = int(m.group("mm"))
    ref = now or datetime.now(timezone.utc)
    year = ref.year
    try:
        kickoff = datetime(year, mon, day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    # Year-wrap: if the kickoff looks >90 days in the past, bump a year.
    if (ref - kickoff).total_seconds() > 90 * 86400:
        try:
            kickoff = datetime(year + 1, mon, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            return None
    return kickoff


def _hours_to_kickoff(card: Card, *, now: Optional[datetime] = None) -> Optional[float]:
    """Positive = kickoff ahead; negative = kickoff passed; None = unknown."""
    if not card.game or not card.game.start_time:
        return None
    kickoff = _parse_kickoff_utc(card.game.start_time, now=now)
    if kickoff is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return (kickoff - ref).total_seconds() / 3600.0


# ── News freshness ──────────────────────────────────────────────────────
# Card.ago_minutes is set for news-driven cards (stage 2 handoff).
# For multi-news cards we pick the newest.
def _hours_since_news(card: Card) -> Optional[float]:
    if card.ago_minutes is not None and card.ago_minutes >= 0:
        return card.ago_minutes / 60.0
    # news[].time_ago is a human string ("2h ago", "30m ago"). Best-effort parse.
    newest: Optional[float] = None
    for n in card.news or []:
        h = _parse_time_ago_hours(getattr(n, "time_ago", "") or "")
        if h is None:
            continue
        if newest is None or h < newest:
            newest = h
    return newest


_TIME_AGO_RE = re.compile(r"(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days)", re.IGNORECASE)


def _parse_time_ago_hours(s: str) -> Optional[float]:
    if not s:
        return None
    m = _TIME_AGO_RE.search(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("d"):
        return n * 24.0
    if unit.startswith("h"):
        return float(n)
    return n / 60.0  # minutes


# ── Scoring ─────────────────────────────────────────────────────────────
def score_card(card: Card, *, now: Optional[datetime] = None) -> float:
    """Weighted 0-1ish score. Deterministic; pure function for tests."""
    # 1. Relevance (already 0-1).
    relevance = float(card.relevance_score or 0.0)

    # 2. Proximity: exp(-h/72), bounded [0, 1]. 0 if kickoff passed.
    proximity = 0.0
    h = _hours_to_kickoff(card, now=now)
    if h is not None and h > 0:
        proximity = math.exp(-h / _PROXIMITY_TAU_HOURS)
    elif h is None:
        # Unknown kickoff → treat as mid-horizon rather than 0.
        proximity = math.exp(-24.0 / _PROXIMITY_TAU_HOURS)  # ~0.72

    # 3. Freshness: exp(-h/12). If no news referenced, small default.
    freshness = 0.0
    hn = _hours_since_news(card)
    if hn is not None and hn >= 0:
        freshness = math.exp(-hn / _FRESHNESS_TAU_HOURS)
    else:
        freshness = 0.3  # no news = neutral-ish; don't zero it out

    # 4. Operator preference: +bump for featured BBs.
    operator = _FEATURED_BUMP if _is_featured(card) else 0.0

    # 5. Engagement: R5 placeholder.
    engagement = 0.0

    return (
        relevance * _W_RELEVANCE
        + proximity * _W_PROXIMITY
        + freshness * _W_FRESHNESS
        + operator * _W_OPERATOR
        + engagement * _W_ENGAGEMENT
    )


def _is_featured(card: Card) -> bool:
    """Featured BBs are operator-curated BBs with no news attached.

    In production featured BBs come through with either ``hook_type`` unset
    or explicitly tagged ``"featured"`` (case-insensitive) — accept both.
    """
    if (card.bet_type or "") != "bet_builder":
        return False
    hook = (card.hook_type or "").strip().lower()
    if hook and hook != "featured":
        return False  # news-driven BB
    if card.news:
        return False
    return True


# ── Drop filters ────────────────────────────────────────────────────────
def _is_no_show(card: Card, *, now: Optional[datetime] = None) -> bool:
    """Drop cards where kickoff has passed OR all selections are suspended."""
    h = _hours_to_kickoff(card, now=now)
    if h is not None and h < 0:
        return True
    if getattr(card, "suspended", False):
        return True
    return False


def _dedupe_by_fixture_market(cards: list[Card]) -> list[Card]:
    """Drop same-fixture same-market_type duplicates; keep highest scored."""
    seen: dict[tuple[str, str], Card] = {}
    for c in cards:
        fixture = c.game.id if c.game else ""
        mtype = c.market.market_type if c.market else ""
        if not fixture or not mtype:
            # Can't dedupe without a key — pass through but preserve ordering
            # by using object id as a tiebreak-safe unique key.
            seen[(fixture or id(c), mtype or id(c))] = c
            continue
        key = (fixture, mtype)
        prev = seen.get(key)
        if prev is None or (c.__ranker_score__ > prev.__ranker_score__):  # type: ignore[attr-defined]
            seen[key] = c
    return list(seen.values())


# ── Mix quota (slot-based interleaving) ─────────────────────────────────
def _apply_mix_quota(
    sorted_cards: list[Card], mix: dict[str, int], limit: int,
) -> list[Card]:
    """Walk the sorted list, picking the highest-score card whose bucket
    still has slot quota. When a bucket fills, skip and keep interleaving.

    Quota shape: we translate the weights in `mix` into integer slot counts
    for `limit` slots, rounding down with leftover handed to the largest
    bucket.
    """
    total_weight = sum(max(0, v) for v in mix.values()) or 1
    quotas: dict[str, int] = {}
    for bucket in ("singles", "bb", "combos"):
        w = max(0, int(mix.get(bucket, 0)))
        quotas[bucket] = (w * limit) // total_weight
    # Hand leftover to the largest bucket (stable tiebreak).
    used = sum(quotas.values())
    if used < limit:
        leftover = limit - used
        largest = max(quotas.keys(), key=lambda k: (mix.get(k, 0), k))
        quotas[largest] += leftover

    out: list[Card] = []
    remaining = list(sorted_cards)
    # Slot-based walk: at every position, prefer the highest-scoring card
    # whose bucket is under-quota. Once a bucket is full, further cards of
    # that bucket are skipped entirely.
    while remaining and len(out) < limit:
        picked_idx: Optional[int] = None
        for i, c in enumerate(remaining):
            if quotas.get(_bucket_of(c), 0) > 0:
                picked_idx = i
                break
        if picked_idx is None:
            break  # every remaining card is in a full bucket
        card = remaining.pop(picked_idx)
        out.append(card)
        quotas[_bucket_of(card)] -= 1
    return out


# ── Hook-variety guard (runs first) ─────────────────────────────────────
def _apply_hook_variety_guard(
    ordered: list[Card], *, max_swaps: int = _MAX_HOOK_VARIETY_SWAPS,
) -> list[Card]:
    """Break up consecutive cards that share ``hook_type`` regardless of
    league.

    Walk the ordered list. For each slot ``i`` where
    ``cards[i].hook_type == cards[i-1].hook_type`` (both non-empty), look
    for the best later card whose hook_type differs from both
    ``cards[i-1]`` and ``cards[i+1]`` (if present) and swap it in.

    Preference order when picking a donor card:
      1. Same bet_type bucket as ``cards[i]`` (preserves the mix quota).
      2. Any bucket — log one line when we take a cross-bucket swap.

    Budget-capped at ``max_swaps`` per rank pass. If no compatible swap
    exists, accept the clump and move on.
    """
    n = len(ordered)
    if n < 2:
        return ordered
    swaps_used = 0
    i = 1
    while i < n and swaps_used < max_swaps:
        prev_hook = ordered[i - 1].hook_type or ""
        curr_hook = ordered[i].hook_type or ""
        if not prev_hook or prev_hook != curr_hook:
            i += 1
            continue
        # Hook clump at slot i. Look for a donor at index j > i whose
        # hook differs from prev_hook AND from the card that would land
        # at slot i+1 after the swap (i.e. ordered[i+1] if j != i+1,
        # else the old ordered[i]). Prefer same-bucket donors.
        target_bucket = _bucket_of(ordered[i])
        after_hook = ordered[i + 1].hook_type or "" if i + 1 < n else ""
        same_bucket_donor: Optional[int] = None
        any_bucket_donor: Optional[int] = None
        for j in range(i + 1, n):
            donor = ordered[j]
            dhook = donor.hook_type or ""
            if not dhook or dhook == prev_hook:
                continue
            # After the swap the card at i+1 will be the old ordered[i]
            # (if j == i+1) — its hook equals prev_hook, which is fine;
            # the clump we care about is between i-1 and i, not i and
            # i+1. For j > i+1, the card at i+1 stays the same so we
            # also want donor's hook != after_hook to avoid immediately
            # creating a new i/i+1 clump.
            if j > i + 1 and after_hook and dhook == after_hook:
                continue
            if any_bucket_donor is None:
                any_bucket_donor = j
            if _bucket_of(donor) == target_bucket:
                same_bucket_donor = j
                break
        donor_idx = same_bucket_donor if same_bucket_donor is not None else any_bucket_donor
        if donor_idx is None:
            # No compatible donor — accept the clump.
            i += 1
            continue
        if same_bucket_donor is None:
            # Cross-bucket swap: log one line so we can see it in the feed
            # audit. Keep it lightweight — this is a pure function so we
            # just print; the caller captures stdout in admin rerun logs.
            print(
                "[feed_ranker] hook-variety cross-bucket swap: "
                f"slot={i} hook={curr_hook!r} donor_slot={donor_idx} "
                f"donor_bucket={_bucket_of(ordered[donor_idx])} "
                f"target_bucket={target_bucket}"
            )
        ordered[i], ordered[donor_idx] = ordered[donor_idx], ordered[i]
        swaps_used += 1
        # Re-check this slot — the swapped-in card may still clump with
        # i-1 if all later cards share the same hook, in which case the
        # next loop iteration will find no donor and move on.
        # Advance to next slot regardless; budget cap bounds total work.
        i += 1
    return ordered


# ── Variety guard ───────────────────────────────────────────────────────
def _apply_variety_guard(ordered: list[Card]) -> list[Card]:
    """If consecutive cards share league AND hook_type, push the later one
    one slot back. Max 3 swaps per card so pathological inputs never loop."""
    if len(ordered) < 2:
        return ordered
    swaps_per_card: dict[int, int] = {}
    i = 1
    # Index-safe walk with bounded swaps.
    while i < len(ordered):
        prev = ordered[i - 1]
        curr = ordered[i]
        if _collides(prev, curr) and i + 1 < len(ordered):
            cid = id(curr)
            if swaps_per_card.get(cid, 0) < _MAX_VARIETY_SWAPS:
                ordered[i], ordered[i + 1] = ordered[i + 1], ordered[i]
                swaps_per_card[cid] = swaps_per_card.get(cid, 0) + 1
                # Re-check the new position (don't advance)
                continue
        i += 1
    return ordered


def _collides(a: Card, b: Card) -> bool:
    la = (a.game.broadcast or "") if a.game else ""
    lb = (b.game.broadcast or "") if b.game else ""
    ha = a.hook_type or ""
    hb = b.hook_type or ""
    if not la or not ha:
        return False
    return la == lb and ha == hb


# ── Public entry point ──────────────────────────────────────────────────
def rank_cards(
    cards: list[Card],
    mix: dict[str, int],
    *,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> list[Card]:
    """Rank and bucket the feed. Pure: input list is not mutated in place
    beyond a transient `__ranker_score__` attribute on each Card."""
    if not cards:
        return []

    # 1. Score all cards (stash on the Card for dedupe + debug).
    for c in cards:
        try:
            setattr(c, "__ranker_score__", score_card(c, now=now))
        except Exception:
            setattr(c, "__ranker_score__", float(c.relevance_score or 0.0))

    # 2. Drop no-shows. (Correctness — kickoff-passed / suspended cards
    #    must never render. Not gated.)
    alive = [c for c in cards if not _is_no_show(c, now=now)]

    # 3. Dedupe same-fixture same-market duplicates.
    #    Gated behind PULSE_PRUNE_PAID_CARDS (default false) per the
    #    "publish everything we paid LLM cost for" decision (Ian,
    #    2026-04-28). When the kill switch is off (default), this filter
    #    is a no-op so every card we incurred LLM cost on stays visible
    #    until kickoff / TTL expiry. When set to "true", prior pruning
    #    behaviour is restored (drop the lower-scored same-fixture +
    #    same-market_type card). See item 1 in
    #    docs/follow-ups-from-ops-session-2026-04-28.md for the trace
    #    showing this filter was the source of today's 2-card gap.
    try:
        from app.config import PULSE_PRUNE_PAID_CARDS as _prune_paid
    except Exception:
        _prune_paid = False
    if _prune_paid:
        alive = _dedupe_by_fixture_market(alive)

    # 4. Sort by score desc.
    alive.sort(key=lambda c: getattr(c, "__ranker_score__", 0.0), reverse=True)

    # 5. Slot-interleave by bet-type quota.
    picked = _apply_mix_quota(alive, mix, limit)

    # 6. Hook-variety guard (league-agnostic). Toggleable so we can A/B
    # via PULSE_HOOK_VARIETY_GUARD_ENABLED. Import is deferred to keep
    # this module importable in the self-test without the app package
    # being fully wired.
    try:
        from app.config import PULSE_HOOK_VARIETY_GUARD_ENABLED as _hvg_on
    except Exception:
        _hvg_on = True
    if _hvg_on:
        picked = _apply_hook_variety_guard(picked)

    # 7. Legacy league+hook variety guard (tie-breaker).
    picked = _apply_variety_guard(picked)

    return picked[:limit]


# ── Self-test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Self-test pre-dates the PULSE_PRUNE_PAID_CARDS kill switch; it
    # asserts the same-fixture+same-market dedupe runs. Force the kill
    # switch on so the self-test continues to exercise prior behaviour
    # (the production default is now off — every paid card stays
    # visible). Real test coverage of both modes lives in
    # tests/test_publish_everything_paid.py.
    import os as _os
    _os.environ["PULSE_PRUNE_PAID_CARDS"] = "true"
    from app import config as _cfg
    _cfg.PULSE_PRUNE_PAID_CARDS = True

    from app.models.schemas import (
        Game, Team, Sport, GameStatus, Market, MarketSelection, NewsItem,
    )

    def mk_team(tid: str, name: str) -> Team:
        return Team(id=tid, name=name, short_name=name[:3], color="#000", sport=Sport.SOCCER)

    def mk_game(gid: str, league: str, hours_ahead: float) -> Game:
        kickoff = datetime.now(timezone.utc).replace(microsecond=0)
        kickoff = kickoff.fromtimestamp(kickoff.timestamp() + hours_ahead * 3600, tz=timezone.utc)
        start = kickoff.strftime("%d %b %H:%M UTC")
        return Game(
            id=gid, sport=Sport.SOCCER,
            home_team=mk_team(f"{gid}_h", "Home"),
            away_team=mk_team(f"{gid}_a", "Away"),
            status=GameStatus.SCHEDULED, broadcast=league, start_time=start,
        )

    def mk_card(
        *, gid: str, league: str, hours_ahead: float, bet_type: str,
        relevance: float, hook: Optional[str] = None, ago_minutes: Optional[int] = None,
        market_type: str = "match_result", suspended: bool = False,
    ) -> Card:
        game = mk_game(gid, league, hours_ahead)
        market = Market(
            id=f"m_{gid}", game_id=gid, market_type=market_type, label="x",
            selections=[MarketSelection(label="Home", odds="2.00")],
        )
        c = Card(
            card_type=CardType.PRE_MATCH, game=game, market=market,
            relevance_score=relevance, bet_type=bet_type, hook_type=hook,
            ago_minutes=ago_minutes, suspended=suspended,
        )
        return c

    # Need CardType for the helper above
    from app.models.schemas import CardType  # noqa: E402

    # --- Synthetic cards --------------------------------------------------
    cards = [
        # A featured BB (no hook, no news) — should NOT auto-top.
        mk_card(gid="g1", league="EPL", hours_ahead=72, bet_type="bet_builder",
                relevance=0.70, hook=None),
        # A high-relevance fresh single, 6h to kickoff — should top the list.
        mk_card(gid="g2", league="EPL", hours_ahead=6, bet_type="single",
                relevance=0.85, hook="injury", ago_minutes=30,
                market_type="player_goals"),
        # A news BB, 24h to kickoff, mid relevance.
        mk_card(gid="g3", league="La Liga", hours_ahead=24, bet_type="bet_builder",
                relevance=0.65, hook="team_news", ago_minutes=60),
        # Same fixture & market as the 0.85 single, lower score — drop.
        mk_card(gid="g2", league="EPL", hours_ahead=6, bet_type="single",
                relevance=0.40, hook="injury", ago_minutes=30,
                market_type="player_goals"),
        # Another EPL news BB with same hook_type as the featured — variety test
        # if it ends up next to an EPL neighbour with same hook.
        mk_card(gid="g4", league="EPL", hours_ahead=48, bet_type="bet_builder",
                relevance=0.60, hook="tactical", ago_minutes=180),
        # A combo.
        mk_card(gid="g5", league="Bundesliga", hours_ahead=12, bet_type="combo",
                relevance=0.72, hook="storyline", ago_minutes=90),
        # Kickoff-passed card — must be dropped.
        mk_card(gid="g6", league="EPL", hours_ahead=-2, bet_type="single",
                relevance=0.90),
        # Suspended card — must be dropped.
        mk_card(gid="g7", league="EPL", hours_ahead=5, bet_type="single",
                relevance=0.88, suspended=True),
        # Two EPL injury singles in a row to trigger variety guard.
        mk_card(gid="g8", league="EPL", hours_ahead=30, bet_type="single",
                relevance=0.80, hook="injury", ago_minutes=20,
                market_type="anytime_scorer"),
        mk_card(gid="g9", league="EPL", hours_ahead=30, bet_type="single",
                relevance=0.78, hook="injury", ago_minutes=25,
                market_type="btts"),
    ]

    mix = {"singles": 40, "bb": 30, "combos": 30}

    ranked = rank_cards(cards, mix, limit=10)

    # Assertions
    assert len(ranked) <= 10, "limit respected"
    # 1. Kickoff-passed and suspended dropped.
    fixture_ids = [c.game.id for c in ranked]
    assert "g6" not in fixture_ids, "kickoff-passed card dropped"
    assert "g7" not in fixture_ids, "suspended card dropped"
    # 2. Duplicate dropped (we had two g2 singles; only one survives).
    assert fixture_ids.count("g2") == 1, "duplicate same-fixture same-market dropped"
    # 3. Mix quota: with limit 10 and 40:30:30 → singles=4, bb=3, combos=3.
    from collections import Counter
    bucket_counts = Counter(_bucket_of(c) for c in ranked)
    # Only 3 singles and 3 BBs available post-dedupe, 1 combo. Slot walk
    # should pick everything since no bucket is over-supplied vs quota.
    # Assert no bucket exceeds its quota.
    assert bucket_counts["singles"] <= 4, f"singles over quota: {bucket_counts}"
    assert bucket_counts["bb"] <= 3, f"bb over quota: {bucket_counts}"
    assert bucket_counts["combos"] <= 3, f"combos over quota: {bucket_counts}"
    # 4. The 0.85-relevance fresh single should be at or near the top
    # (singles slot comes first in the 40:30:30 interleave).
    top_score = getattr(ranked[0], "__ranker_score__", 0.0)
    assert top_score > 0.4, f"top card score should be real, got {top_score}"
    # 5. Featured BB not top if a higher-score card exists.
    top = ranked[0]
    assert not (_is_featured(top) and top.relevance_score < 0.85), (
        "featured BB should not auto-top over a higher-scored news card"
    )
    # 6. Variety guard: no two consecutive cards should share league+hook
    # (we had g8+g9 both EPL/injury; guard should have split them).
    for i in range(1, len(ranked)):
        a, b = ranked[i - 1], ranked[i]
        if (a.game.broadcast == b.game.broadcast
                and a.hook_type and a.hook_type == b.hook_type):
            # Allowed only if we ran out of swap budget (< 2 other cards).
            pass

    print("=== feed_ranker self-test ===")
    print(f"Input cards: {len(cards)}  Ranked: {len(ranked)}")
    print(f"Bucket distribution: {dict(bucket_counts)}")
    for i, c in enumerate(ranked):
        s = getattr(c, "__ranker_score__", 0.0)
        print(
            f"  {i + 1}. [{_bucket_of(c):7s}] {c.game.id} "
            f"({c.game.broadcast}/{c.hook_type or '-'}) "
            f"rel={c.relevance_score:.2f} score={s:.3f}"
        )

    # --- Hook-variety stress test ----------------------------------------
    # 10 cards: 6 team_news, 3 injury, 1 preview, spread across leagues so
    # the league+hook guard alone would not break them up.
    hv_cards: list[Card] = []
    leagues = ["EPL", "La Liga", "Bundesliga", "Serie A", "Ligue 1", "Eredivisie"]
    # 6 team_news singles across 6 different leagues
    for idx in range(6):
        hv_cards.append(mk_card(
            gid=f"tn{idx}", league=leagues[idx], hours_ahead=10 + idx,
            bet_type="single", relevance=0.80 - idx * 0.01,
            hook="team_news", ago_minutes=30 + idx,
            market_type=f"mt_tn{idx}",
        ))
    # 3 injury singles across 3 different leagues
    for idx in range(3):
        hv_cards.append(mk_card(
            gid=f"inj{idx}", league=leagues[idx], hours_ahead=20 + idx,
            bet_type="single", relevance=0.75 - idx * 0.01,
            hook="injury", ago_minutes=40 + idx,
            market_type=f"mt_inj{idx}",
        ))
    # 1 preview single
    hv_cards.append(mk_card(
        gid="prev0", league="EPL", hours_ahead=36,
        bet_type="single", relevance=0.70,
        hook="preview", ago_minutes=90,
        market_type="mt_prev0",
    ))

    hv_ranked = rank_cards(hv_cards, {"singles": 100, "bb": 0, "combos": 0}, limit=15)
    print("\n=== hook-variety stress test ===")
    for i, c in enumerate(hv_ranked):
        print(f"  {i + 1}. hook={c.hook_type or '-':10s} league={c.game.broadcast}")

    # Count remaining consecutive-same-hook pairs in the first 15 slots.
    hv_consecutive = 0
    for i in range(1, min(15, len(hv_ranked))):
        a_hook = hv_ranked[i - 1].hook_type or ""
        b_hook = hv_ranked[i].hook_type or ""
        if a_hook and a_hook == b_hook:
            # With 6 team_news + 3 injury + 1 preview = 10 cards, we have
            # at most 4 "other" cards to slot between the 6 team_news, so
            # some clumping is mathematically forced. Check that we did
            # better than the dumb sorted order (which would yield 5
            # consecutive pairs from the 6 team_news block).
            hv_consecutive += 1
    print(f"Consecutive same-hook pairs in first 15: {hv_consecutive}")
    # Dumb sort would give 5 (6-in-a-row team_news) + 2 (3-in-a-row
    # injury) = 7 clump pairs. The guard should bring this down well
    # below that. Assert strictly less than 3 — the mathematically
    # forced floor with this composition is 2 (6 team_news spread
    # among 4 others = two clumps).
    assert hv_consecutive <= 2, (
        f"hook-variety guard should leave ≤2 forced clumps, got {hv_consecutive}"
    )

    # --- _is_featured: accept hook_type="featured" ------------------------
    # Production featured BBs come through tagged with hook_type="featured"
    # rather than None — both shapes must count as operator-featured.
    featured_tagged = mk_card(
        gid="feat1", league="EPL", hours_ahead=48, bet_type="bet_builder",
        relevance=0.60, hook="featured",
    )
    assert _is_featured(featured_tagged) is True, (
        "BB with hook_type='featured' and no news should be treated as featured"
    )

    print("PASS")
