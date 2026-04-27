"""Cold-start feed rehydrate from `published_cards` snapshots.

Reads the serialized Card snapshots written by FeedManager on every
`add_prematch_card(...)` and re-inserts them into a fresh FeedManager.
Catalog-independent — the snapshots already carry every render field
(legs, market labels, deep_link, bscode), so this path needs no LLM,
no Rogue API call, and no MarketCatalog lookup. Strictly read-only at
boot.

This is the pivot from PR #63 (which crash-looped because it tried to
re-render via `_publish_loop` → `catalog.get(...)` with an empty
catalog). Reverted in PR #64. This file replaces that approach with a
pure JSON → pydantic load.

Failure semantics: malformed snapshot rows are logged + skipped, never
raised. The startup hook in main.py wraps the whole call in a
try/except so even a wholesale rehydrate failure cannot stop the app
from booting.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.schemas import Card

if TYPE_CHECKING:
    from app.services.candidate_store import CandidateStore
    from app.services.feed_manager import FeedManager

logger = logging.getLogger(__name__)


async def rehydrate_feed_from_snapshots(
    store: "CandidateStore",
    feed: "FeedManager",
    *,
    limit: int = 200,
) -> dict:
    """Load `published_cards` snapshots into FeedManager.

    Returns `{loaded, skipped, total}` for the boot log line. Read-only;
    no catalog or LLM dependency. Each row is parsed via
    `Card.model_validate_json` and re-inserted with `_skip_snapshot=True`
    so the upsert hook doesn't pointlessly re-write the same row we
    just read.
    """
    rows = await store.list_published_cards(limit=limit)
    loaded = 0
    skipped = 0
    for card_id, snapshot_json, _expires_at in rows:
        try:
            card = Card.model_validate_json(snapshot_json)
        except Exception as exc:
            logger.warning(
                "[PULSE] rehydrate skipped %s: parse error %r",
                card_id, exc,
            )
            skipped += 1
            continue
        try:
            feed.add_prematch_card(card, _skip_snapshot=True)
            loaded += 1
        except Exception as exc:
            logger.warning(
                "[PULSE] rehydrate skipped %s: insert error %r",
                card_id, exc,
            )
            skipped += 1
    logger.info(
        "[PULSE] feed rehydrated — loaded=%d skipped=%d total=%d",
        loaded, skipped, len(rows),
    )
    return {"loaded": loaded, "skipped": skipped, "total": len(rows)}
