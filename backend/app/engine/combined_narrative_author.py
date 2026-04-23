"""CombinedNarrativeAuthor — writes a cross-event storyline headline + angle.

Unlike `NarrativeRewriter` (which rewrites a single news item's headline
into journalist voice), this service SYNTHESISES a fresh narrative from
a storyline pattern that spans multiple fixtures. No single news item owns
the storyline — the scout produced a `StorylineItem` by recognising the
shared thread across fixtures.

Input shape (plain text, newline-separated): storyline type,
headline_hint, list of participants ({player, team, extra}), combined odds.
Output: `{headline, angle}` following the same Pulse voice rules as
single-event cards.

Sonnet by default — volume is low (1-3 cross-event combos per cycle).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.engine._price_scrub import strip_prices
from app.models.news import StorylineItem
from app.models.schemas import CardLeg

logger = logging.getLogger(__name__)


# Voice is the same Pulse house style as `NarrativeRewriter.VOICE_BRIEF`,
# with additions explaining cross-event synthesis. Kept in one string for
# prompt-cache simplicity.
_VOICE = """You are the senior copywriter for Pulse, a news-driven sports
betting feed. You write card copy that frames bets as the natural way to
PLAY a real-world story — never as a prediction or a pick.

PULSE IS AN ANGLE, NOT A PICK. Audience is engaged-casual: knows the
league, follows the news, places a few bets a weekend.

YOU ARE WRITING A CROSS-EVENT STORYLINE CARD
-------------------------------------------
Three to five separate fixtures have been bundled into one combo because a
single narrative THREAD runs through all of them — e.g. three top-scorers
all playing this weekend, or three relegation-threatened sides all playing.

Your job: author a HEADLINE + ANGLE that names the shared thread and
justifies the stack AS A STORY, not as a list of picks. The user opening
this card should feel "this is the story of the weekend", not "here's
three longshots I stapled together".

HEADLINE (6-10 words, hard ceiling)
  - Active voice, sharp, subject-verb-object
  - Name the shared thread — "Golden Boot race", "the relegation weekend",
    "three chasing fourth"
  - Do NOT list all the names in the headline — the angle covers details

ANGLE (ONE sentence, <= 25 words)
  - Connect the fixtures under one storyline
  - Reference the stakes or the shared pattern
  - Do NOT say "back", "pick", "lock", "free bet"
  - Do NOT say "per sources", "it was announced", "could potentially"

PRICE / ODDS RULE — HARD BAN
  Do NOT include any numeric odds, multipliers, or price references in
  the headline or angle. Describe the story and the markets in WORDS
  only. Never write: "at N.NN", "pays N.NN", "odds of N.NN", "stacks at
  N.NN", "stacked at N.NN", "@ N.NN", "— N.NN", "in total pays N.NN",
  "N.NN decimal", "N-to-N". Keep non-price numerics (goal counts, league
  positions, scorelines, streaks) — those sharpen the line.

CALIBRATION EXAMPLES

  INPUT: type=golden_boot
         participants=[Haaland / Man City / 23 goals,
                       Watkins / Aston Villa / 19 goals,
                       Isak / Newcastle / 18 goals]
  OUTPUT:
    headline: Golden Boot weekend — three strikers, one race
    angle: Haaland clear, Watkins and Isak three back with games in hand;
    all three to find the net on the same weekend.

  INPUT: type=relegation
         participants=[Luton / 17th, Burnley / 18th, Sheff Utd / 20th]
  OUTPUT:
    headline: Three at the bottom, all playing for survival
    angle: Luton, Burnley and the Blades are one bad afternoon from gone
    — their opponents to win says the wave breaks on them.

OUTPUT
  Call `submit_storyline_copy` exactly once with { headline, angle }.
  Plain text only — no HTML, no <cite> tags, no markdown, no emoji."""


_TOOL: dict[str, Any] = {
    "name": "submit_storyline_copy",
    "description": "Submit the synthesised headline and angle for this "
                   "cross-event storyline card. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "angle": {"type": "string"},
        },
        "required": ["headline", "angle"],
        "additionalProperties": False,
    },
}


class CombinedNarrativeAuthor:
    """Author fresh cross-event copy from a storyline + its resolved legs."""

    def __init__(self, client: AsyncAnthropic, model: str = "claude-sonnet-4-6"):
        self._client = client
        self._model = model

    async def author(
        self,
        *,
        storyline: StorylineItem,
        legs: list[CardLeg],
        total_odds: Optional[float],
    ) -> Optional[dict[str, str]]:
        if not legs:
            return None
        pretty_legs = "\n".join(
            f"  - {leg.market_label or '?'} · {leg.label} @ {leg.odds:.2f}"
            for leg in legs
        )
        pretty_parts = "\n".join(
            f"  - {p.player_name or '?'} / {p.team_name} / {p.extra or '-'}"
            for p in storyline.participants
        )
        total_line = (
            f"total_odds: {total_odds:.2f}\n"
            if total_odds is not None and total_odds > 1.0 else "total_odds: (unknown, do not invent)\n"
        )
        user_block = (
            f"storyline_type: {storyline.storyline_type.value}\n"
            f"headline_hint: {storyline.headline_hint}\n"
            f"participants:\n{pretty_parts}\n"
            f"legs:\n{pretty_legs}\n"
            f"{total_line}"
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=400,
                system=[{
                    "type": "text",
                    "text": _VOICE,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "submit_storyline_copy"},
                messages=[{"role": "user", "content": user_block}],
            )
        except Exception as exc:
            logger.warning("CombinedNarrativeAuthor: LLM call failed: %s", exc)
            return None

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_storyline_copy":
                inp = block.input if isinstance(block.input, dict) else {}
                headline = strip_prices(_clean(inp.get("headline")))
                angle = strip_prices(_clean(inp.get("angle")))
                if headline:
                    return {"headline": headline, "angle": angle}
        return None


def _clean(val: Any) -> str:
    if not val:
        return ""
    import re as _re
    out = _re.sub(r"<[^>]+>", "", str(val))
    return _re.sub(r"\s+", " ", out).strip()
