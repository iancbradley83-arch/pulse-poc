"""SSE-driven live price updates for BB + combo cards on the pre-match feed.

Keeps the displayed `total_odds` + leg odds fresh as the operator reprices.
Single long-lived connection to `/v1/sportsdata/sse/events?eventIDs=<live>`,
debounced recompute via `/v1/betting/calculateBets`, pushed to WS clients.

Scope (v1): pre-match only. Live SSE volume is much higher and needs a
different cadence strategy; deferred.

Cost: $0 LLM. `calculate_bets` is part of the Rogue Betting API on the
anonymous Bearer JWT we already use — same rate limits as any other Rogue
call (5 req/s). Debouncing per card (1s window) keeps us well under.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.models.schemas import Card
from app.services.feed_manager import FeedManager
from app.services.rogue_client import RogueClient

logger = logging.getLogger(__name__)

# Recompute debounce window per card. Coalesces bursts of leg ticks into
# one calculate_bets call. 1s is conservative for pre-match; tune up to 2s
# if Rogue's rate limit becomes a concern.
DEBOUNCE_S = 1.0

# How often we prune the debounce queue and reconsider subscriptions.
PRUNE_INTERVAL_S = 30.0


class SSEPricingManager:
    """Subscribes to Rogue SSE for the events in the pre-match feed; on any
    leg tick, re-prices affected BB / combo cards via `calculate_bets` and
    broadcasts `card_update` WS messages.

    Lifecycle: created once at startup; `set_cards()` called whenever the
    feed's card list changes (initial load + every rerun swap). Resets the
    SSE subscription to match the new event set.
    """

    def __init__(self, feed: FeedManager, rogue_client: RogueClient):
        self._feed = feed
        self._rogue = rogue_client
        # card_id → (bet_type, vs_id or None, game_id, set of leg selection_ids)
        self._cards: dict[str, dict[str, Any]] = {}
        # event_id → set of card_ids
        self._event_to_cards: dict[str, set[str]] = {}
        # selection_id → set of card_ids (so per-selection ticks route quickly)
        self._selection_to_cards: dict[str, set[str]] = {}
        # Pending-recompute set (debounced); card_id → earliest-ok-to-run time
        self._pending: dict[str, float] = {}
        self._pending_lock = asyncio.Lock()
        # Main loop tasks
        self._sse_task: Optional[asyncio.Task] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Current event-id set we're subscribed to. Changes trigger a
        # teardown + reconnect of the SSE stream.
        self._subscribed_events: set[str] = set()
        # Generation counter — incremented on each set_cards() call. The
        # SSE loop checks this to bail out when a reconfigure is requested.
        self._gen: int = 0

    # ── Public API ──

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(self._recompute_worker())
        logger.info("[sse_pricing] manager started")

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._sse_task, self._worker_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._sse_task = None
        self._worker_task = None
        logger.info("[sse_pricing] manager stopped")

    def set_cards(self, cards: list[Card]) -> None:
        """Rebuild card + event indices from the current card list, then
        kick the SSE loop to reconnect with the new event-id filter.

        Called once after initial load + after every rerun atomic swap.
        Only indexes BB/combo cards (singles have nothing to recompute —
        their one leg's odds are already surfaced by the catalogue refresh).
        """
        self._cards.clear()
        self._event_to_cards.clear()
        self._selection_to_cards.clear()
        for c in cards:
            if c.bet_type not in ("bet_builder", "combo"):
                continue
            if not c.legs:
                continue
            sel_ids = {leg.selection_id for leg in c.legs if leg.selection_id}
            if not sel_ids:
                continue
            game_id = getattr(c.game, "id", "") if getattr(c, "game", None) else ""
            self._cards[c.id] = {
                "bet_type": c.bet_type,
                "virtual_selection": c.virtual_selection,
                "game_id": game_id,
                "selection_ids": list(sel_ids),
            }
            if game_id:
                self._event_to_cards.setdefault(game_id, set()).add(c.id)
            for sid in sel_ids:
                self._selection_to_cards.setdefault(sid, set()).add(c.id)
        new_events = set(self._event_to_cards.keys())
        if new_events != self._subscribed_events:
            self._subscribed_events = new_events
            self._gen += 1
            # Kill the current SSE loop; worker's sleep will notice generation
            # mismatch on next tick, OR cancel the task directly.
            if self._sse_task is not None:
                self._sse_task.cancel()
                self._sse_task = None
            if self._subscribed_events:
                self._sse_task = asyncio.create_task(self._sse_loop(self._gen))
        logger.info(
            "[sse_pricing] indexed %d cards across %d events (gen=%d)",
            len(self._cards), len(self._subscribed_events), self._gen,
        )

    # ── SSE consumer ──

    async def _sse_loop(self, my_gen: int) -> None:
        """Long-lived connection. Reconnects with exponential backoff on
        drop. Bails out if `set_cards()` was called (generation changed)
        since this loop started — a new loop will take over with the
        fresh event-id set."""
        backoff = 2.0
        while not self._stop.is_set() and my_gen == self._gen:
            event_ids = ",".join(sorted(self._subscribed_events))
            if not event_ids:
                return
            try:
                await self._stream_once(my_gen, event_ids)
                # Clean exit means the upstream closed; reconnect promptly.
                backoff = 2.0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[sse_pricing] SSE stream errored: %s (backoff %.1fs)", exc, backoff)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 30.0)

    async def _stream_once(self, my_gen: int, event_ids: str) -> None:
        token = await self._rogue._auth.get_session_jwt()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        }
        params = {
            "eventIDs": event_ids,
            "isLive": "false",
            "initialData": "false",   # we already have initial state from the load path
            "isBetBuilderEnabled": "true",
        }
        url = f"{self._rogue._base_url}/v1/sportsdata/sse/events"

        timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("GET", url, params=params, headers=headers) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="ignore")[:300]
                    raise RuntimeError(f"SSE stream returned {r.status_code}: {body}")
                logger.info(
                    "[sse_pricing] SSE connected (gen=%d, events=%d)",
                    my_gen, len(self._subscribed_events),
                )
                data_buf: list[str] = []
                async for line in r.aiter_lines():
                    if self._stop.is_set() or my_gen != self._gen:
                        return
                    # SSE frame terminator is a blank line.
                    if not line.strip():
                        if data_buf:
                            await self._handle_sse_data("\n".join(data_buf))
                            data_buf = []
                        continue
                    if line.startswith(":"):
                        # comment / heartbeat — ignore
                        continue
                    if line.startswith("data:"):
                        data_buf.append(line[5:].lstrip())
                    # Ignore other prefixes (event:, id:, retry:) — we key off data.

    async def _handle_sse_data(self, raw: str) -> None:
        """Parse an SSE `data:` payload and enqueue affected cards for
        recompute. Rogue SSE wraps one or more change objects in a JSON
        array; format per the MCP README:
          [{Operation, Type, Reference: {EventId, MarketId, ...}, Changeset}]
        """
        try:
            payload = json.loads(raw)
        except Exception:
            return
        changes = payload if isinstance(payload, list) else [payload]
        affected: set[str] = set()
        suspend_events: set[str] = set()
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            ref = ch.get("Reference") or {}
            cs = ch.get("Changeset") or {}
            typ = ch.get("Type")
            event_id = ref.get("EventId") or cs.get("_id") if typ == "event" else ref.get("EventId")
            # Event-level suspension → flag every card on that event
            if typ == "event" and isinstance(cs.get("IsSuspended"), bool):
                if cs["IsSuspended"]:
                    if event_id:
                        suspend_events.add(event_id)
                else:
                    # Un-suspend: re-quote every affected card to refresh price
                    for cid in self._event_to_cards.get(event_id, ()):
                        affected.add(cid)
                continue
            # Market/selection ticks → route via event_id or selection ids
            if event_id and event_id in self._event_to_cards:
                # Cheaper path: per-selection routing when we have sel IDs
                for sel in (cs.get("Selections") or []):
                    sid = sel.get("_id")
                    for cid in self._selection_to_cards.get(sid, ()):
                        affected.add(cid)
                # If no per-selection sel ids on this change, fall back to
                # every card on the event.
                if not cs.get("Selections"):
                    for cid in self._event_to_cards.get(event_id, ()):
                        affected.add(cid)

        # Apply suspension flags immediately (UI-critical)
        for eid in suspend_events:
            for cid in self._event_to_cards.get(eid, ()):
                updated = self._feed.update_card_total(cid, suspended=True)
                if updated is not None:
                    try:
                        await self._feed.broadcast_card_update(updated)
                    except Exception as exc:
                        logger.warning("[sse_pricing] broadcast suspend failed: %s", exc)

        # Debounce all affected recomputes
        if affected:
            async with self._pending_lock:
                now = time.monotonic()
                for cid in affected:
                    # Set earliest-run time forward by DEBOUNCE_S.
                    self._pending[cid] = max(self._pending.get(cid, 0.0), now + DEBOUNCE_S)

    # ── Recompute worker ──

    async def _recompute_worker(self) -> None:
        """Loops forever; pulls debounced card recomputes off the pending
        queue and calls calculate_bets. Broadcasts card_update on change."""
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            due: list[str] = []
            async with self._pending_lock:
                for cid, run_at in list(self._pending.items()):
                    if run_at <= now:
                        due.append(cid)
                        del self._pending[cid]
            for cid in due:
                try:
                    await self._recompute_card(cid)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.warning("[sse_pricing] recompute %s errored: %s", cid[:12], exc)

    async def _recompute_card(self, card_id: str) -> None:
        meta = self._cards.get(card_id)
        if not meta:
            return
        # For BBs: use the VirtualSelection id (one calculate_bets call).
        # For combos: send the leg selection ids (calculate_bets returns a
        # Bets[Type='Combo'] entry with the combined TrueOdds).
        if meta["bet_type"] == "bet_builder":
            if not meta.get("virtual_selection"):
                return
            ids = [meta["virtual_selection"]]
            target_bet_types = ("BetBuilder", "Single")
        else:
            ids = list(meta["selection_ids"])
            target_bet_types = ("Combo",)

        try:
            quote = await self._rogue.calculate_bets(ids)
        except Exception as exc:
            logger.warning("[sse_pricing] calculate_bets errored for %s: %s", card_id[:12], exc)
            return
        if not isinstance(quote, dict):
            return
        bets = quote.get("Bets") or []
        target = next(
            (b for b in bets if (b or {}).get("Type") in target_bet_types),
            None,
        )
        if not target or not isinstance(target.get("TrueOdds"), (int, float)):
            # Likely a suspended/disabled leg. Mark card suspended.
            updated = self._feed.update_card_total(card_id, suspended=True)
            if updated is not None:
                try:
                    await self._feed.broadcast_card_update(updated)
                except Exception:
                    pass
            return
        new_total = round(float(target["TrueOdds"]), 2)
        # Extract leg odds from the Selections[] array
        leg_odds = {
            s.get("Id"): float(s["TrueOdds"])
            for s in (quote.get("Selections") or [])
            if isinstance(s.get("TrueOdds"), (int, float)) and s.get("Id")
        }
        updated = self._feed.update_card_total(
            card_id, total_odds=new_total, leg_odds=leg_odds, suspended=False,
        )
        if updated is not None:
            try:
                await self._feed.broadcast_card_update(updated)
            except Exception as exc:
                logger.warning("[sse_pricing] broadcast_card_update failed: %s", exc)
