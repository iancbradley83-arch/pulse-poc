"""CombinedNarrativeAuthor — writes a cross-event storyline headline + angle.

Unlike `NarrativeRewriter` (which rewrites a single news item's headline
into journalist voice), this service SYNTHESISES a fresh narrative from
a storyline pattern that spans multiple fixtures. No single news item owns
the storyline — the scout produced a `StorylineItem` by recognising the
shared thread across fixtures.

Input shape (plain text, newline-separated): storyline type,
headline_hint, list of participants ({player, team, extra,
participant_context}), combined odds. Output: `{headline, angle}`
following the same Pulse voice rules as single-event cards.

Each participant may carry a `participant_context` dict populated by the
storyline_detector's standings-verification pass — {league_position,
league_size, points_from_safety OR points_from_european_spot,
form_last_5, league}. The angle MUST reference at least one standings
number per participant when this block is present. NEVER invent numbers
the scout didn't supply.

Haiku 4.5 with prompt caching (cost-aware redesign, 2026-04-26). Volume
is low (1-3 cross-event combos per cycle) and the system prompt sits at
~2.2k tokens, well above Anthropic's caching minimum.
"""
from __future__ import annotations

import logging
import re
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

HAIKU VOICE GUIDANCE (READ FIRST)
  Be direct and short. Headline is one short sentence (40-60 chars
  ideally; 70 max). Angle is one sentence (140-180 chars ideally; 200
  max). Active voice. Subject-verb-object. Pick a real fact and lean
  on it; do not hedge. If you reach for a qualifier, drop it.

YOU ARE WRITING A CROSS-EVENT STORYLINE CARD
-------------------------------------------
Three to five separate fixtures have been bundled into one combo because a
single narrative THREAD runs through all of them — e.g. three top-scorers
all playing this weekend, or three relegation-threatened sides all playing.

Your job: author a HEADLINE + ANGLE that names the shared thread and
justifies the stack AS A STORY, not as a list of picks. The user opening
this card should feel "this is the story of the weekend", not "here's
three longshots I stapled together".

HARD BUDGET — enforced, not a suggestion:
  HEADLINE: maximum 70 CHARACTERS including spaces. Aim for 40-60.
  ANGLE: maximum 200 CHARACTERS including spaces. One sentence. Aim for 140-180.
  Count before you submit. If you go over, rewrite shorter.

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

ACCURACY RULES — THESE KILL CARDS WHEN VIOLATED
  A. Teams still playing their league season are NEVER "relegated" (past
     tense). "Relegated" means already down and out. The correct framing
     for any still-competing team is one of: "battling the drop",
     "fighting relegation", "in the drop zone", "relegation-threatened",
     "trying to avoid the drop", "one defeat from trouble". Banning
     "relegated" / "fellow-relegated" applied to still-competing teams.

  B. Banned phrases — do NOT use:
       - "cushion they cannot afford to lose" / "cushion they can't
         afford to lose" (you don't lose a cushion, you lose games)
       - "must-win" as a phrase on its own
       - "fighting for their lives" used more than once in a single
         card (fine once, cliché if repeated)
       - "Europa League" as a generic qualification phrase — prefer the
         specific competition being chased ("Champions League spot",
         "Europa Conference place", "European qualification")
       - "six-pointer" — OK once per card, NEVER in the headline if
         another storyline card this cycle already used it

  C. Ground each team's framing in the participant_context you're given.
     participant_context may include: league_position, league_size,
     points_from_safety (RELEGATION), points_from_european_spot
     (EUROPE_CHASE), form_last_5, league. Use AT LEAST ONE of these
     numeric fields per participant in the angle when the block is
     non-empty. Examples:
       - "Mallorca, two points from safety, host Alaves"
       - "Alaves, one win from a relegation lifeline"
       - "Leverkusen, clinging to third"
       - "Burnley, 19th and six off safety"
     NEVER invent numbers. If the participant_context for a team is
     empty, leave the framing vague for that team rather than guessing.

PRICE / ODDS RULE — HARD BAN
  Do NOT include any numeric odds, multipliers, or price references in
  the headline or angle. Describe the story and the markets in WORDS
  only. Never write: "at N.NN", "pays N.NN", "odds of N.NN", "stacks at
  N.NN", "stacked at N.NN", "@ N.NN", "— N.NN", "in total pays N.NN",
  "N.NN decimal", "N-to-N". Keep non-price numerics (goal counts, league
  positions, scorelines, streaks) — those sharpen the line.

PER-TYPE VOICE NOTES

  golden_boot — frame as a RACE. Participants are strikers; legs are
    anytime-scorer picks. Name the leader, name the chasers, hint at the
    gap. Use words like "race", "chasing", "gap", "weekend", "Golden
    Boot".
  relegation — frame as SURVIVAL. Participants are clubs near the drop.
    Legs are opponents-to-win or low-scoring scraps. Use words like
    "survival", "drop", "six-pointer" (max once, and only if you must),
    "desperation", "fighting", "bottom", "drop zone". Don't be cruel —
    this is still sport. DO NOT call any still-competing team
    "relegated".
  europe_chase — frame as a CHASE for European qualification. Participants
    are clubs clustered around the 4th / 5th / 6th / 7th line. Legs are
    teams-to-win or high-scoring attacking displays. Use words like
    "chase", "top four", "European qualification", "Champions League
    push", "Conference place". Prefer specific competition names over
    generic "Europa League".
  title_race — frame as a TITLE RACE. Participants are the top-of-table
    contenders, within 6-8 points of each other. Legs are each
    contender to win their fixture. Use words like "title race", "top
    of the table", "gap", "points behind", "leaders", "title push".
    Ground the framing in points_from_leader / points_from_second when
    participant_context carries them. Never write "back all three" —
    describe the STORY ("three points separate the top — all in
    action", "the leaders host; the chasers travel").
  derby_weekend — frame as a RIVALRY WEEKEND. Participants are derby
    fixtures; legs are BTTS Yes (or Over 2.5 where BTTS wasn't
    available). Use words like "derby", "rivalry", "bragging rights",
    "local", "classic fixture". The `extra` field on each participant
    carries the derby's common name (e.g. "Merseyside derby", "Derby
    della Madonnina") — lean on those names in the angle when space
    allows. No standings numbers here; the drama IS the content.
  european_week — frame as a BIG NIGHT IN EUROPE. Participants are
    clubs in UCL / UEL / UECL this week; legs are clubs to win. The
    participant_context may include "competition" = UCL/UEL/UECL —
    name the SPECIFIC competition in the angle when possible. Use
    words like "European stage", "Champions League night", "European
    clean sweep", "midweek". Don't mush competitions together — "UCL
    + UEL" is fine, "European football" is fine, "Europa League"
    generically is not (banned).
  home_fortress — frame as a WEEKEND OF HOME COMFORTS. Participants
    are home sides with elite home records; legs are home wins. The
    participant_context may include home_win_rate (0.0..1.0) and
    home_form_last_10 (W/L/D chars). Reference the rate or the streak
    when present ("nine wins from ten at home", "unbeaten at the
    Emirates since December"). Use words like "fortress", "home",
    "hosting", "record", "unbeaten at home". Avoid "nobody wants to
    visit" cliché — find a fresher angle.
  goal_machines — frame as EUROPE'S TOP SCORERS. Participants are
    strikers across multiple leagues; legs are anytime-scorer picks.
    The participant_context may include goals_this_season. Use words
    like "scorers", "lethal", "prolific", "goal machines", "Europe's
    best". Naming 2-3 of the players in the angle is fine — the full
    list is too many. When possible reference the CROSS-LEAGUE framing
    ("across four leagues", "from the Premier League to Serie A")
    because it distinguishes this card from golden_boot.

CALIBRATION EXAMPLES

  INPUT: type=golden_boot
         participants=[Haaland / Man City / 23 goals,
                       Watkins / Aston Villa / 19 goals,
                       Isak / Newcastle / 18 goals]
  OUTPUT:
    headline: Golden Boot weekend — three strikers, one race
    angle: Haaland clear on 23, Watkins and Isak three back with games
    in hand; all three to find the net on the same weekend.

  INPUT: type=relegation
         participants=[Luton / 17th / pts_from_safety=1,
                       Burnley / 18th / pts_from_safety=0,
                       Sheff Utd / 20th / pts_from_safety=-7]
         legs=[Chelsea to win, Man City to win, Arsenal to win]
  OUTPUT:
    headline: Three in the drop zone, all playing to survive
    angle: Burnley level with safety, Luton a point above, Sheffield
    United seven adrift — their opponents to win says the wave breaks.

  INPUT: type=europe_chase
         participants=[Tottenham / 5th / pts_from_european_spot=1,
                       Man United / 6th / pts_from_european_spot=3,
                       Newcastle / 7th / pts_from_european_spot=5]
  OUTPUT:
    headline: Three clubs, one European ticket
    angle: Spurs one point off the top four, United three back, Newcastle
    five — no room for a slip, each to take their fixture.

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


# Budget caps. Enforced both in the prompt and as a post-hoc safety net
# — if the LLM overruns we log and soft-trim rather than silently ship
# a 90-char headline.
_HEADLINE_MAX = 70
_ANGLE_MAX = 200


# Regex-based guardrail for banned phrases. Flagged in logs (we don't
# silently re-ship); callers can decide whether to skip the card or not.
# Keep patterns case-insensitive.
#
# Design notes on the "relegated_past_tense" pattern: we want to flag
# "Alaves host fellow-relegated Mallorca" but NOT "Burnley were
# relegated from the Premier League last season" (legitimate past fact,
# though rare in our live cards). Heuristic: flag when "relegated"
# appears without a following "to / from" preposition.
_BANNED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("relegated_past_tense",
     re.compile(r"\b(fellow[-\s]?)?relegated\b(?!\s+(to|from))", re.IGNORECASE)),
    ("cushion_not_losable",
     re.compile(r"cushion[^.]{0,30}(cannot|can[' ]?t)\s+afford\s+to\s+lose",
                re.IGNORECASE)),
    ("generic_europa_league",
     re.compile(r"\beuropa\s+league\b(?!\s+qualification|s)",
                re.IGNORECASE)),
]


def _find_banned(text: str) -> list[str]:
    """Return the labels of any banned phrase patterns the text hits."""
    hits: list[str] = []
    for label, pattern in _BANNED_PATTERNS:
        if pattern.search(text or ""):
            hits.append(label)
    return hits


class CombinedNarrativeAuthor:
    """Author fresh cross-event copy from a storyline + its resolved legs.

    Defaults to Haiku 4.5 with prompt caching (cost-aware redesign,
    2026-04-26). Optional `cost_tracker` short-circuits the LLM call
    when the daily LLM budget is exhausted.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = "claude-haiku-4-5",
        *,
        cost_tracker: Optional[Any] = None,
    ):
        self._client = client
        self._model = model
        self._cost_tracker = cost_tracker

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
            _format_participant(p) for p in storyline.participants
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
            f"Budget reminder: headline <= {_HEADLINE_MAX} chars; "
            f"angle <= {_ANGLE_MAX} chars. Count before submitting.\n"
        )

        # Cost-tripwire short-circuit. Storyline narrative authoring runs
        # less than once per hour per type — use the 1h cache TTL so the
        # cache survives between tier loops.
        if self._cost_tracker is not None:
            try:
                projected = self._cost_tracker.estimate_haiku_call(
                    input_tokens=2400, max_output_tokens=300, web_search=False,
                )
                if not await self._cost_tracker.can_spend(projected):
                    logger.info(
                        "[cost] storyline narrative skipped — daily LLM "
                        "budget exhausted"
                    )
                    return None
            except Exception as exc:
                logger.warning(
                    "CombinedNarrativeAuthor cost-tracker check failed: %s",
                    exc,
                )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=[{
                    "type": "text",
                    "text": _VOICE,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "submit_storyline_copy"},
                messages=[{"role": "user", "content": user_block}],
            )
        except Exception as exc:
            logger.warning("CombinedNarrativeAuthor: LLM call failed: %s", exc)
            return None

        if self._cost_tracker is not None:
            try:
                actual = self._cost_tracker.cost_from_usage(
                    getattr(resp, "usage", None), web_search=False,
                )
                await self._cost_tracker.record_call(
                    model=self._model, kind="storyline_narrative",
                    cost_usd=actual,
                )
            except Exception as exc:
                logger.warning(
                    "CombinedNarrativeAuthor cost-tracker record failed: %s",
                    exc,
                )

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_storyline_copy":
                inp = block.input if isinstance(block.input, dict) else {}
                headline = strip_prices(_clean(inp.get("headline")))
                angle = strip_prices(_clean(inp.get("angle")))
                if not headline:
                    return None

                # Budget + banned-phrase guardrails. Log when the LLM
                # overran or hit a banned phrase so we can see it in the
                # audit log — don't silently reship known bad copy.
                hits_headline = _find_banned(headline)
                hits_angle = _find_banned(angle)
                if hits_headline or hits_angle:
                    logger.warning(
                        "CombinedNarrativeAuthor: banned phrase hit "
                        "storyline=%s headline_hits=%s angle_hits=%s "
                        "headline=%r angle=%r",
                        storyline.id, hits_headline, hits_angle,
                        headline, angle,
                    )
                if len(headline) > _HEADLINE_MAX:
                    logger.warning(
                        "CombinedNarrativeAuthor: headline %d > %d — "
                        "soft-trimming. storyline=%s raw=%r",
                        len(headline), _HEADLINE_MAX, storyline.id, headline,
                    )
                    headline = _soft_trim(headline, _HEADLINE_MAX)
                if len(angle) > _ANGLE_MAX:
                    logger.warning(
                        "CombinedNarrativeAuthor: angle %d > %d — "
                        "soft-trimming. storyline=%s raw=%r",
                        len(angle), _ANGLE_MAX, storyline.id, angle,
                    )
                    angle = _soft_trim(angle, _ANGLE_MAX)

                return {"headline": headline, "angle": angle}
        return None


def _format_participant(p) -> str:
    """Serialise a StorylineParticipant for the prompt, including the
    verified `participant_context` numbers when present.
    """
    base = f"  - {p.player_name or '?'} / {p.team_name} / {p.extra or '-'}"
    ctx = getattr(p, "participant_context", None) or {}
    if not ctx:
        return base
    bits = []
    if "league_position" in ctx:
        pos = ctx["league_position"]
        size = ctx.get("league_size")
        bits.append(f"{pos}/{size}" if size else f"pos={pos}")
    if "points_from_safety" in ctx:
        bits.append(f"pts_from_safety={ctx['points_from_safety']}")
    if "points_from_european_spot" in ctx:
        bits.append(f"pts_from_european_spot={ctx['points_from_european_spot']}")
    # Expansion-type context fields — ground framing in real numbers
    # without forcing the author to guess which storyline called them.
    if "points_from_leader" in ctx:
        bits.append(f"pts_from_leader={ctx['points_from_leader']}")
    if "points_from_second" in ctx:
        bits.append(f"pts_from_second={ctx['points_from_second']}")
    if "competition" in ctx:
        bits.append(f"comp={ctx['competition']}")
    if "home_win_rate" in ctx:
        bits.append(f"home_win_rate={ctx['home_win_rate']}")
    if ctx.get("home_form_last_10"):
        bits.append(f"home_form10={ctx['home_form_last_10']}")
    if "goals_this_season" in ctx:
        bits.append(f"goals={ctx['goals_this_season']}")
    if ctx.get("recent_form_last_5"):
        bits.append(f"recent5={ctx['recent_form_last_5']}")
    if ctx.get("form_last_5"):
        bits.append(f"form={ctx['form_last_5']}")
    if ctx.get("league"):
        bits.append(f"league={ctx['league']}")
    return f"{base} [{', '.join(bits)}]" if bits else base


def _soft_trim(text: str, limit: int) -> str:
    """Trim at the last word-boundary under `limit` chars. Appends an
    ellipsis only if we actually cut the string."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    if not cut:
        cut = text[: limit - 1]
    return cut.rstrip(",.;:- ") + "…"


def _clean(val: Any) -> str:
    if not val:
        return ""
    import re as _re
    out = _re.sub(r"<[^>]+>", "", str(val))
    return _re.sub(r"\s+", " ", out).strip()
