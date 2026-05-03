"""Narrative thesis — `news → (subject, archetype, signals)` derivation.

The thesis is the structured representation of "what is this story
about and what does it imply about the match." It carries:

  * the detected archetype (with confidence + alternatives)
  * the resolved subject (player_id / team_id / manager-team_id /
    match)
  * the set of resolved signals (`{p}` / `{team}` placeholders filled)
  * an `is_uncertain` flag so the caller can log
    `[narrative_uncertain]` and decide whether to fall back

Pure-function module — takes a `NewsItem`, returns a `NarrativeThesis`.
No persistence, no engine wiring; the composer consumes the thesis,
telemetry persists the decision separately.

## Self-learning hooks (built in, dormant for now)

  * `alternatives` field captures the top-5 also-rans so we can
    measure "did our primary pick actually align with engagement?"
  * `is_uncertain` flag triggers an LLM second opinion in the next PR
    via `narrative_archetypes.llm_second_opinion_hook` (today: no-op)
  * `confidence` is logged with every published card so we can train
    matcher rules against engagement data
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.engine import narrative_signals
from app.engine.narrative_archetypes import (
    Archetype,
    ArchetypeMatch,
    SUBJECT_MANAGER,
    SUBJECT_MATCH,
    SUBJECT_PLAYER,
    SUBJECT_TEAM,
    derive_archetype,
    llm_second_opinion_hook,
)
from app.models.news import NewsItem


UNCERTAINTY_THRESHOLD = 0.5


@dataclass(frozen=True)
class NarrativeThesis:
    """Structured "what's this story about" output for the composer."""
    news_item_id: str
    archetype: Optional[Archetype]
    confidence: float
    alternatives: tuple[tuple[str, float], ...]  # (archetype_key, score)
    subject_type: str                              # SUBJECT_*
    subject_player_id: Optional[str] = None
    subject_team_id: Optional[str] = None
    subject_player_name: Optional[str] = None
    fixture_id: Optional[str] = None
    resolved_signals: tuple[str, ...] = ()
    matched_keywords: tuple[str, ...] = ()
    is_uncertain: bool = False


def _resolve_subject(news: NewsItem,
                     arch: Archetype) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve subject_player_id, subject_team_id, subject_player_name.

    Heuristic:
      * SUBJECT_PLAYER → first entry in news.position_data with a known
        player_name, fallback to first mention
      * SUBJECT_TEAM / SUBJECT_MANAGER → first team_id from news.team_ids
      * SUBJECT_MATCH → no subject ids
    """
    if arch.subject_type == SUBJECT_PLAYER:
        # Prefer injury_details which is structured (player_name + team).
        idata = getattr(news, "injury_details", None) or []
        if idata and isinstance(idata, list) and idata[0].get("player_name"):
            entry = idata[0]
            # We don't have player_id directly — resolution to id happens
            # downstream by composer / market_meta. Keep name + team for
            # now.
            return (None, None, entry["player_name"])
        if news.mentions:
            return (None, None, news.mentions[0])
        return (None, None, None)
    if arch.subject_type in (SUBJECT_TEAM, SUBJECT_MANAGER):
        if news.team_ids:
            return (None, news.team_ids[0], None)
        return (None, None, None)
    return (None, None, None)


def build_thesis(news: NewsItem) -> NarrativeThesis:
    """Derive a NarrativeThesis from a NewsItem.

    Resolves placeholders in archetype signal templates using the
    detected subject. Sets `is_uncertain=True` when no archetype
    scored above `UNCERTAINTY_THRESHOLD` — the caller logs this and
    can request the LLM second opinion (no-op in this PR).
    """
    match: ArchetypeMatch = derive_archetype(news)
    match = llm_second_opinion_hook(news, match)

    if match.primary is None or match.confidence < UNCERTAINTY_THRESHOLD:
        return NarrativeThesis(
            news_item_id=news.id,
            archetype=match.primary,
            confidence=match.confidence,
            alternatives=tuple(
                (a.key, s) for a, s in match.alternatives
            ),
            subject_type=SUBJECT_MATCH,
            fixture_id=(news.fixture_ids[0] if news.fixture_ids else None),
            is_uncertain=True,
            matched_keywords=match.matched_keywords,
        )

    arch = match.primary
    sub_player_id, sub_team_id, sub_player_name = _resolve_subject(news, arch)

    # Resolve signal templates. For per-team templates without a
    # team_id (subject is SUBJECT_MATCH or unresolved), we drop those
    # signals rather than silently emit unfilled ones.
    resolved: list[str] = []
    for tmpl in arch.signal_templates:
        try:
            # player_id is the SUBJECT player; for KEY_*_OUT archetypes the
            # `{p}` placeholder is just a marker — we use player NAME as
            # a stable key for now (id resolution comes later via the
            # entity_resolver module).
            player_id = sub_player_id or sub_player_name
            sig = narrative_signals.resolve(
                tmpl, team_id=sub_team_id, player_id=player_id,
            )
            resolved.append(sig)
        except ValueError:
            continue

    return NarrativeThesis(
        news_item_id=news.id,
        archetype=arch,
        confidence=match.confidence,
        alternatives=tuple((a.key, s) for a, s in match.alternatives),
        subject_type=arch.subject_type,
        subject_player_id=sub_player_id,
        subject_team_id=sub_team_id,
        subject_player_name=sub_player_name,
        fixture_id=(news.fixture_ids[0] if news.fixture_ids else None),
        resolved_signals=tuple(resolved),
        matched_keywords=match.matched_keywords,
        is_uncertain=False,
    )
