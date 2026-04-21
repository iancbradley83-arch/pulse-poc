"""NarrativeRewriter — journalist voice pass over scouted news.

Two-step pipeline:

  Scout (news_ingester):  Haiku + web_search gathers raw facts as structured JSON
                          — headline/summary/hook_type/mentions. Prioritises
                          speed + recall, not voice.

  Copywriter (here):      Sonnet rewrites the raw scout output into card-ready
                          copy with a strong journalist voice: punchy headline,
                          one-sentence betting angle, active verbs, no
                          wire-service tics. Runs on published candidates only
                          so below-threshold candidates don't burn tokens.

The rewriter's system prompt is prompt-cached; a typical run rewrites ~50
candidates and most requests hit the cache. On failure we fall back to the
scout's raw headline/summary so the pipeline never blocks.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.models.news import CandidateCard, NewsItem
from app.models.schemas import CardLeg, Game, Market

logger = logging.getLogger(__name__)


VOICE_BRIEF = """You are the senior copywriter for Pulse, a news-driven sports
betting feed. You rewrite scout-gathered news into punchy, voice-forward card
copy that feels like a sharp sports journalist wrote it — not a wire service,
not a corporate sportsbook, not a tabloid.

Every card has ONE job: make a bettor look at a specific market and think
"I hadn't thought of it that way". The news is the hook; the market is the
action.

You are NOT writing:
  - Wire-service prose ("per sources", "it was confirmed", "announced today")
  - Corporate sportsbook copy ("back the Gunners today!")
  - Clickbait tabloid ("SHOCK as star RULED OUT!!")
  - Uncritical summary of a press conference

You ARE writing:
  - Sharp, confident, active-voice lines
  - Sports-fluent — the language of the terraces and the broadcast booth
  - Point of view — imply stakes, don't just recite facts
  - Implications for THIS market — what does this news mean for this bet?

VOICE RULES

Headline:
  - 6 to 10 words, hard ceiling
  - Active voice, always
  - Subject-verb-object or a dramatic em-dash beat
  - No wire-service openings (`Per sources…`, `It was announced…`, `Confirmed:…`)
  - No `despite`, `amid`, `with` as sentence-openers
  - No exclamation points, no all-caps words, no emoji
  - Put the KEY FACT first, not the attribution

Angle (the one-sentence body under the headline):
  - ONE sentence, maximum 25 words
  - Connect the news directly to the market stake
  - No `could potentially`, `might be expected to`, `is said to`
  - Don't frame with "this could mean" — just say what it means
  - Use a specific number when it sharpens the line (goals, form, odds, streak)
  - End on the implication, not the source

FORBIDDEN PHRASES (never use — automatic fail)
  "per sources", "it was announced", "confirmed today", "the manager said",
  "in a press conference", "amid speculation", "according to reports",
  "could potentially", "might be expected to", "is said to be",
  "sources close to", "understood to be"

BET BUILDER MODE

When the input includes a `legs` block, the card is a multi-leg Bet Builder.
Treat all legs as one package and write an angle that justifies the STACK,
not any individual leg. Name-drop the total odds when it sharpens the line.
Keep to the same 25-word ceiling.

  BB RAW → {injury to Palmer, legs=[Brighton win 2.50, Under 2.5 Goals 1.98,
           BTTS No 2.10], total=10.40}
  REWRITE →
    headline: Palmer out — Brighton tighten the grip on the whole evening
    angle: No creativity, no goals; stack Brighton + Under + BTTS No at 10.4
    and the whole game falls on a knock in training.

CALIBRATION EXAMPLES (SINGLES)

  RAW → "Bournemouth officially announced Marco Rose as successor to departing
  Andoni Iraola. Focus must remain on European qualification push despite
  managerial transition."
  REWRITE →
    headline: Cherries lock in Rose — Iraola's farewell tour rolls on
    angle: Twelve unbeaten before the announcement, and the squad are playing
    for the exit; the European push stays firmly in focus.

  RAW → "Atletico Madrid players Lookman and Sorloth picked up injuries in the
  Copa del Rey final. Both train separately. Neither expected to feature
  against Elche."
  REWRITE →
    headline: Atletico lose Lookman and Sorloth — Elche smell blood
    angle: Simeone's attack gutted a week before the Champions League semi;
    Elche at home suddenly look a live underdog.

  RAW → "Chelsea manager Enzo Maresca told reporters midfielder Cole Palmer is
  a doubt with a knock picked up in training."
  REWRITE →
    headline: Palmer doubtful — Chelsea's only spark walks out
    angle: No Palmer, no creativity; Chelsea's away-day scoring rate without
    him this season tells the whole story.

  RAW → "Girona have an extensive injury list including seven players."
  REWRITE →
    headline: Girona fielding seven absentees — still unbeaten at home
    angle: Seven out and Michel keeps winning; Under 2.5 Goals has been the
    pattern when the squad runs this thin.

  RAW → "Valverde says the Osasuna clash is vital. Athletic face four defeats
  in five matches and are six points from both European spots and relegation."
  REWRITE →
    headline: Valverde calls it vital — Bilbao's season hinges here
    angle: Four defeats in five and six points from both ends of the table;
    no room left for a flat Athletic display.

INPUT YOU RECEIVE (plain-text fields, newline-separated)
  source, hook_type, raw_headline, raw_summary, home, away, league, kickoff,
  market_label, pick, odds, and (when bet-builder) legs + total

OUTPUT
  Call the `submit_rewrite` tool exactly once with { headline, angle }.
  Both fields are PLAIN TEXT — no HTML, no <cite> tags, no markdown, no emoji.
  If the raw input is too vague to rewrite well, return the scout's raw
  headline verbatim and a one-sentence angle that at minimum names the market
  stake."""


REWRITE_TOOL: dict[str, Any] = {
    "name": "submit_rewrite",
    "description": "Submit the rewritten headline and angle for this card. "
                   "Call exactly once, after reading the input block.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "6-10 word headline"},
            "angle":    {"type": "string", "description": "One sentence, <= 25 words"},
        },
        "required": ["headline", "angle"],
        "additionalProperties": False,
    },
}


class NarrativeRewriter:
    """Rewrites scouted news into journalist-voice card copy.

    Uses Sonnet by default (cost per rewrite ~$0.002, prose quality is the
    bottleneck). Haiku works in a pinch — override `model` at construction.
    """

    def __init__(self, client: AsyncAnthropic, model: str = "claude-sonnet-4-6"):
        self._client = client
        self._model = model

    async def rewrite(
        self,
        *,
        news: NewsItem,
        market: Optional[Market],
        game: Optional[Game],
        candidate: CandidateCard,
        legs: Optional[list[CardLeg]] = None,
        total_odds: Optional[float] = None,
    ) -> Optional[dict[str, str]]:
        pick_label = ""
        pick_odds: Optional[float] = None
        market_label = ""
        if market and market.selections:
            market_label = market.label
            sel = market.selections[0]   # same selection the renderer shows as the "Pulse Pick"
            pick_label = sel.label
            try:
                pick_odds = float(sel.odds)
            except Exception:
                pick_odds = None

        legs_block = ""
        if legs:
            pretty = [f"{leg.market_label or '?'} · {leg.label} @ {leg.odds:.2f}" for leg in legs]
            legs_block = "legs:\n  - " + "\n  - ".join(pretty) + "\n"
            if total_odds:
                legs_block += f"total: {total_odds:.2f}\n"

        user_block = (
            f"source: {news.source_name or news.source or 'unknown'}\n"
            f"hook_type: {news.hook_type.value}\n"
            f"raw_headline: {news.headline}\n"
            f"raw_summary: {news.summary}\n"
            f"home: {game.home_team.name if game else '?'}\n"
            f"away: {game.away_team.name if game else '?'}\n"
            f"league: {game.broadcast if game else '?'}\n"
            f"kickoff: {game.start_time if game else '?'}\n"
            f"market_label: {market_label or '?'}\n"
            f"pick: {pick_label or '?'}\n"
            f"odds: {pick_odds if pick_odds is not None else '?'}\n"
            f"{legs_block}"
        )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=400,
                system=[{
                    "type": "text",
                    "text": VOICE_BRIEF,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[REWRITE_TOOL],
                tool_choice={"type": "tool", "name": "submit_rewrite"},
                messages=[{"role": "user", "content": user_block}],
            )
        except Exception as exc:
            logger.warning("NarrativeRewriter call failed: %s", exc)
            return None

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_rewrite":
                inp = block.input if isinstance(block.input, dict) else {}
                headline = _clean(inp.get("headline"))
                angle = _clean(inp.get("angle"))
                if headline:
                    return {"headline": headline, "angle": angle}
        return None


def _clean(val: Any) -> str:
    if not val:
        return ""
    import re as _re
    out = _re.sub(r"<[^>]+>", "", str(val))
    return _re.sub(r"\s+", " ", out).strip()
