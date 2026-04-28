"""NarrativeRewriter — journalist voice pass over scouted news.

Two-step pipeline:

  Scout (news_ingester):  Haiku + web_search gathers raw facts as structured JSON
                          — headline/summary/hook_type/mentions. Prioritises
                          speed + recall, not voice.

  Copywriter (here):      Haiku rewrites the raw scout output into card-ready
                          copy with a strong journalist voice: punchy headline,
                          one-sentence betting angle, active verbs, no
                          wire-service tics. Runs on published candidates only
                          so below-threshold candidates don't burn tokens.

The rewriter's system prompt is prompt-cached (`cache_control: ephemeral`).
A typical run rewrites ~50 candidates and most requests hit the cache. On
failure we fall back to the scout's raw headline/summary so the pipeline
never blocks. Cost-aware redesign (2026-04-26) moved this from Sonnet to
Haiku — voice quality is good enough for the POC at 1/15th the price.
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Optional

from anthropic import AsyncAnthropic

from app.engine._price_scrub import strip_prices
from app.models.news import CandidateCard, NewsItem
from app.models.schemas import CardLeg, Game, Market

if TYPE_CHECKING:
    from app.services.candidate_store import CandidateStore

logger = logging.getLogger(__name__)


def _cache_key(
    *,
    bet_type: str,
    hook_type: str,
    headline: str,
    legs: Optional[list[CardLeg]],
    news_mentions: Optional[list[str]] = None,
) -> str:
    """SHA256 over the canonicalised inputs that feed the rewriter prompt.

    Keyed on the THESIS (bet_type, hook_type, scout headline, leg market
    identities, news player mentions), NOT the price. Total odds was the
    cache-busting bug pre-redesign — SSE pricing ticks change total_odds on
    every leg-odds update so the key never matched across cycles. The
    rewriter prompt already bans odds in copy, so the price genuinely is
    irrelevant to what the model produces.

    `legs_csv` is sorted selection_ids pipe-joined so leg order doesn't
    affect the hash. `news_mentions` is sorted-lower-cased to harden the
    key against trivial casing churn.
    """
    if legs:
        legs_csv = "|".join(sorted(str(leg.selection_id) for leg in legs))
    else:
        legs_csv = ""
    if news_mentions:
        mentions_csv = "|".join(sorted(m.strip().lower() for m in news_mentions if m))
    else:
        mentions_csv = ""
    raw = f"{bet_type}|{hook_type}|{headline}|{legs_csv}|{mentions_csv}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


VOICE_BRIEF = """You are the senior copywriter for Pulse, a news-driven sports
betting feed. You rewrite scout-gathered news into punchy, voice-forward card
copy that feels like a sharp sports journalist wrote it — not a wire service,
not a corporate sportsbook, not a tabloid.

HAIKU VOICE GUIDANCE (READ FIRST)
  Be direct. Short, punchy lines. Headlines are 1 sentence, 6-10 words.
  Angles are 1 sentence, max 25 words / 200 characters. Active voice
  always. Subject-verb-object. Pick a real fact and lean on it; do not
  hedge. If you are tempted to qualify ("perhaps", "may", "could"),
  delete the qualifier and commit to the line.

PULSE IS AN ANGLE, NOT A PICK
Pulse is NOT a tipster service. Each card frames a market as the natural way
to play a real-world story — "if you noticed this news, here's the angle".
We do NOT predict outcomes. We do NOT tell the user to back something. We
present the market AS the story so the user becomes part of it.

The audience is an engaged casual bettor: knows the league, follows the
news, places a few bets a weekend. They want to feel insightful and they
hate the feeling of being late on a story. Write for someone who saw the
news on Twitter this morning and wants the betting angle a smart friend
would point at.

You are NOT writing:
  - Wire-service prose ("per sources", "it was confirmed", "announced today")
  - Tipster / prediction copy ("back the Gunners today!", "we like the
    over", "go big on Palmer", "this one's a lock")
  - Clickbait tabloid ("SHOCK as star RULED OUT!!")
  - Uncritical summary of a press conference

You ARE writing:
  - Sharp, confident, active-voice lines that name the angle
  - Sports-fluent — the language of the terraces and the broadcast booth
  - Point of view — imply stakes, don't just recite facts
  - The CONNECTION between the news and the market: "X happened → this
    market is now where the story plays out"

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
  Wire-service tics:
    "per sources", "it was announced", "confirmed today", "the manager said",
    "in a press conference", "amid speculation", "according to reports",
    "could potentially", "might be expected to", "is said to be",
    "sources close to", "understood to be"
  Tipster / prediction language (we are an angle, NOT a pick):
    "back the X", "we like the X", "go with X", "smart money on X",
    "lock", "banker", "free pick", "this one's calling", "good value here"

PRICE / ODDS RULE — HARD BAN
  Do NOT include any numeric odds, multipliers, or price references in the
  headline or angle. Describe the story and the markets in WORDS only. The
  UI renders every leg's price separately and re-quotes live; embedding a
  number in your copy makes the text drift against the displayed price.
  Never write:
    "at N.NN", "pays N.NN", "odds of N.NN", "stacks at N.NN",
    "stacked at N.NN", "priced at N.NN", "N.NN decimal", "N-to-N",
    "@ N.NN", "— N.NN", "in total pays N.NN"
  DO keep real-world numerics unrelated to price: goal counts ("14 goals
  this season"), scorelines ("2-1 loss"), streaks ("four wins in five"),
  thresholds ("3+ goals", "Over 2.5"), minutes ("85th minute"), league
  positions ("17th").

BET BUILDER MODE

When the input includes a `legs` block, the card is a multi-leg Bet Builder.
Treat all legs as one package and write an angle that justifies the STACK,
not any individual leg. Describe the markets in WORDS — do not quote the
stack's price or any individual leg's price. The UI shows every price
separately; the narrative is there to frame the story, not re-state the
odds. Keep to the same 25-word ceiling.

  BB RAW → {injury to Palmer, legs=[Brighton win, Under 2.5 Goals, BTTS No]}
  REWRITE →
    headline: Palmer out — Brighton tighten the grip on the whole evening
    angle: No creativity, no goals; Brighton + Under + BTTS No turns the whole
    game on a knock in training.

PLAYER-LED BB MODE (NEW — read carefully)

When the input includes a `lead_player` field, the BB is built around an
anytime-scorer leg for that named player. The card's whole story is "this
specific player is the angle". The headline MUST contain the player's name
(or their surname). Generic headlines that omit the player name are wrong.

  PLAYER BB RAW → {team_news, lead_player: "Frenkie de Jong",
                   legs=[Goalscorer · Frenkie de Jong,
                         FT 1X2 · Barcelona,
                         Total Goals O/U · Over 2.5]}
  REWRITE →
    headline: De Jong returns — Barcelona's engine room fires up
    angle: De Jong back from suspension; Barcelona to win + Over 2.5 + the man
    himself to find the net.

  PLAYER BB RAW → {transfer, lead_player: "Cole Palmer",
                   legs=[Goalscorer · Cole Palmer, FT 1X2 · Chelsea]}
  REWRITE →
    headline: Palmer leads the line — Chelsea ride him to the win
    angle: New role, same edge; Palmer to score and Chelsea to win, stacked
    in the slip.

  WRONG (player name dropped) → "Farke tips Bournemouth — his Leeds walk into a trap"
  RIGHT (player named)        → "Farke trusts James — Leeds attack runs through him"

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
  market_label, pick, odds, and (when bet-builder) legs and (sometimes)
  lead_player. When `lead_player` is present, the headline must name them.

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

    Defaults to Haiku 4.5 with prompt caching (cost-aware redesign,
    2026-04-26). Sonnet is OFF for the POC; the system prompt is heavy
    enough (~2k tokens) for Anthropic prompt caching to absorb the input
    cost across the cycle.

    When `store` and `cache_enabled` are both set, rewrites are memoised
    by SHA256(bet_type|hook_type|headline|legs_csv|mentions_csv) for
    `cache_ttl_seconds` (default 24h). Identical candidates across reruns
    skip the LLM call entirely. `cache_hits` / `cache_misses` are
    per-instance counters the orchestrator reads at end-of-cycle for the
    summary log line.

    Optional `cost_tracker` short-circuits the LLM call when the daily
    LLM budget is exhausted; on short-circuit `rewrite()` returns None
    and the caller falls back to the scout's raw copy.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str = "claude-haiku-4-5",
        *,
        store: "Optional[CandidateStore]" = None,
        cache_enabled: bool = True,
        cache_ttl_seconds: float = 86400.0,
        cost_tracker: "Optional[Any]" = None,
    ):
        self._client = client
        self._model = model
        self._store = store
        self._cache_enabled = bool(cache_enabled and store is not None)
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._cost_tracker = cost_tracker
        self.cache_hits = 0
        self.cache_misses = 0

    def reset_cache_counters(self) -> None:
        """Clear per-cycle cache counters (called by the orchestrator)."""
        self.cache_hits = 0
        self.cache_misses = 0

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
        # ── Cache lookup. Hash the same THESIS inputs that determine the
        # rewriter output; on hit, return the cached pair without touching
        # the API. total_odds is NOT in the key (cost-aware redesign):
        # SSE pricing ticks bumped it on every cycle, the cache never
        # matched, and Sonnet ran every time. The thesis is what matters.
        mentions: list[str] = []
        try:
            for m in (news.mentions or []):
                if isinstance(m, str) and m.strip():
                    mentions.append(m)
        except Exception:
            mentions = []
        cache_key = _cache_key(
            bet_type=candidate.bet_type.value,
            hook_type=candidate.hook_type.value,
            headline=news.headline or "",
            legs=legs,
            news_mentions=mentions,
        )
        if self._cache_enabled and self._store is not None:
            try:
                cached = await self._store.get_rewrite_cache(
                    cache_key, max_age_seconds=self._cache_ttl_seconds,
                )
            except Exception as exc:
                logger.warning("NarrativeRewriter cache read failed: %s", exc)
                cached = None
            if cached and cached.get("headline"):
                self.cache_hits += 1
                logger.debug(
                    "[NarrativeRewriter] rewrite_cache_hit key=%s...", cache_key[:12],
                )
                # Persist the cache-hit to the daily counter so
                # /admin/cost.json?detail=1 can surface rewrite_cache_hits_today.
                if self._cost_tracker is not None:
                    try:
                        await self._cost_tracker.record_rewrite_cache_hit()
                    except Exception:
                        pass
                return {"headline": cached["headline"], "angle": cached.get("angle", "")}

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
        lead_player_block = ""
        if legs:
            pretty = [f"{leg.market_label or '?'} · {leg.label} @ {leg.odds:.2f}" for leg in legs]
            legs_block = "legs:\n  - " + "\n  - ".join(pretty) + "\n"
            # Include total_odds only when it's a real correlated/operator
            # price (caller passes None when it's just the naive product).
            if total_odds is not None and total_odds > 1.0:
                legs_block += f"total_odds: {total_odds:.2f}\n"
            # When a goalscorer leg is present its label IS the player name.
            # Surface it as an explicit `lead_player` field so the rewriter
            # can't drop it from the headline (the model is bad at inferring
            # "this leg is the story" from the legs block alone).
            for leg in legs:
                if (leg.market_label or "").strip().lower() == "goalscorer":
                    lead_player_block = f"lead_player: {leg.label}\n"
                    break
        elif market and market.market_type == "goalscorer" and market.selections:
            # Single-bet path: a player-matched goalscorer single carries
            # exactly one selection, the matched player.
            if len(market.selections) == 1:
                lead_player_block = f"lead_player: {market.selections[0].label}\n"

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
            f"{lead_player_block}"
            f"{legs_block}"
        )

        # Cost-tripwire short-circuit. Pre-call estimate uses the
        # configured Haiku rates with conservative input-token estimate
        # (system prompt is cached, but assume worst-case cache miss).
        if self._cost_tracker is not None:
            try:
                projected = self._cost_tracker.estimate_haiku_call(
                    input_tokens=2200, max_output_tokens=400, web_search=False,
                )
                if not await self._cost_tracker.can_spend(projected):
                    logger.info(
                        "[cost] rewrite skipped — daily LLM budget exhausted"
                    )
                    return None
            except Exception as exc:
                logger.warning(
                    "NarrativeRewriter cost-tracker check failed: %s", exc,
                )

        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=300,
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

        # Post-call cost accounting based on actual usage.
        if self._cost_tracker is not None:
            try:
                actual = self._cost_tracker.cost_from_usage(
                    getattr(resp, "usage", None), web_search=False,
                )
                await self._cost_tracker.record_call(
                    model=self._model, kind="rewrite", cost_usd=actual,
                )
            except Exception as exc:
                logger.warning(
                    "NarrativeRewriter cost-tracker record failed: %s", exc,
                )

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_rewrite":
                inp = block.input if isinstance(block.input, dict) else {}
                headline = strip_prices(_clean(inp.get("headline")))
                angle = strip_prices(_clean(inp.get("angle")))
                if headline:
                    # Cache miss path — store the freshly-generated rewrite
                    # under the same key we probed above. Failures here are
                    # non-fatal; we still return the rewrite.
                    if self._cache_enabled and self._store is not None:
                        self.cache_misses += 1
                        logger.debug(
                            "[NarrativeRewriter] rewrite_cache_miss key=%s...", cache_key[:12],
                        )
                        try:
                            await self._store.save_rewrite_cache(
                                key=cache_key,
                                headline=headline,
                                angle=angle,
                                model=self._model,
                            )
                        except Exception as exc:
                            logger.warning("NarrativeRewriter cache write failed: %s", exc)
                    return {"headline": headline, "angle": angle}
        return None


def _clean(val: Any) -> str:
    if not val:
        return ""
    import re as _re
    out = _re.sub(r"<[^>]+>", "", str(val))
    return _re.sub(r"\s+", " ", out).strip()
