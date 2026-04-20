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
from typing import Any, Optional

from anthropic import AsyncAnthropic

from app.models.news import HookType, NewsItem
from app.services.candidate_store import CandidateStore

logger = logging.getLogger(__name__)


_HOOK_VALUES = [h.value for h in HookType if h not in (HookType.PRICE_MOVE, HookType.LIVE_MOMENT)]


SYSTEM_PROMPT = """You are a sports news scout for a betting-content product.

For a given soccer fixture, find newsworthy items from the last 48 hours that
could make specific betting markets more interesting. You must:

1. Use the `web_search` tool to look up current news, injury reports, team
   news, transfers, manager quotes, press conferences, and pre-match previews
   for the teams and key players involved.
2. After researching, call the `submit_news_items` tool exactly once with the
   structured list of findings. Do not write a free-text answer.

Each news item must be independently newsworthy. Skip generic season-summary
content. Include concrete player / team / coach names in the `mentions` list
so downstream entity resolution works. Prefer primary-source URLs.

Hook type must be one of: injury, team_news, transfer, manager_quote,
tactical, preview, article, other."""


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
    ):
        self._client = client
        self._store = store
        self._model = model
        self._max_searches = max_searches
        self._cache_ttl_seconds = cache_ttl_seconds
        self._submit_tool = _submit_tool_schema()

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
            return [NewsItem(**row) for row in cached]

        raw_items = await self._call_llm(home=home, away=away, league=league, kickoff_iso=kickoff_iso)

        news_items = [_raw_to_news(row) for row in raw_items if row.get("headline")]

        # Persist the raw payload in the cache as list[dict] (pre-NewsItem);
        # saving the resolved NewsItem fields lets us round-trip on cache hit.
        cache_payload = [item.model_dump() for item in news_items]
        await self._store.save_cached_ingest(fixture_id, cache_key, cache_payload)
        await self._store.save_news_items(news_items)

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

    return NewsItem(
        source="llm_web_search",
        source_url=str(row.get("source_url") or "").strip(),
        source_name=str(row.get("source_name") or "").strip(),
        headline=str(row.get("headline") or "").strip(),
        summary=str(row.get("summary") or "").strip(),
        hook_type=hook,
        published_at=str(row.get("published_at") or "").strip(),
        mentions=[str(m).strip() for m in mentions if str(m).strip()],
    )
