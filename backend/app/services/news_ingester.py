"""News ingester — per-fixture LLM + web-search scout.

For each upcoming fixture we want to surface content for, this service asks
Claude Haiku 4.5 to find newsworthy items in the last 48 hours (injuries,
team news, transfers, manager quotes, tactical stories, previews) using the
native web_search tool, and returns a structured list via a terminal
`submit_news_items` tool call.

Prompt caching on the system block keeps the tools + system prefix cached
across fixtures in a run — the first fixture pays the ~1.25x write premium,
every subsequent fixture in the same run reads at ~0.1x.

The ingester is not aware of Rogue entities. The returned NewsItem carries
raw mention strings; EntityResolver turns those into fixture / team IDs
downstream.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.engine._price_scrub import strip_prices
from app.models.news import HookType, NewsItem
from app.services.candidate_store import CandidateStore

# Defensive cleanup — web_search sometimes injects <cite index="...">...</cite>
# markup which the scout can echo into the summary. Strip any HTML-like tag
# and collapse whitespace. Cheap regex, no external deps.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_copy(s: Any) -> str:
    if not s:
        return ""
    out = _TAG_RE.sub("", str(s))
    return _WS_RE.sub(" ", out).strip()

logger = logging.getLogger(__name__)


_HOOK_VALUES = [h.value for h in HookType if h not in (HookType.PRICE_MOVE, HookType.LIVE_MOMENT)]


SYSTEM_PROMPT = """You are a sports-betting content scout. You write for a
news-driven feed where every card answers the question "what just happened
that makes this market interesting right now?"

For a given soccer fixture, find newsworthy items from the last 48 hours that
change how a bettor should look at the match. You must:

1. Use the `web_search` tool to look up: injury reports, team news / starting
   XIs / suspensions, transfers, manager press-conference quotes, tactical
   previews, and breaking stories for the teams and key players involved.
2. After researching, call the `submit_news_items` tool exactly once with the
   structured list of findings. Do not write a free-text answer.

WRITING RULES — this is the whole job, read carefully:

**Headlines** — tight, punchy, active voice. Maximum 10 words. Lead with the
news, not the provenance. Think tabloid back-page, not wire-service byline.
  Good: "Saka back in full training — derby boost"
  Good: "Palmer a doubt, Chelsea scramble"
  Good: "Simeone switches to diamond — Atletico go aggressive"
  Bad:  "Ademola Lookman and Alexander Sorloth picked up injuries in Atletico
         Madrid's Copa del Rey final defeat to Real Sociedad on Saturday."
  Bad:  "Press conference: manager confirms starting XI."

**Summaries** — one bettor-facing sentence. Maximum 25 words. Say what this
means for a market, not what was said at a press conference. No preamble.
  Good: "First goal since the hamstring — 14 goals already this season and
         he's hungry on his return."
  Good: "Losing their main striker in a game expected to hinge on goals —
         Over 2.5 now looks generous."
  Bad:  "Per sources, the player completed his first full training session
         since suffering the injury on March 18, according to the manager's
         pre-match press conference..."

**Hook type** — pick the tightest match from: injury, team_news, transfer,
manager_quote, tactical, preview, article, other. Each news item is ONE hook.

**Mentions** — concrete player / team / coach names that appear in the story.
Used by downstream entity resolution. Include both short and full forms
("Saka", "Bukayo Saka", "Arsenal").

**Injury details (for injury / team_news items only)** — when a player is
OUT, SUSPENDED, DOUBTFUL, or has just RETURNED from injury, populate
`injury_details` with one entry per named player. Fields:
  - `player_name`: full name as it would appear on a team sheet
  - `team`: which of the two fixture teams they play for
  - `position_guess`: one of "striker", "winger", "attacking_mid",
    "defensive_mid", "centre_back", "fullback", "goalkeeper",
    "unknown". Guess from the story ("their striker X", "centre-back
    Y torn his ACL"); when the story doesn't say and you can't infer
    confidently from general knowledge, use "unknown". For midfielders
    you MUST pick either "attacking_mid" (number 10, creative / box-to-
    box / winger-ish) or "defensive_mid" (6, holder, destroyer). If
    you truly can't tell from context or general knowledge, use
    "unknown" — there is no generic "midfielder" bucket.
  - `is_out_confirmed`: true for confirmed OUT / SUSPENDED / RULED OUT,
    false for DOUBTFUL / ASSESSMENT / RETURN (i.e. the player might
    still play).
This field is only required when the hook names a specific player who
is unavailable. For generic team news ("squad rotation expected") leave
the list empty.

**What to skip** — generic season-summary content, fixture previews with no
new information, promotional "best bets" listicles, anything older than 48
hours that isn't still breaking today.

**RELEVANCE — HARD RULE** — every item you return must be directly about
THIS fixture, its two teams, its managers, or its key players. If a search
surfaces an unrelated story that happens to mention one of the teams in
passing (e.g. a Burnley injury story that namedrops Brighton as the next
opponent two weeks away), DROP IT. Do not surface it. The `mentions` array
must include at least one of the two teams in the fixture by name, and the
headline must be about that team or a player at that team — not a third
team. A bettor reading the card must feel the story is unambiguously
about THIS match.

**CRITICAL OUTPUT RULE** — the `headline` and `summary` fields must be plain
prose text. NEVER include XML, HTML, or citation markup of any kind. The
web_search tool's output includes `<cite index="...">...</cite>` tags around
quoted passages — strip those out when paraphrasing. If you cannot write the
line without using such markup, drop the item entirely. Do not include
`<cite>`, `<ref>`, `<sup>`, square-bracket citation numbers, or any other
tracking syntax in your output.

**PRICE / ODDS RULE — HARD BAN** — do NOT include any numeric odds,
multipliers, or price references in the `headline` or `summary`. Describe
the story and the markets in WORDS only. Never write: "at N.NN", "pays
N.NN", "odds of N.NN", "stacks at N.NN", "stacked at N.NN", "@ N.NN",
"— N.NN", "in total pays N.NN", "N.NN decimal", "N-to-N". The downstream
UI re-quotes live prices and embedded numbers drift against the display.
Keep non-price numerics (goal counts, league positions, scorelines,
streaks, thresholds like "Over 2.5" or "3+ goals") — those sharpen the
line and will survive the scrub.

You are writing editorial copy that will be displayed as the hero headline
on a card. Punchy matters more than complete."""


def _submit_tool_schema() -> dict[str, Any]:
    return {
        "name": "submit_news_items",
        "description": (
            "Submit the list of news items found for this fixture. "
            "Call exactly once, after all web searches are complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "headline": {"type": "string"},
                            "summary": {"type": "string"},
                            "hook_type": {"type": "string", "enum": _HOOK_VALUES},
                            "source_url": {"type": "string"},
                            "source_name": {"type": "string"},
                            "published_at": {
                                "type": "string",
                                "description": "ISO 8601 if known, else empty string",
                            },
                            "mentions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Concrete team / player / coach names mentioned. "
                                    "Used by entity resolution downstream."
                                ),
                            },
                            "injury_details": {
                                "type": "array",
                                "description": (
                                    "Only populate for injury / team_news items "
                                    "that name a specific player who is out, "
                                    "suspended, doubtful, or returning. Empty "
                                    "list otherwise."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "player_name": {"type": "string"},
                                        "team": {"type": "string"},
                                        "position_guess": {
                                            "type": "string",
                                            "enum": [
                                                "striker", "winger",
                                                "attacking_mid",
                                                "defensive_mid",
                                                "centre_back", "fullback",
                                                "goalkeeper", "unknown",
                                            ],
                                        },
                                        "is_out_confirmed": {"type": "boolean"},
                                    },
                                    "required": [
                                        "player_name", "team",
                                        "position_guess", "is_out_confirmed",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["headline", "summary", "hook_type", "mentions"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    }


class NewsIngester:
    """Wraps AsyncAnthropic + web_search + terminal tool_use extraction.

    Results are cached in SQLite keyed on (fixture_id, YYYY-MM-DD) so repeated
    boot-time runs on the same day don't re-spend LLM tokens.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        store: CandidateStore,
        *,
        model: str,
        max_searches: int,
        cache_ttl_seconds: float,
        cost_tracker: "Optional[Any]" = None,
    ):
        self._client = client
        self._store = store
        self._model = model
        self._max_searches = max_searches
        self._cache_ttl_seconds = cache_ttl_seconds
        self._submit_tool = _submit_tool_schema()
        self._cost_tracker = cost_tracker

    async def ingest_for_fixture(
        self,
        *,
        fixture_id: str,
        home: str,
        away: str,
        league: str,
        kickoff_iso: str,
    ) -> list[NewsItem]:
        """Return NewsItem list for a fixture. Hits cache first, then the LLM."""
        cache_key = kickoff_iso[:10] if kickoff_iso else "unknown"
        cached = await self._store.get_cached_ingest(
            fixture_id, cache_key, self._cache_ttl_seconds
        )
        if cached is not None:
            logger.info("News cache hit: %s (%s)", fixture_id, cache_key)
            # Bump ingest_cache.ingested_at so the tier-loop freshness
            # check sees this scout pass. Without this the timestamp
            # only advances on LLM-call paths, and `skipped_fresh` stays
            # at 0 forever in the tier loop (cost leak: every cycle
            # re-runs the engine even though nothing changed). See
            # candidate_store.is_fixture_news_fresh + the
            # freshness_timestamp_trap memory.
            try:
                await self._store.touch_ingest_cache(fixture_id, cache_key)
            except Exception as exc:
                logger.warning(
                    "News ingest: touch_ingest_cache failed for %s: %s",
                    fixture_id, exc,
                )
            return [NewsItem(**row) for row in cached]

        raw_items = await self._call_llm(home=home, away=away, league=league, kickoff_iso=kickoff_iso)

        # Relevance filter — drop items that aren't about this fixture's two
        # teams. Prompt-level rule already covers this, but belt-and-braces
        # against the scout occasionally surfacing a third-team story.
        home_low = (home or "").lower()
        away_low = (away or "").lower()

        def _mentions_fixture_team(row: dict[str, Any]) -> bool:
            # Require the HEADLINE or SUMMARY to name one of the fixture's two
            # teams. The `mentions` array alone is too permissive — the scout
            # routinely name-drops adjacent teams in passing and the entity
            # resolver would then incorrectly attach the story.
            head = str(row.get("headline") or "").lower()
            body = str(row.get("summary") or "").lower()
            for team in (home_low, away_low):
                if not team:
                    continue
                if team in head or team in body:
                    return True
            return False

        before = len(raw_items)
        raw_items = [r for r in raw_items if _mentions_fixture_team(r)]
        if len(raw_items) != before:
            logger.info(
                "News ingest: dropped %d/%d items unrelated to %s vs %s",
                before - len(raw_items), before, home, away,
            )

        news_items = [_raw_to_news(row) for row in raw_items if row.get("headline")]

        # Persist the raw payload in the cache as list[dict] (pre-NewsItem);
        # saving the resolved NewsItem fields lets us round-trip on cache hit.
        #
        # IMPORTANT: do NOT cache empty results. The LLM call returning 0
        # items is almost always a transient failure (credit balance, rate
        # limit, network) — not a real "this fixture has no news" verdict.
        # Caching `[]` poisons subsequent runs in the cache TTL window: every
        # rerun reads the empty cache + skips the LLM, so the feed stays
        # broken for 6 hours after a single failure burst. (Hit this on
        # 2026-04-22 PM after the credit-wall outage.)
        if news_items:
            cache_payload = [item.model_dump() for item in news_items]
            await self._store.save_cached_ingest(fixture_id, cache_key, cache_payload)
            await self._store.save_news_items(news_items)
        else:
            logger.info(
                "News ingest: %s -> 0 items, skipping cache write so the next "
                "run can retry the LLM (likely transient failure)",
                fixture_id,
            )

        logger.info(
            "News ingest: %s (%s vs %s) -> %d items",
            fixture_id, home, away, len(news_items),
        )
        return news_items

    async def _call_llm(
        self, *, home: str, away: str, league: str, kickoff_iso: str
    ) -> list[dict[str, Any]]:
        user_msg = (
            f"Fixture: {home} vs {away}\n"
            f"League: {league}\n"
            f"Kickoff: {kickoff_iso}\n\n"
            "Research the latest news. Call submit_news_items when done."
        )
        # Cycle-cost telemetry: bump scout counter before the call so a
        # crashed/timed-out call still shows up as a paid invocation.
        # Lazy import keeps the ingester importable when main.py isn't
        # loaded (tests / scripts).
        try:
            from app.main import _bump_cycle_counter
            _bump_cycle_counter("scout_haiku_websearch")
        except Exception:
            pass
        # Cost-tripwire short-circuit. Scout uses web_search; without
        # it we cannot do useful research, so when the budget is gone
        # we silently skip and return [] (no new candidates this cycle).
        if self._cost_tracker is not None:
            try:
                projected = self._cost_tracker.estimate_haiku_call(
                    input_tokens=1500, max_output_tokens=4096,
                    web_search=True, web_search_calls=self._max_searches,
                )
                if not await self._cost_tracker.can_spend(projected):
                    logger.info(
                        "[cost] news scout skipped — daily LLM budget "
                        "exhausted (fixture=%s)", home,
                    )
                    return []
            except Exception as exc:
                logger.warning(
                    "NewsIngester cost-tracker check failed: %s", exc,
                )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=[
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": self._max_searches,
                    },
                    self._submit_tool,
                ],
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as exc:
            logger.warning("News ingest LLM call failed: %s", exc)
            return []

        if self._cost_tracker is not None:
            try:
                actual = self._cost_tracker.cost_from_usage(
                    getattr(response, "usage", None),
                    web_search=True, web_search_calls=self._max_searches,
                )
                await self._cost_tracker.record_call(
                    model=self._model, kind="news_scout",
                    cost_usd=actual,
                )
            except Exception as exc:
                logger.warning(
                    "NewsIngester cost-tracker record failed: %s", exc,
                )

        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.debug(
                "News ingest tokens — input=%s cache_read=%s cache_create=%s output=%s",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "cache_read_input_tokens", "?"),
                getattr(usage, "cache_creation_input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_news_items":
                raw_input = block.input if isinstance(block.input, dict) else {}
                items = raw_input.get("items", [])
                return items if isinstance(items, list) else []

        logger.info("News ingest: model finished without calling submit_news_items")
        return []


def _raw_to_news(row: dict[str, Any]) -> NewsItem:
    hook_value = str(row.get("hook_type") or HookType.OTHER.value).lower()
    try:
        hook = HookType(hook_value)
    except ValueError:
        hook = HookType.OTHER

    mentions = row.get("mentions") or []
    if not isinstance(mentions, list):
        mentions = []

    injury_raw = row.get("injury_details") or []
    injury_details: list[dict] = []
    if isinstance(injury_raw, list):
        for entry in injury_raw:
            if not isinstance(entry, dict):
                continue
            pos = str(entry.get("position_guess") or "unknown").lower()
            # Legacy compat: PR #33 v1 cached rows can still contain a bare
            # "midfielder" bucket. That value is no longer part of the enum
            # and no longer has a route, so fold it down to "unknown" and
            # let the downstream router fall through.
            if pos == "midfielder":
                pos = "unknown"
            injury_details.append({
                "player_name": _clean_copy(entry.get("player_name")),
                "team": _clean_copy(entry.get("team")),
                "position_guess": pos,
                "is_out_confirmed": bool(entry.get("is_out_confirmed")),
            })

    return NewsItem(
        source="llm_web_search",
        source_url=str(row.get("source_url") or "").strip(),
        source_name=_clean_copy(row.get("source_name")),
        headline=strip_prices(_clean_copy(row.get("headline"))),
        summary=strip_prices(_clean_copy(row.get("summary"))),
        hook_type=hook,
        published_at=str(row.get("published_at") or "").strip(),
        mentions=[_clean_copy(m) for m in mentions if _clean_copy(m)],
        injury_details=injury_details,
    )
