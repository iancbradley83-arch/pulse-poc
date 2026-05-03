"""Narrative archetypes — story shapes that bridge news to bet shapes.

An archetype captures **what kind of story this is** at a level of
abstraction above the news hook (INJURY/TACTICAL/etc.) and below the
specific market types. Each archetype:

  * Detects itself from a news item (rule-based matchers; LLM second
    opinion hooked but inert in this PR).
  * Names the **subject** of the story (player / team / manager / match).
  * Emits **derived signals** that the composer uses to score market
    legs.
  * Carries **bet-shape constraints** — rules like "every leg must
    connect to subject_player" — that prevent off-topic combinations.

The starter set covers the most common news patterns we see today.
Extend by adding entries to `ARCHETYPES`. Don't try to perfect this
list; the design assumes it grows as we hit narratives the matchers
miss (logged via `[narrative_uncertain]`).

## Self-learning hook

`derive_archetype()` returns a `(primary, alternatives, confidence)`
tuple. Every decision is logged via `narrative_telemetry` so we can
later measure: did high-confidence matches drive engagement? Did our
manual archetype list miss patterns that engagement implies are real?
This is the data plumbing that makes the rule set self-correcting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.models.news import HookType, NewsItem


# ── Subject types ──────────────────────────────────────────────────────


SUBJECT_PLAYER = "player"
SUBJECT_TEAM = "team"
SUBJECT_MANAGER = "manager"
SUBJECT_MATCH = "match"


# ── Archetype dataclass ────────────────────────────────────────────────


@dataclass(frozen=True)
class Archetype:
    """Definition of one narrative archetype.

    `match_keywords` is an OR list of substrings (case-insensitive).
    `match_hooks` filters by news.hook_type — if non-empty, the news
    hook must be in this set.
    `signal_templates` are signal strings (with `{p}`/`{team}`
    placeholders) that get resolved at thesis-build time using the
    detected subject id.
    `forbidden_market_keys` is a hard reject — no leg from these
    market metadata `key`s may appear in the combination.
    `preferred_market_keys` carries an additive scoring boost for
    matching market_meta keys.
    `bet_shape_rule` is a free-form string the composer reads — values
    today: `subject_centric` (every leg must reference subject id),
    `subject_team_centric` (every leg must reference subject's team),
    `match_centric` (no subject constraint), `discipline_only`
    (every leg must be in {Cards, Player Specials} groups).
    """
    key: str
    description: str
    match_keywords: tuple[str, ...] = ()
    match_hooks: tuple[HookType, ...] = ()
    subject_type: str = SUBJECT_MATCH  # one of SUBJECT_*
    signal_templates: tuple[str, ...] = ()
    preferred_market_keys: tuple[str, ...] = ()
    forbidden_market_keys: tuple[str, ...] = ()
    bet_shape_rule: str = "match_centric"
    max_legs_hint: Optional[int] = None  # composer caps at this if set
    base_confidence: float = 0.7         # rule match → this confidence


# ── Starter archetype set ──────────────────────────────────────────────

ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        key="PLAYER_DISCIPLINE_RISK",
        description=(
            "Named player on a booking watch — one yellow from "
            "suspension, repeat offender, opp will target. Bet orbits "
            "this player's discipline; goalscorer / team result legs "
            "are off-topic."
        ),
        match_keywords=(
            "one booking from", "one yellow from", "yellow card away",
            "suspension watch", "must avoid a card", "carded in",
            "booked in", "discipline record", "five yellow cards",
            "target him", "rough him up",
        ),
        match_hooks=(HookType.TEAM_NEWS, HookType.TACTICAL,
                     HookType.PREVIEW, HookType.MANAGER_QUOTE),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.discipline_pressure",
            "player.{p}.targeted_by_opp",
            "discipline.heavy.first_half",
            "discipline.heavy",
            "physicality.high",
        ),
        preferred_market_keys=(
            "player_to_be_booked",
            "player_to_be_carded_first",
            "player_red_card",
            "cards_ft_ou",
            "cards_first_half_ou",
            "team_total_cards_ou",
            "total_match_fouls",
        ),
        forbidden_market_keys=(
            "anytime_goalscorer",
            "player_to_score_or_assist",
            "player_to_score_2_or_more",
            "player_to_score_3_or_more",
            "player_to_score_with_header",
            "player_to_score_outside_box",
            "first_half_anytime_goalscorer",
        ),
        bet_shape_rule="discipline_only",
        max_legs_hint=4,
        base_confidence=0.85,
    ),
    Archetype(
        key="PLAYER_FORM_STREAK",
        description=(
            "Named player in attacking form — scored in N straight, on "
            "shot streak, key man returning. Bet leads with that "
            "player's attacking output."
        ),
        match_keywords=(
            "scored in", "on a streak", "in form", "hot streak",
            "back-to-back goals", "consecutive games",
            "shot streak", "shooting frenzy", "in red-hot form",
        ),
        match_hooks=(HookType.TEAM_NEWS, HookType.PREVIEW,
                     HookType.MANAGER_QUOTE, HookType.TRANSFER),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.in_form",
            "player.{p}.active",
            "player.{p}.attacking_role",
        ),
        preferred_market_keys=(
            "anytime_goalscorer",
            "player_to_score_or_assist",
            "player_over_shots",
            "player_over_shots_on_target",
            "player_to_score_2_or_more",
            "team_total_goals_ou",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="subject_centric",
        max_legs_hint=4,
        base_confidence=0.8,
    ),
    Archetype(
        key="KEY_ATTACKER_OUT",
        description=(
            "Star attacker ruled out (injury / suspension). Affected "
            "team's attack weakens; opp clean sheet + opp dominance."
        ),
        match_keywords=(
            "ruled out", "injured", "out for", "suspended",
            "won't feature", "misses out", "absent",
            "blow for",
        ),
        match_hooks=(HookType.INJURY, HookType.TEAM_NEWS),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.out",
            # Composer fills team-level signals from subject's team
        ),
        preferred_market_keys=(
            # Filled by composer using opp team_id; the strings here are
            # market_meta keys that BENEFIT from the derived team signals.
            "team_total_goals_ou",       # opp under
            "team_clean_sheet",          # opp clean sheet
            "total_goals_ou",            # under
            "both_teams_to_score",       # no
            "match_result",              # opp side
            "asian_handicap",            # opp side
        ),
        forbidden_market_keys=(),  # but the absent player's markets are pruned by signals
        bet_shape_rule="subject_team_centric",
        max_legs_hint=5,
        base_confidence=0.85,
    ),
    Archetype(
        key="KEY_DEFENDER_OUT",
        description=(
            "Star defender / GK out. Affected team's defence weakens; "
            "opp scores, BTTS yes, totals up."
        ),
        match_keywords=(
            "centre-back out", "defender ruled out", "keeper out",
            "goalkeeper injured", "back four reshuffled",
            "defensive crisis", "clean sheet record",
        ),
        match_hooks=(HookType.INJURY, HookType.TEAM_NEWS),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.out",
        ),
        preferred_market_keys=(
            "team_total_goals_ou",       # opp over
            "anytime_goalscorer",        # opp side scorers
            "total_goals_ou",            # over
            "both_teams_to_score",       # yes
            "team_total_corners_ou",     # opp over
        ),
        forbidden_market_keys=(),
        bet_shape_rule="subject_team_centric",
        max_legs_hint=5,
        base_confidence=0.8,
    ),
    Archetype(
        key="RETURNING_PLAYER",
        description=(
            "Player back in starting XI after injury / suspension. "
            "Anytime scorer + team attack live."
        ),
        match_keywords=(
            "returns", "back in the squad", "back in training",
            "fit again", "available for selection",
            "named in starting", "starts after",
        ),
        match_hooks=(HookType.TEAM_NEWS, HookType.INJURY,
                     HookType.PREVIEW),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.returning_from_layoff",
            "player.{p}.starting_confirmed",
            "player.{p}.active",
        ),
        preferred_market_keys=(
            "anytime_goalscorer",
            "player_to_score_or_assist",
            "player_over_shots",
            "team_total_goals_ou",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="subject_centric",
        max_legs_hint=4,
        base_confidence=0.8,
    ),
    Archetype(
        key="MANAGER_PRESSURE",
        description=(
            "Manager on the brink — must win, sack watch. Bet orbits "
            "team result + dominance markers."
        ),
        match_keywords=(
            "sack watch", "must win", "under pressure", "job on the line",
            "future uncertain", "boardroom doubt", "fans turning",
            "win or sacked",
        ),
        match_hooks=(HookType.MANAGER_QUOTE, HookType.PREVIEW,
                     HookType.ARTICLE),
        subject_type=SUBJECT_MANAGER,
        signal_templates=(
            "manager.{team}.under_pressure",
            "team.{team}.must_win_pressure",
        ),
        preferred_market_keys=(
            "match_result",
            "asian_handicap",
            "team_to_win_to_nil",
            "team_clean_sheet",
            "team_total_goals_ou",
            "double_chance",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="subject_team_centric",
        max_legs_hint=4,
        base_confidence=0.75,
    ),
    Archetype(
        key="TACTICAL_HIGH_PRESS",
        description=(
            "Manager promising aggressive press / open play. First-half "
            "tempo + corners + cards."
        ),
        match_keywords=(
            "high press", "press from minute one", "front foot",
            "aggressive start", "go for the throat", "from kickoff",
            "intensity from", "full-throttle",
        ),
        match_hooks=(HookType.TACTICAL, HookType.MANAGER_QUOTE,
                     HookType.PREVIEW),
        subject_type=SUBJECT_TEAM,
        signal_templates=(
            "team.{team}.high_press",
            "tempo.first_half.high",
            "tempo.high",
            "set_pieces.heavy",
            "discipline.heavy",
        ),
        preferred_market_keys=(
            "first_half_total_goals_ou",
            "corners_ft_ou",
            "team_total_corners_ou",
            "cards_ft_ou",
            "total_goals_ou",
            "first_half_1x2",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="match_centric",
        max_legs_hint=5,
        base_confidence=0.8,
    ),
    Archetype(
        key="TACTICAL_LOW_BLOCK",
        description=(
            "Defensive setup, park-the-bus. Unders + clean sheet + "
            "low cards."
        ),
        match_keywords=(
            "park the bus", "low block", "defensive setup",
            "shut up shop", "compact shape", "deep defensive line",
            "absorb pressure", "counter-attack",
        ),
        match_hooks=(HookType.TACTICAL, HookType.MANAGER_QUOTE,
                     HookType.PREVIEW),
        subject_type=SUBJECT_TEAM,
        signal_templates=(
            "team.{team}.low_block",
            "tempo.low",
            "goals.low",
            "clean_sheet.{team}",
        ),
        preferred_market_keys=(
            "total_goals_ou",
            "both_teams_to_score",
            "team_clean_sheet",
            "double_chance",
            "draw_no_bet",
            "first_half_total_goals_ou",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="match_centric",
        max_legs_hint=4,
        base_confidence=0.75,
    ),
    Archetype(
        key="DERBY_INTENSITY",
        description=(
            "Local-rivalry framing. Cards + tempo + first-half "
            "competitive markers; goals can go either way."
        ),
        match_keywords=(
            "derby", "rivalry", "local pride", "needle in this fixture",
            "old enemy", "bragging rights", "north vs", "city rivals",
        ),
        match_hooks=(HookType.PREVIEW, HookType.TACTICAL,
                     HookType.ARTICLE, HookType.MANAGER_QUOTE),
        subject_type=SUBJECT_MATCH,
        signal_templates=(
            "derby_intensity",
            "discipline.heavy",
            "physicality.high",
            "tempo.first_half.high",
        ),
        preferred_market_keys=(
            "cards_ft_ou",
            "cards_first_half_ou",
            "first_half_1x2",
            "total_match_fouls",
            "both_teams_to_score",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="match_centric",
        max_legs_hint=4,
        base_confidence=0.7,
    ),
    Archetype(
        key="SET_PIECE_THREAT",
        description=(
            "Player named as set-piece danger / corner specialist. "
            "Corners + headed goal + that player to score."
        ),
        match_keywords=(
            "set-piece threat", "aerial threat", "corner specialist",
            "set-piece routine", "headed goals", "free-kick specialist",
            "dead-ball expert",
        ),
        match_hooks=(HookType.PREVIEW, HookType.TACTICAL,
                     HookType.MANAGER_QUOTE, HookType.TEAM_NEWS),
        subject_type=SUBJECT_PLAYER,
        signal_templates=(
            "player.{p}.set_piece_specialist",
            "set_pieces.heavy",
        ),
        preferred_market_keys=(
            "anytime_goalscorer",
            "player_to_score_with_header",
            "corners_ft_ou",
            "team_total_corners_ou",
        ),
        forbidden_market_keys=(),
        bet_shape_rule="subject_centric",
        max_legs_hint=4,
        base_confidence=0.75,
    ),
)


ARCHETYPE_BY_KEY = {a.key: a for a in ARCHETYPES}


# ── Detection ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchetypeMatch:
    """Result of matching a news item against the archetype set."""
    primary: Optional[Archetype]
    confidence: float
    alternatives: tuple[tuple[Archetype, float], ...] = ()
    matched_keywords: tuple[str, ...] = ()


def _keyword_score(text: str, keywords: tuple[str, ...]) -> tuple[int, list[str]]:
    """Return (hit_count, matched_keyword_strings) for a case-insensitive
    substring scan. A keyword counts at most once."""
    text_lc = text.lower()
    hits = []
    for kw in keywords:
        if kw.lower() in text_lc:
            hits.append(kw)
    return (len(hits), hits)


def derive_archetype(news: NewsItem) -> ArchetypeMatch:
    """Score every archetype against the news; return primary + alternatives.

    Score = (keyword hits) * 0.25 + (hook in match_hooks ? 0.4 : 0) +
            base_confidence_seed.

    Confidence is min(1.0, score). When NO archetype scores above 0.5
    we return primary=None — the caller should log
    `[narrative_uncertain]` and fall back to today's themes (or wait
    for the LLM second opinion in the next PR).
    """
    text = (news.headline or "") + " " + (news.summary or "")
    if not text.strip():
        return ArchetypeMatch(primary=None, confidence=0.0)
    hook_match = news.hook_type if news.hook_type else None
    scored: list[tuple[Archetype, float, list[str]]] = []
    for arch in ARCHETYPES:
        hit_count, matched_kws = _keyword_score(text, arch.match_keywords)
        # Hook gate: if archetype lists hooks, news.hook must be in them
        # to be considered. If no hooks listed, any hook is acceptable.
        if arch.match_hooks and hook_match not in arch.match_hooks:
            continue
        if hit_count == 0:
            continue
        score = (
            min(0.6, hit_count * 0.25)        # keyword contribution capped
            + (0.3 if arch.match_hooks else 0.0)
            + max(0.0, arch.base_confidence - 0.6)  # archetype prior
        )
        score = min(1.0, score)
        scored.append((arch, score, matched_kws))
    if not scored:
        return ArchetypeMatch(primary=None, confidence=0.0)
    scored.sort(key=lambda t: -t[1])
    primary, conf, kws = scored[0]
    alts = tuple((a, s) for a, s, _ in scored[1:5])
    return ArchetypeMatch(
        primary=primary,
        confidence=conf,
        alternatives=alts,
        matched_keywords=tuple(kws),
    )


def llm_second_opinion_hook(news: NewsItem,
                              rule_match: ArchetypeMatch) -> ArchetypeMatch:
    """Reserved hook for LLM-based archetype detection (next PR).

    Today: returns rule_match unchanged. Next PR will call Haiku when
    `rule_match.confidence < 0.5` to either propose an alternative
    archetype or confirm "no archetype matches — file as
    UNCATEGORISED". The LLM's verdict is logged and reviewed; over
    time we promote new patterns into `ARCHETYPES`.
    """
    return rule_match
