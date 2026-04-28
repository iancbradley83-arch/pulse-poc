"""SQLite-backed store for NewsItem + CandidateCard.

Thin wrapper over aiosqlite. Minimal schema on purpose — we'll migrate to
Postgres (Stage 9-ish) once concurrent writes and cross-process access
matter. Until then this is a file on disk the engine writes to and the
admin table reads from.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    Embed,
    HookType,
    NewsItem,
    StorylineItem,
    StorylineParticipant,
    StorylineType,
)

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,
    source TEXT,
    source_url TEXT,
    source_name TEXT,
    headline TEXT,
    summary TEXT,
    hook_type TEXT,
    published_at TEXT,
    ingested_at REAL,
    mentions_json TEXT,
    fixture_ids_json TEXT,
    team_ids_json TEXT,
    -- Optional structured position data for INJURY / TEAM_NEWS items.
    -- JSON-encoded list[dict] — see NewsItem.injury_details for the shape.
    -- Added 2026-04-23 for position-aware INJURY routing.
    injury_details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ingested_at ON news_items(ingested_at);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    created_at REAL,
    expires_at REAL,
    news_item_id TEXT,
    hook_type TEXT,
    bet_type TEXT,
    game_id TEXT,
    market_ids_json TEXT,
    selection_ids_json TEXT,
    score REAL,
    threshold_passed INTEGER,
    reason TEXT,
    status TEXT,
    narrative TEXT,
    supporting_stats_json TEXT,
    -- Real correlated BB / boosted combo price stamped by ComboBuilder after
    -- Rogue's POST /v1/betting/calculateBets. NULL = no real quote, naive
    -- product only.
    total_odds REAL,
    -- Price provenance: "rogue_calculate_bets" (real correlated/boosted),
    -- "naive" (leg-product fallback), or NULL (not priced).
    price_source TEXT,
    -- Rogue VirtualSelection id (returned by /betbuilder/match). Persisted
    -- so the SSEPricingManager can re-quote the BB on leg ticks without
    -- rebuilding the piped id from leg ids.
    virtual_selection TEXT,
    -- Cross-event storyline FK — set when CrossEventBuilder produced this
    -- candidate from a StorylineItem (Golden Boot race, etc.). NULL for
    -- single-event cards and per-fixture bet builders. Publisher swaps
    -- the "Bet Builder" badge for "Weekend Storyline" when present.
    storyline_id TEXT,
    -- Stage 5b — server-minted bscode (6-char code) from kmianko's
    -- share-betslip endpoint. Persisted so we don't re-mint the same
    -- selection set across publish cycles. NULL when minter disabled
    -- or mint failed; publisher falls back to PR #36 selectionId URL.
    bscode TEXT,
    FOREIGN KEY (news_item_id) REFERENCES news_items(id),
    FOREIGN KEY (storyline_id) REFERENCES storyline_items(id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_created_at ON candidates(created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_hook_type ON candidates(hook_type);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
CREATE INDEX IF NOT EXISTS idx_candidates_game_id ON candidates(game_id);

CREATE TABLE IF NOT EXISTS ingest_cache (
    fixture_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    ingested_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (fixture_id, cache_key)
);

-- Cross-event storyline items. Each row is one detected narrative that
-- spans multiple fixtures in the same matchweek (Golden Boot race,
-- relegation battle, Europe chase). Persisted additively so the admin
-- table + debug endpoints can inspect what the scout found even if the
-- downstream combo build failed. Candidates link back via
-- `candidates.storyline_id`.
CREATE TABLE IF NOT EXISTS storyline_items (
    id TEXT PRIMARY KEY,
    storyline_type TEXT NOT NULL,             -- StorylineType enum value
    title TEXT,                               -- e.g. the authored headline
    summary TEXT,                             -- detector's headline_hint / one-liner
    participating_fixture_ids_json TEXT,      -- JSON array of Rogue event ids
    participating_players_json TEXT,          -- JSON array of {player_name, team_name, fixture_id, extra}
    generated_at REAL NOT NULL,
    expires_at REAL,                          -- 0 / NULL = no expiry
    status TEXT NOT NULL DEFAULT 'active'     -- 'active' | 'expired' | 'skipped'
);
CREATE INDEX IF NOT EXISTS idx_storyline_items_generated_at ON storyline_items(generated_at);
CREATE INDEX IF NOT EXISTS idx_storyline_items_status ON storyline_items(status);
CREATE INDEX IF NOT EXISTS idx_storyline_items_type ON storyline_items(storyline_type);

-- Human labels on candidates for the learning loop. One row per review
-- event (a card reviewed N times stores N rows so we can track flips).
CREATE TABLE IF NOT EXISTS candidate_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    verdict TEXT NOT NULL,           -- 'good' | 'bad'
    reason_code TEXT,                -- 'wrong_team' | 'bad_headline' | 'odds_nonsensical' | 'story_unrelated' | 'angle_mismatch' | 'other'
    note TEXT,                       -- optional free-text ("rewriter hallucinated player name")
    reviewer TEXT,                   -- email / handle / "anonymous"
    created_at REAL NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_candidate ON candidate_reviews(candidate_id);
CREATE INDEX IF NOT EXISTS idx_reviews_verdict ON candidate_reviews(verdict);
CREATE INDEX IF NOT EXISTS idx_reviews_created ON candidate_reviews(created_at);

-- Public thumbs-up / thumbs-down reactions on feed cards. Anonymous
-- viewers keyed by the `pulse_anon_id` cookie (set by middleware in
-- main.py). One reaction per (card_id, anon_id) — the UNIQUE index
-- enforces it and a second vote upserts via INSERT .. ON CONFLICT.
-- Additive migration: new table only, no changes to existing tables.
CREATE TABLE IF NOT EXISTS card_reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    anon_id TEXT NOT NULL,
    reaction TEXT NOT NULL CHECK (reaction IN ('up', 'down')),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reactions_card ON card_reactions(card_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reactions_anon_card
    ON card_reactions(anon_id, card_id);

-- Stage 5 CTA click-through tracking. One row per click (unlike reactions
-- which upsert) — the same anon tapping the same card twice means two
-- distinct deep-link opens, and we want to see the rate of that vs
-- single-click abandonment. Additive migration.
CREATE TABLE IF NOT EXISTS card_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    anon_id TEXT,                  -- may be NULL for sendBeacon before cookie lands
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clicks_card ON card_clicks(card_id);
CREATE INDEX IF NOT EXISTS idx_clicks_created ON card_clicks(created_at);

-- U3 rewrite cache. NarrativeRewriter calls Sonnet for every published
-- candidate every cycle; when the same fixture still has the same news
-- items 4h later the inputs hash to the same key and the second run is
-- free. TTL (default 24h) bounds how stale a cached rewrite can be
-- before we pay Sonnet again. Key is a SHA256 over
--   bet_type|hook_type|headline|legs_csv|total_odds
-- computed by NarrativeRewriter so we don't have to reconstruct it on
-- read. Additive table; does not touch candidates / news_items.
CREATE TABLE IF NOT EXISTS rewrite_cache (
    key TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    angle TEXT NOT NULL,
    model TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rewrite_cache_created ON rewrite_cache(created_at);

-- Daily LLM spend tripwire (cost-aware redesign, 2026-04-26). One row
-- per UTC day. `accumulated_usd` is monotonic-increasing within a day;
-- `calls` is the count of LLM messages-create invocations counted
-- against the budget. The CostTracker upserts via
-- `INSERT ... ON CONFLICT(date) DO UPDATE` (see add_daily_cost).
CREATE TABLE IF NOT EXISTS daily_cost (
    date TEXT PRIMARY KEY,
    accumulated_usd REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    last_call_at REAL
);

-- Per-kind daily cost aggregation (PR feat/cost-by-kind-telemetry,
-- 2026-04-28). Restores the bucket-level telemetry that PR #66 simplified
-- out. One row per (utc_date, kind); `kind` strings are the ones already
-- passed into `cost_tracker.record_call(kind=...)` (e.g. `news_scout`,
-- `rewrite`, `storyline_scout`, `boot_scout`). Foundation for the
-- upcoming `/admin/cost.json?detail=1` endpoint that ops-bot's
-- `/breakdown` command will consume. Pure aggregate — no Pydantic model;
-- callers see / write tuples.
CREATE TABLE IF NOT EXISTS daily_cost_by_kind (
    utc_date TEXT NOT NULL,
    kind     TEXT NOT NULL,
    usd      REAL NOT NULL DEFAULT 0,
    calls    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (utc_date, kind)
);

-- Published-card snapshot table (PR fix/published-cards-snapshot,
-- 2026-04-27). Each row is a fully-rendered Card serialized to JSON at
-- the moment it lands in FeedManager. Cold-start rehydrate reads these
-- rows directly so the feed survives a redeploy WITHOUT having to call
-- the catalog or LLM again — which was PR #63's crash mode (catalog was
-- empty during rehydrate, `catalog.get(...).market_type` AttributeErrored
-- and crash-looped the app for 30 min). The snapshot carries every
-- field the frontend needs (legs, market labels, deep_link, etc.) so the
-- rehydrate path is pure JSON → pydantic, no network, no LLM cost.
--
-- Additive new table only — no changes to existing tables. `candidate_id`
-- is nullable because featured BBs bypass the candidate_store (built at
-- runtime in services/featured_bb.py).
CREATE TABLE IF NOT EXISTS published_cards (
    card_id TEXT PRIMARY KEY,
    candidate_id TEXT,
    snapshot_json TEXT NOT NULL,
    snapshotted_at REAL NOT NULL,
    expires_at REAL,
    bet_type TEXT,
    storyline_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_published_cards_expires ON published_cards(expires_at);
CREATE INDEX IF NOT EXISTS idx_published_cards_storyline ON published_cards(storyline_id);

-- Per-operator embed contract (PR feat/embed-token-contract, 2026-04-28).
-- One row per operator-environment. The widget loaded inside an operator
-- iframe sends `embed_token` on every /api/feed request; the
-- verify_embed_token middleware looks the token up here, checks `active`,
-- and verifies the request's Origin / Referer host matches one of
-- `allowed_origins`. Plain opaque tokens — NOT JWT, NOT OAuth.
--
-- `theme_overrides` is provisioned for wave 4 (CSS-vars per brand) but
-- not consumed yet — the column persists whatever the admin form posts.
CREATE TABLE IF NOT EXISTS embeds (
    token TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    allowed_origins TEXT NOT NULL,    -- JSON list of host patterns; "*.foo.com" wildcard supported
    theme_overrides TEXT,             -- nullable JSON map; wave-4 reserved
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,         -- ISO-8601 UTC
    notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_embeds_slug ON embeds(slug);
CREATE INDEX IF NOT EXISTS idx_embeds_active ON embeds(active);
"""


class CandidateStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open an aiosqlite connection with Railway-volume-safe pragmas.

        Railway's persistent volumes are NFS-style and do NOT support
        SQLite's WAL journal (mmap + file locking). WAL mode throws
        `disk I/O error` on these mounts. We force the classic rollback
        journal (`DELETE`) on every connection so the setting sticks
        regardless of prior boots, and keep synchronous=NORMAL for
        throughput. See memory feedback_sqlite_railway_volume.md.
        """
        conn = await aiosqlite.connect(self._db_path)
        try:
            await conn.execute("PRAGMA journal_mode=DELETE")
            await conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            await conn.close()

    async def init(self) -> None:
        # Ensure the parent dir exists — critical when PULSE_DB_PATH points
        # at a freshly mounted Railway volume like /data/pulse.db.
        parent_dir = os.path.dirname(self._db_path) or "."
        os.makedirs(parent_dir, exist_ok=True)

        # Sweep stale -wal / -shm sidecars from a prior WAL-mode attempt.
        # If we inherit these from a previous boot that tried WAL on the
        # NFS volume, every subsequent connection fails with "disk I/O
        # error" even though we're now in DELETE mode — sqlite sees the
        # files and assumes WAL is still active.
        for suffix in ("-wal", "-shm"):
            stale = Path(self._db_path + suffix)
            try:
                if stale.exists():
                    stale.unlink()
                    logger.info("[CandidateStore] swept stale sidecar %s", stale)
            except Exception as exc:
                logger.warning(
                    "[CandidateStore] could not remove %s: %s", stale, exc
                )

        # Log fresh-vs-pre-existing for the Railway deploy verification.
        db_file = Path(self._db_path)
        if db_file.exists():
            size = db_file.stat().st_size
            logger.info(
                "[CandidateStore] opening pre-existing DB at %s (%d bytes)",
                self._db_path,
                size,
            )
        else:
            logger.info(
                "[CandidateStore] opening fresh DB at %s", self._db_path
            )

        async with self._connect() as db:
            # Confirm the pragma actually took — useful proof-of-persistence
            # log line that Railway deploy verification greps for.
            async with db.execute("PRAGMA journal_mode") as cur:
                mode_row = await cur.fetchone()
                mode = (mode_row[0] if mode_row else "?")
                if str(mode).lower() == "delete":
                    logger.info("[CandidateStore] journal_mode=DELETE confirmed")
                else:
                    logger.warning(
                        "[CandidateStore] unexpected journal_mode=%s (expected DELETE)",
                        mode,
                    )

            # Schema migrations — additive only, idempotent. The candidates
            # table grew over multiple PRs:
            #   - PR #8  added total_odds + price_source
            #   - PR #16 added virtual_selection
            #   - PR #32 (this) adds storyline_id for cross-event combos
            # We use ALTER TABLE ADD COLUMN (preserves existing rows) when
            # only the newest columns are missing, and fall back to a
            # drop-and-recreate when the table predates total_odds.
            async with db.execute("PRAGMA table_info(candidates)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if cols:
                # Old (pre-PR-#8) schema: drop + recreate, lose history
                if "total_odds" not in cols:
                    logger.warning(
                        "[CandidateStore] Dropping candidates table (old schema "
                        "missing total_odds/price_source); will recreate."
                    )
                    await db.execute("DROP TABLE candidates")
                    await db.commit()
                else:
                    # Run each additive ALTER independently — order matters
                    # only for first-run installs, but every column is
                    # idempotent on re-run.
                    if "virtual_selection" not in cols:
                        logger.info(
                            "[CandidateStore] Migrating candidates: ADD COLUMN virtual_selection"
                        )
                        await db.execute("ALTER TABLE candidates ADD COLUMN virtual_selection TEXT")
                        await db.commit()
                    if "storyline_id" not in cols:
                        logger.info(
                            "[CandidateStore] Migrating candidates: ADD COLUMN storyline_id"
                        )
                        await db.execute("ALTER TABLE candidates ADD COLUMN storyline_id TEXT")
                        await db.commit()
                    if "bscode" not in cols:
                        logger.info(
                            "[CandidateStore] Migrating candidates: ADD COLUMN bscode"
                        )
                        await db.execute("ALTER TABLE candidates ADD COLUMN bscode TEXT")
                        await db.commit()

            # news_items migration: injury_details_json added 2026-04-23 for
            # position-aware INJURY routing. Additive ALTER; no data loss.
            async with db.execute("PRAGMA table_info(news_items)") as cur:
                news_cols = {row[1] for row in await cur.fetchall()}
            if news_cols and "injury_details_json" not in news_cols:
                logger.info(
                    "[CandidateStore] Migrating news_items: ADD COLUMN injury_details_json"
                )
                await db.execute(
                    "ALTER TABLE news_items ADD COLUMN injury_details_json TEXT"
                )
                await db.commit()

            # rewrite_cache migration (U3 — 2026-04-23). Additive new table
            # only; follows the storyline_items pattern (CREATE TABLE IF
            # NOT EXISTS via executescript below is enough on first-run,
            # but we probe table_info so a future column addition has a
            # place to live without duplicating the whole block).
            async with db.execute("PRAGMA table_info(rewrite_cache)") as cur:
                _rc_cols = {row[1] for row in await cur.fetchall()}
            # No columns to migrate yet — the table is created by the
            # executescript(_SCHEMA) call below when missing. Probe exists
            # so the next drift has a landing spot.
            _ = _rc_cols

            # embeds migration (PR feat/embed-token-contract, 2026-04-28).
            # Additive new table; probe exists so future column additions
            # have a landing spot. CREATE TABLE IF NOT EXISTS in
            # executescript below handles first-run install.
            async with db.execute("PRAGMA table_info(embeds)") as cur:
                _embed_cols = {row[1] for row in await cur.fetchall()}
            _ = _embed_cols

            # daily_cost_by_kind migration (PR feat/cost-by-kind-telemetry,
            # 2026-04-28). Additive new table; CREATE TABLE IF NOT EXISTS
            # in executescript below handles first-run install. Probe
            # exists so future column additions (e.g. p95 latency, error
            # counts) have a landing spot without re-doing this block.
            async with db.execute("PRAGMA table_info(daily_cost_by_kind)") as cur:
                _bk_cols = {row[1] for row in await cur.fetchall()}
            _ = _bk_cols

            await db.executescript(_SCHEMA)
            await db.commit()

            # Proof-of-persistence log: after init, how many rows did we
            # inherit from the previous boot? First-boot runs will all
            # read 0; a post-redeploy cache-hit boot will show non-zero
            # and confirms the Railway volume retained state.
            try:
                async with db.execute("SELECT COUNT(*) FROM candidates") as cur:
                    cand_row = await cur.fetchone()
                async with db.execute("SELECT COUNT(*) FROM news_items") as cur:
                    news_row = await cur.fetchone()
                logger.info(
                    "[CandidateStore] init complete — candidates=%s news_items=%s",
                    (cand_row[0] if cand_row else 0),
                    (news_row[0] if news_row else 0),
                )
            except Exception as exc:
                logger.warning("[CandidateStore] row-count probe failed: %s", exc)

    # ── News items ──

    async def save_news_items(self, items: list[NewsItem]) -> None:
        if not items:
            return
        rows = [_news_to_row(item) for item in items]
        async with self._connect() as db:
            await db.executemany(
                """
                INSERT OR REPLACE INTO news_items (
                    id, source, source_url, source_name, headline, summary,
                    hook_type, published_at, ingested_at,
                    mentions_json, fixture_ids_json, team_ids_json,
                    injury_details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()

    async def get_news_item(self, news_item_id: str) -> Optional[NewsItem]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM news_items WHERE id = ?", (news_item_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_news(row) if row else None

    async def latest_news_ingested_at(self, fixture_id: str) -> Optional[float]:
        """Return the epoch-seconds of the freshest news signal for a fixture.

        Used by the tier-loop boot-freshness skip: if the freshest news is
        newer than the tier's cadence, we skip the scout entirely (candidates
        + prices rebuild from cache, no LLM cost).

        We check TWO tables and take MAX:
        1. `news_items.ingested_at` — set when items are INSERTed (cache
           miss path, i.e. genuinely-new stories).
        2. `ingest_cache.ingested_at` — set whenever the per-fixture cache
           row is written OR touched by `touch_ingest_cache` on a cache
           hit (PR fix/cost-leak-freshness-cooldowns). Captures every
           scout pass, including pure cache-hit replays — without this
           the timestamp only advances when the LLM is actually called,
           and `skipped_fresh` stays at 0 forever in the tier loop.

        Returns None if neither table has a record for this fixture.
        """
        if not fixture_id:
            return None
        pattern = f'%"{fixture_id}"%'
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT MAX(ts) FROM (
                    SELECT MAX(ingested_at) AS ts FROM news_items
                    WHERE fixture_ids_json LIKE ?
                    UNION ALL
                    SELECT MAX(ingested_at) AS ts FROM ingest_cache
                    WHERE fixture_id = ?
                )
                """,
                (pattern, fixture_id),
            ) as cur:
                row = await cur.fetchone()
        if not row or row[0] is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    async def is_fixture_news_fresh(
        self, fixture_id: str, ttl_seconds: float,
    ) -> tuple[bool, Optional[float]]:
        """Single source of truth for "is this fixture's news fresh?".

        Reads from `ingest_cache.ingested_at` (always-bumped on every scout
        pass, including cache-hit replays via `touch_ingest_cache`) and
        compares to `now() - ttl_seconds`. Falls back to
        `news_items.ingested_at` for fixtures whose ingest_cache row
        somehow predates the touch fix.

        Returns (is_fresh, age_seconds). age is None when there's no record.
        """
        latest = await self.latest_news_ingested_at(fixture_id)
        if latest is None:
            return False, None
        import time as _t
        age = _t.time() - latest
        return (age < ttl_seconds), age

    async def touch_ingest_cache(
        self, fixture_id: str, cache_key: str,
    ) -> None:
        """Bump `ingest_cache.ingested_at` for an existing row without
        rewriting the payload. Called from the news ingester's cache-hit
        path so the freshness timestamp advances on EVERY scout pass, not
        just LLM-call ones.

        No-op if the row doesn't exist (defensive — UPDATE without an
        INSERT preserves the contract that the cache write path is the
        only thing that creates rows).
        """
        if not fixture_id or not cache_key:
            return
        import time as _t
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE ingest_cache
                SET ingested_at = ?
                WHERE fixture_id = ? AND cache_key = ?
                """,
                (_t.time(), fixture_id, cache_key),
            )
            await db.commit()

    # ── Candidates ──

    async def save_candidates(self, candidates: list[CandidateCard]) -> None:
        if not candidates:
            return
        rows = [_candidate_to_row(c) for c in candidates]
        async with self._connect() as db:
            await db.executemany(
                """
                INSERT OR REPLACE INTO candidates (
                    id, created_at, expires_at, news_item_id, hook_type,
                    bet_type, game_id, market_ids_json, selection_ids_json,
                    score, threshold_passed, reason, status, narrative,
                    supporting_stats_json, total_odds, price_source,
                    virtual_selection, storyline_id, bscode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()

    async def list_candidates(
        self,
        *,
        status: Optional[str] = None,
        hook_type: Optional[str] = None,
        game_id: Optional[str] = None,
        above_threshold_only: bool = False,
        limit: int = 500,
    ) -> list[CandidateCard]:
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if hook_type:
            where.append("hook_type = ?")
            args.append(hook_type)
        if game_id:
            where.append("game_id = ?")
            args.append(game_id)
        if above_threshold_only:
            where.append("threshold_passed = 1")
        sql = "SELECT * FROM candidates"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [_row_to_candidate(r) for r in rows]

    async def save_review(
        self,
        *,
        candidate_id: str,
        verdict: str,
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
        reviewer: Optional[str] = None,
    ) -> None:
        import time as _t
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO candidate_reviews
                    (candidate_id, verdict, reason_code, note, reviewer, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (candidate_id, verdict, reason_code, note, reviewer or "anonymous", _t.time()),
            )
            await db.commit()

    async def expire_published_candidates(self) -> int:
        """Mark all currently-published candidates as EXPIRED.

        Used at the start of each candidate-engine rerun so the publish
        loop only sees the freshly-generated batch — without this, every
        rerun stacks on top of prior runs (boot's candidates + rerun's
        candidates all visible at once, doubling card count per cycle).

        Historical rows are kept (status=EXPIRED) so the admin table can
        still show what was published in past cycles.
        """
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE candidates SET status = 'expired' WHERE status = 'published'"
            )
            await db.commit()
            return cur.rowcount or 0

    async def expire_published_for_fixtures(self, fixture_ids: list[str]) -> int:
        """Mark published candidates belonging to the given fixtures as EXPIRED.

        Scoped variant of `expire_published_candidates`: a tier-scoped
        re-scout re-expires only its own fixtures so other tiers' live
        cards stay visible.
        """
        if not fixture_ids:
            return 0
        placeholders = ",".join("?" for _ in fixture_ids)
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE candidates SET status = 'expired' "
                f"WHERE status = 'published' AND game_id IN ({placeholders})",
                fixture_ids,
            )
            await db.commit()
            return cur.rowcount or 0

    async def latest_verdict_by_candidate(self) -> dict[str, str]:
        """Return {candidate_id: latest_verdict} for fast render in the admin table."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT r.candidate_id, r.verdict
                FROM candidate_reviews r
                INNER JOIN (
                    SELECT candidate_id, MAX(created_at) AS max_at
                    FROM candidate_reviews
                    GROUP BY candidate_id
                ) latest ON latest.candidate_id = r.candidate_id
                         AND latest.max_at = r.created_at
                """
            ) as cur:
                rows = await cur.fetchall()
        return {r["candidate_id"]: r["verdict"] for r in rows}

    async def review_summary(self) -> dict[str, Any]:
        """Aggregate review stats for the admin dashboard."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT verdict, COUNT(*) AS n FROM candidate_reviews GROUP BY verdict") as cur:
                verdicts = {r["verdict"]: r["n"] for r in await cur.fetchall()}
            async with db.execute(
                """
                SELECT reason_code, COUNT(*) AS n FROM candidate_reviews
                WHERE verdict = 'bad' AND reason_code IS NOT NULL
                GROUP BY reason_code ORDER BY n DESC
                """
            ) as cur:
                reasons = [dict(r) for r in await cur.fetchall()]
        total = sum(verdicts.values())
        return {
            "total_reviews": total,
            "good": verdicts.get("good", 0),
            "bad": verdicts.get("bad", 0),
            "bad_pct": round(100 * verdicts.get("bad", 0) / total, 1) if total else 0.0,
            "top_bad_reasons": reasons[:5],
        }

    async def counts_by_hook_and_status(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT hook_type, status, COUNT(*) AS n
                FROM candidates
                GROUP BY hook_type, status
                ORDER BY hook_type, status
                """
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Storyline items ──

    async def store_storyline(
        self,
        storyline: StorylineItem,
        *,
        title: Optional[str] = None,
        expires_at: Optional[float] = None,
        status: str = "active",
    ) -> None:
        """Persist a detected cross-event storyline.

        `title` overrides the headline_hint when provided (e.g. the
        CombinedNarrativeAuthor's synthesised headline); otherwise we
        fall back to the detector hint. Idempotent via INSERT OR REPLACE
        on id.
        """
        fixture_ids = [p.fixture_id for p in storyline.participants if p.fixture_id]
        players = [
            {
                "player_name": p.player_name,
                "team_name": p.team_name,
                "fixture_id": p.fixture_id,
                "extra": p.extra,
            }
            for p in storyline.participants
        ]
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO storyline_items (
                    id, storyline_type, title, summary,
                    participating_fixture_ids_json,
                    participating_players_json,
                    generated_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    storyline.id,
                    storyline.storyline_type.value,
                    (title or storyline.headline_hint or "").strip(),
                    storyline.headline_hint or "",
                    json.dumps(fixture_ids),
                    json.dumps(players),
                    storyline.detected_at,
                    expires_at,
                    status,
                ),
            )
            await db.commit()

    async def get_storylines(
        self,
        *,
        limit: int = 100,
        status: Optional[str] = None,
        storyline_type: Optional[str] = None,
    ) -> list[StorylineItem]:
        """Read back persisted storylines (newest first)."""
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if storyline_type:
            where.append("storyline_type = ?")
            args.append(storyline_type)
        sql = "SELECT * FROM storyline_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY generated_at DESC LIMIT ?"
        args.append(limit)
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [_row_to_storyline(r) for r in rows]

    # ── Ingest cache ──

    async def get_cached_ingest(
        self, fixture_id: str, cache_key: str, max_age_seconds: float
    ) -> Optional[list[dict[str, Any]]]:
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT ingested_at, payload_json FROM ingest_cache
                WHERE fixture_id = ? AND cache_key = ?
                """,
                (fixture_id, cache_key),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        ingested_at, payload_json = row
        import time as _t
        if _t.time() - ingested_at > max_age_seconds:
            return None
        try:
            return json.loads(payload_json)
        except Exception:
            return None

    async def save_cached_ingest(
        self, fixture_id: str, cache_key: str, payload: list[dict[str, Any]]
    ) -> None:
        import time as _t
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO ingest_cache
                (fixture_id, cache_key, ingested_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (fixture_id, cache_key, _t.time(), json.dumps(payload)),
            )
            await db.commit()

    # ── Card reactions (public thumbs-up/down) ──

    async def save_reaction(
        self, *, card_id: str, anon_id: str, reaction: str,
    ) -> dict[str, int]:
        """Upsert the caller's reaction for this card and return fresh totals.

        `reaction` must be 'up' or 'down' (pydantic validates upstream; the
        DB CHECK constraint is a second fence). Same (anon_id, card_id)
        voting again flips the stored reaction — enforced via the UNIQUE
        index + `ON CONFLICT DO UPDATE`.
        """
        import time as _t
        if reaction not in ("up", "down"):
            raise ValueError(f"reaction must be 'up'|'down', got {reaction!r}")
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO card_reactions (card_id, anon_id, reaction, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(anon_id, card_id) DO UPDATE SET
                    reaction = excluded.reaction,
                    created_at = excluded.created_at
                """,
                (card_id, anon_id, reaction, _t.time()),
            )
            await db.commit()
        return await self.reaction_totals(card_id)

    async def reaction_totals(self, card_id: str) -> dict[str, int]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT reaction, COUNT(*) AS n
                FROM card_reactions
                WHERE card_id = ?
                GROUP BY reaction
                """,
                (card_id,),
            ) as cur:
                rows = await cur.fetchall()
        counts = {r["reaction"]: int(r["n"]) for r in rows}
        return {"up": counts.get("up", 0), "down": counts.get("down", 0)}

    async def reaction_for_anon(
        self, card_id: str, anon_id: str,
    ) -> Optional[str]:
        """The caller's own stored reaction for this card, or None."""
        if not anon_id:
            return None
        async with self._connect() as db:
            async with db.execute(
                "SELECT reaction FROM card_reactions WHERE card_id = ? AND anon_id = ?",
                (card_id, anon_id),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    # ── Card clicks (Stage 5 CTA deep-link) ──

    async def save_click(
        self, *, card_id: str, anon_id: "str | None",
    ) -> None:
        """Record a single CTA click. Multiple clicks per (card, anon) are
        kept — each is one distinct deep-link open, useful for seeing
        abandonment vs repeat-tap patterns."""
        import time as _t
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO card_clicks (card_id, anon_id, created_at) "
                "VALUES (?, ?, ?)",
                (card_id, anon_id or None, _t.time()),
            )
            await db.commit()

    async def click_totals(self) -> dict[str, int]:
        """Total clicks per card_id. Returned as {card_id: count} dict."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT card_id, COUNT(*) AS n FROM card_clicks GROUP BY card_id"
            ) as cur:
                rows = await cur.fetchall()
        return {r["card_id"]: int(r["n"]) for r in rows}

    # ── Rewrite cache (U3 — narrative rewriter memoisation) ──

    async def get_rewrite_cache(
        self, key: str, *, max_age_seconds: float,
    ) -> Optional[dict[str, str]]:
        """Read a cached rewrite by key. Returns None on miss or TTL expiry.

        The hash is computed by NarrativeRewriter from
        (bet_type, hook_type, headline, legs_csv, news_mentions) — pure
        thesis, no price. Pre-redesign the key included `total_odds`,
        which churned on every SSE pricing tick and made the cache miss
        every cycle (the $407/6d leak). See cost-aware-redesign.md.
        """
        import time as _t
        cutoff = _t.time() - max_age_seconds
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT headline, angle FROM rewrite_cache
                WHERE key = ? AND created_at > ?
                """,
                (key, cutoff),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return {"headline": row[0] or "", "angle": row[1] or ""}

    async def save_rewrite_cache(
        self, *, key: str, headline: str, angle: str, model: str,
    ) -> None:
        """Upsert a fresh rewrite into the cache. Idempotent via PK on key."""
        import time as _t
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO rewrite_cache
                    (key, headline, angle, model, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, headline, angle, model, _t.time()),
            )
            await db.commit()

    # ── Daily LLM cost tripwire (cost-aware redesign, 2026-04-26) ──

    async def get_daily_cost_total(self, date: str) -> float:
        """Return accumulated USD cost for a UTC day (`YYYY-MM-DD`).

        Returns 0.0 if the day has no row yet (first call of the day).
        """
        async with self._connect() as db:
            async with db.execute(
                "SELECT accumulated_usd FROM daily_cost WHERE date = ?",
                (date,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return 0.0
        try:
            return float(row[0] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def get_daily_cost_calls(self, date: str) -> int:
        """Return the call counter for a UTC day. 0 if no row."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT calls FROM daily_cost WHERE date = ?",
                (date,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row[0] or 0)
        except (TypeError, ValueError):
            return 0

    async def add_daily_cost(
        self, date: str, usd_delta: float, *, calls_delta: int = 1,
    ) -> None:
        """Atomically add a cost delta to today's row. Creates the row on
        first call of the day.

        Uses `INSERT ... ON CONFLICT DO UPDATE` so two concurrent tier
        loops don't race-overwrite each other's deltas — the upsert is a
        single statement.
        """
        import time as _t
        usd = max(0.0, float(usd_delta or 0.0))
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO daily_cost (date, accumulated_usd, calls, last_call_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    accumulated_usd = accumulated_usd + excluded.accumulated_usd,
                    calls = calls + excluded.calls,
                    last_call_at = excluded.last_call_at
                """,
                (date, usd, int(calls_delta), _t.time()),
            )
            await db.commit()

    async def add_daily_cost_by_kind(
        self,
        utc_date: str,
        kind: str,
        usd: float,
        *,
        calls_delta: int = 1,
    ) -> None:
        """Atomically add a per-`kind` cost row for `utc_date`.

        Mirrors `add_daily_cost`'s upsert pattern but bucketed on
        (utc_date, kind). Two concurrent tier loops or a boot-scout +
        scheduled-rerun overlap can both append safely — the
        `ON CONFLICT(utc_date, kind) DO UPDATE` is a single statement.
        """
        u = max(0.0, float(usd or 0.0))
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO daily_cost_by_kind (utc_date, kind, usd, calls)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(utc_date, kind) DO UPDATE SET
                    usd = usd + excluded.usd,
                    calls = calls + excluded.calls
                """,
                (utc_date, str(kind), u, int(calls_delta)),
            )
            await db.commit()

    async def get_daily_cost_by_kind(
        self, utc_date: str,
    ) -> dict[str, dict[str, float | int]]:
        """Return the per-kind breakdown for a UTC day.

        Shape: ``{kind: {"usd": float, "calls": int}, ...}``. Empty dict
        if the day has no rows yet. Powers the upcoming
        `/admin/cost.json?detail=1` endpoint (separate PR) and ops-bot's
        `/breakdown` command.
        """
        async with self._connect() as db:
            async with db.execute(
                "SELECT kind, usd, calls FROM daily_cost_by_kind WHERE utc_date = ?",
                (utc_date,),
            ) as cur:
                rows = await cur.fetchall()
        out: dict[str, dict[str, float | int]] = {}
        for r in rows or []:
            try:
                out[str(r[0])] = {
                    "usd": float(r[1] or 0.0),
                    "calls": int(r[2] or 0),
                }
            except (TypeError, ValueError):
                continue
        return out

    async def get_daily_cost_history(self, days: int = 7) -> list[dict[str, Any]]:
        """Return the most recent `days` rows of daily cost telemetry.

        Powers the `/admin/cost` page. Sorted newest-first.
        """
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT date, accumulated_usd, calls, last_call_at
                FROM daily_cost
                ORDER BY date DESC
                LIMIT ?
                """,
                (max(1, int(days)),),
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows or []:
            out.append({
                "date": r[0],
                "accumulated_usd": float(r[1] or 0.0),
                "calls": int(r[2] or 0),
                "last_call_at": float(r[3]) if r[3] is not None else None,
            })
        return out

    async def reaction_aggregates(self) -> list[dict[str, Any]]:
        """Aggregate reactions by (fixture, hook_type, bet_type, storyline) for
        narrative-engine cards only — i.e. rows that JOIN cleanly to the
        `candidates` table.

        Featured BBs bypass candidate_store entirely (built at runtime in
        services/featured_bb.py), so their reactions don't JOIN. Those are
        reported separately via `reaction_aggregates_orphan()` so the admin
        surface can split them into their own cohort — a straight LEFT JOIN
        collapses them all into one (None, None, None, None) row that's
        useless for analysis.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    c.game_id        AS fixture,
                    c.hook_type      AS hook_type,
                    c.bet_type       AS bet_type,
                    c.storyline_id   AS storyline,
                    SUM(CASE WHEN r.reaction = 'up' THEN 1 ELSE 0 END) AS up,
                    SUM(CASE WHEN r.reaction = 'down' THEN 1 ELSE 0 END) AS down
                FROM card_reactions r
                INNER JOIN candidates c ON c.id = r.card_id
                GROUP BY c.game_id, c.hook_type, c.bet_type, c.storyline_id
                ORDER BY (
                    SUM(CASE WHEN r.reaction = 'up' THEN 1 ELSE 0 END)
                  + SUM(CASE WHEN r.reaction = 'down' THEN 1 ELSE 0 END)
                ) DESC
                """,
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "fixture": r["fixture"],
                "hook_type": r["hook_type"],
                "bet_type": r["bet_type"],
                "storyline": r["storyline"],
                "up": int(r["up"] or 0),
                "down": int(r["down"] or 0),
            })
        return out

    async def reaction_aggregates_orphan(self) -> list[dict[str, Any]]:
        """Reactions on card_ids NOT present in `candidates` — i.e. featured
        BBs from the operator-curated feed, which bypass candidate_store.

        Grouped by card_id since we have no other metadata; the card_id prefix
        (e.g. `featured_`) is the only cohort clue we can surface. Sorted by
        total desc.
        """
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    r.card_id AS card_id,
                    SUM(CASE WHEN r.reaction = 'up' THEN 1 ELSE 0 END) AS up,
                    SUM(CASE WHEN r.reaction = 'down' THEN 1 ELSE 0 END) AS down
                FROM card_reactions r
                WHERE r.card_id NOT IN (SELECT id FROM candidates)
                GROUP BY r.card_id
                ORDER BY (
                    SUM(CASE WHEN r.reaction = 'up' THEN 1 ELSE 0 END)
                  + SUM(CASE WHEN r.reaction = 'down' THEN 1 ELSE 0 END)
                ) DESC
                """,
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "card_id": r["card_id"],
                "up": int(r["up"] or 0),
                "down": int(r["down"] or 0),
            }
            for r in rows
        ]

    async def click_totals_orphan(self) -> int:
        """Total clicks on card_ids NOT present in `candidates`. Single scalar
        — we surface featured-BB clicks as one aggregate since we don't yet
        split by fixture for those."""
        async with self._connect() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM card_clicks "
                "WHERE card_id NOT IN (SELECT id FROM candidates)"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def click_totals_by_card(self) -> dict[str, int]:
        """Clicks per card_id for cards NOT in the candidates table (featured
        BBs). Lets the admin page show per-featured-card CTR."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT card_id, COUNT(*) AS n FROM card_clicks "
                "WHERE card_id NOT IN (SELECT id FROM candidates) "
                "GROUP BY card_id"
            ) as cur:
                rows = await cur.fetchall()
        return {r["card_id"]: int(r["n"]) for r in rows}

    # ── Published-card snapshots (cold-start rehydrate) ──

    async def upsert_published_card(
        self,
        *,
        card_id: str,
        snapshot_json: str,
        candidate_id: Optional[str] = None,
        expires_at: Optional[float] = None,
        bet_type: Optional[str] = None,
        storyline_id: Optional[str] = None,
    ) -> None:
        """Persist a fully-rendered Card snapshot.

        Idempotent on `card_id` (PRIMARY KEY) — repeated upserts of the
        same card simply refresh `snapshotted_at`. Called on every
        `feed.add_prematch_card(...)` so rehydrate after a redeploy can
        read serialized Card JSON directly with no catalog dependency.
        Best-effort: callers wrap this in a try/except so a snapshot
        failure never blocks the publish path.
        """
        import time as _t
        async with self._connect() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO published_cards (
                    card_id, candidate_id, snapshot_json, snapshotted_at,
                    expires_at, bet_type, storyline_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    candidate_id,
                    snapshot_json,
                    _t.time(),
                    expires_at,
                    bet_type,
                    storyline_id,
                ),
            )
            await db.commit()

    async def list_published_cards(
        self, limit: int = 200,
    ) -> list[tuple[str, str, Optional[float]]]:
        """Return `(card_id, snapshot_json, expires_at)` rows ordered by
        snapshotted_at DESC. Powers cold-start rehydrate."""
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT card_id, snapshot_json, expires_at
                FROM published_cards
                ORDER BY snapshotted_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ) as cur:
                rows = await cur.fetchall()
        out: list[tuple[str, str, Optional[float]]] = []
        for r in rows or []:
            try:
                exp = float(r[2]) if r[2] is not None else None
            except (TypeError, ValueError):
                exp = None
            out.append((r[0], r[1], exp))
        return out

    # ── Embeds (per-operator widget registration) ──

    async def list_embeds(self, active_only: bool = False) -> list[Embed]:
        """All embeds, newest-first by created_at. `active_only` drops
        soft-deleted rows."""
        sql = "SELECT * FROM embeds"
        args: list[Any] = []
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY created_at DESC"
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [_row_to_embed(r) for r in rows]

    async def get_embed_by_token(self, token: str) -> Optional[Embed]:
        """Lookup by primary-key token. Returns inactive rows too — the
        middleware checks `.active` itself so a clear 401 reason is
        possible. None on miss."""
        if not token:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM embeds WHERE token = ?", (token,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_embed(row) if row else None

    async def get_embed_by_slug(self, slug: str) -> Optional[Embed]:
        """Lookup by the human-handle slug. None on miss."""
        if not slug:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM embeds WHERE slug = ?", (slug,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_embed(row) if row else None

    async def create_embed(
        self,
        *,
        slug: str,
        display_name: str,
        allowed_origins: list[str],
        token: Optional[str] = None,
        theme_overrides: Optional[dict] = None,
        notes: Optional[str] = None,
        active: bool = True,
    ) -> Embed:
        """Insert a new embed row. Auto-generates a token if not supplied.

        Raises sqlite3.IntegrityError on slug collision (UNIQUE index).
        Caller surfaces the conflict — we don't swallow it here so the
        admin form can render a clear "slug already exists" message.
        """
        import secrets as _secrets
        from datetime import datetime, timezone
        tok = token or _secrets.token_urlsafe(32)
        created_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        embed = Embed(
            token=tok,
            slug=slug,
            display_name=display_name,
            allowed_origins=list(allowed_origins or []),
            theme_overrides=dict(theme_overrides or {}),
            active=bool(active),
            created_at=created_at,
            notes=notes,
        )
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO embeds (
                    token, slug, display_name, allowed_origins,
                    theme_overrides, active, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _embed_to_row(embed),
            )
            await db.commit()
        return embed

    async def update_embed(self, slug: str, **fields: Any) -> Embed:
        """Patch an embed row in place. Only the keys present in `fields`
        are touched. Returns the refreshed row.

        Supported keys: display_name, allowed_origins, theme_overrides,
        active, notes, token. Unknown keys are silently ignored.
        """
        existing = await self.get_embed_by_slug(slug)
        if existing is None:
            raise KeyError(f"embed slug not found: {slug!r}")
        allowed = {
            "display_name", "allowed_origins", "theme_overrides",
            "active", "notes", "token",
        }
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "allowed_origins":
                sets.append("allowed_origins = ?")
                args.append(json.dumps(list(value or [])))
            elif key == "theme_overrides":
                sets.append("theme_overrides = ?")
                args.append(json.dumps(dict(value or {})))
            elif key == "active":
                sets.append("active = ?")
                args.append(1 if bool(value) else 0)
            else:
                sets.append(f"{key} = ?")
                args.append(value)
        if not sets:
            return existing
        args.append(slug)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE embeds SET {', '.join(sets)} WHERE slug = ?",
                args,
            )
            await db.commit()
        refreshed = await self.get_embed_by_slug(slug)
        assert refreshed is not None
        return refreshed

    async def rotate_embed_token(self, slug: str) -> Embed:
        """Generate a fresh urlsafe token for an embed. Old token is
        invalidated immediately because token is the primary key."""
        import secrets as _secrets
        existing = await self.get_embed_by_slug(slug)
        if existing is None:
            raise KeyError(f"embed slug not found: {slug!r}")
        new_token = _secrets.token_urlsafe(32)
        # Token is the PK so we can't UPDATE it cleanly across rows; do
        # delete + insert in a single connection so we don't leave the
        # row missing if a crash hits between statements.
        from datetime import datetime, timezone  # noqa — used by created_at preserve
        async with self._connect() as db:
            await db.execute("DELETE FROM embeds WHERE slug = ?", (slug,))
            new_embed = Embed(
                token=new_token,
                slug=existing.slug,
                display_name=existing.display_name,
                allowed_origins=existing.allowed_origins,
                theme_overrides=existing.theme_overrides,
                active=existing.active,
                created_at=existing.created_at,
                notes=existing.notes,
            )
            await db.execute(
                """
                INSERT INTO embeds (
                    token, slug, display_name, allowed_origins,
                    theme_overrides, active, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _embed_to_row(new_embed),
            )
            await db.commit()
        return new_embed

    async def soft_delete_embed(self, slug: str) -> None:
        """Set active=0. Row stays in the DB so the admin page can show
        history; middleware rejects inactive tokens."""
        async with self._connect() as db:
            await db.execute(
                "UPDATE embeds SET active = 0 WHERE slug = ?", (slug,),
            )
            await db.commit()

    async def delete_expired_published_cards(self, now_ts: float) -> int:
        """Drop snapshot rows whose `expires_at` is in the past.

        TTL sweeper hook — not wired into a periodic task in this PR,
        but kept here so a future sweep loop can call it directly.
        Rows with NULL expires_at never expire by this method.
        """
        async with self._connect() as db:
            cur = await db.execute(
                """
                DELETE FROM published_cards
                WHERE expires_at IS NOT NULL AND expires_at < ?
                """,
                (float(now_ts),),
            )
            await db.commit()
            return cur.rowcount or 0


# ── Row <-> model helpers ──

def _news_to_row(item: NewsItem) -> tuple:
    return (
        item.id,
        item.source,
        item.source_url,
        item.source_name,
        item.headline,
        item.summary,
        item.hook_type.value,
        item.published_at,
        item.ingested_at,
        json.dumps(item.mentions),
        json.dumps(item.fixture_ids),
        json.dumps(item.team_ids),
        json.dumps(item.injury_details),
    )


def _row_to_news(row: aiosqlite.Row) -> NewsItem:
    # Guarded read — injury_details_json was added 2026-04-23 and may not
    # exist in an in-flight migration window.
    def _get(col, default=None):
        try:
            return row[col]
        except (IndexError, KeyError):
            return default
    return NewsItem(
        id=row["id"],
        source=row["source"] or "",
        source_url=row["source_url"] or "",
        source_name=row["source_name"] or "",
        headline=row["headline"] or "",
        summary=row["summary"] or "",
        hook_type=_safe_enum(HookType, row["hook_type"], HookType.OTHER),
        published_at=row["published_at"] or "",
        ingested_at=row["ingested_at"] or 0.0,
        mentions=_safe_json_list(row["mentions_json"]),
        fixture_ids=_safe_json_list(row["fixture_ids_json"]),
        team_ids=_safe_json_list(row["team_ids_json"]),
        injury_details=_safe_json_list(_get("injury_details_json")),
    )


def _candidate_to_row(c: CandidateCard) -> tuple:
    return (
        c.id,
        c.created_at,
        c.expires_at,
        c.news_item_id,
        c.hook_type.value,
        c.bet_type.value,
        c.game_id,
        json.dumps(c.market_ids),
        json.dumps(c.selection_ids),
        c.score,
        1 if c.threshold_passed else 0,
        c.reason,
        c.status.value,
        c.narrative,
        c.supporting_stats_json,
        c.total_odds,
        c.price_source,
        c.virtual_selection,
        c.storyline_id,
        c.bscode,
    )


def _row_to_candidate(row: aiosqlite.Row) -> CandidateCard:
    # `row["col"]` raises IndexError (not KeyError) if the column is missing,
    # which can happen during an in-flight migration — guard both fields.
    def _get(col, default=None):
        try:
            return row[col]
        except (IndexError, KeyError):
            return default
    return CandidateCard(
        id=row["id"],
        created_at=row["created_at"] or 0.0,
        expires_at=row["expires_at"] or 0.0,
        news_item_id=row["news_item_id"],
        hook_type=_safe_enum(HookType, row["hook_type"], HookType.OTHER),
        bet_type=_safe_enum(BetType, row["bet_type"], BetType.SINGLE),
        game_id=row["game_id"] or "",
        market_ids=_safe_json_list(row["market_ids_json"]),
        selection_ids=_safe_json_list(row["selection_ids_json"]),
        score=row["score"] or 0.0,
        threshold_passed=bool(row["threshold_passed"]),
        reason=row["reason"] or "",
        status=_safe_enum(CandidateStatus, row["status"], CandidateStatus.DRAFT),
        narrative=row["narrative"] or "",
        supporting_stats_json=row["supporting_stats_json"] or "",
        total_odds=_get("total_odds"),
        price_source=_get("price_source"),
        virtual_selection=_get("virtual_selection"),
        storyline_id=_get("storyline_id"),
        bscode=_get("bscode"),
    )


def _row_to_storyline(row: aiosqlite.Row) -> StorylineItem:
    raw_players = _safe_json_list(row["participating_players_json"])
    participants: list[StorylineParticipant] = []
    for p in raw_players:
        if not isinstance(p, dict):
            continue
        participants.append(StorylineParticipant(
            player_name=str(p.get("player_name") or ""),
            team_name=str(p.get("team_name") or ""),
            fixture_id=str(p.get("fixture_id") or ""),
            extra=str(p.get("extra") or ""),
        ))
    return StorylineItem(
        id=row["id"],
        storyline_type=_safe_enum(
            StorylineType, row["storyline_type"], StorylineType.GOLDEN_BOOT,
        ),
        headline_hint=row["summary"] or "",
        participants=participants,
        detected_at=row["generated_at"] or 0.0,
    )


def _embed_to_row(embed: Embed) -> tuple:
    """Pydantic Embed → SQLite row tuple. Mirrors the column order in the
    INSERT statements in `create_embed` / `rotate_embed_token`."""
    return (
        embed.token,
        embed.slug,
        embed.display_name,
        json.dumps(list(embed.allowed_origins or [])),
        json.dumps(dict(embed.theme_overrides or {})) if embed.theme_overrides else None,
        1 if embed.active else 0,
        embed.created_at,
        embed.notes,
    )


def _row_to_embed(row: aiosqlite.Row) -> Embed:
    """SQLite row → Embed. Tolerant of NULL/blank JSON columns."""
    def _get(col, default=None):
        try:
            return row[col]
        except (IndexError, KeyError):
            return default
    raw_origins = _get("allowed_origins")
    origins: list[str] = []
    if raw_origins:
        try:
            parsed = json.loads(raw_origins)
            if isinstance(parsed, list):
                origins = [str(x) for x in parsed if isinstance(x, str)]
        except Exception:
            origins = []
    raw_theme = _get("theme_overrides")
    theme: dict = {}
    if raw_theme:
        try:
            parsed = json.loads(raw_theme)
            if isinstance(parsed, dict):
                theme = parsed
        except Exception:
            theme = {}
    return Embed(
        token=row["token"] or "",
        slug=row["slug"] or "",
        display_name=row["display_name"] or "",
        allowed_origins=origins,
        theme_overrides=theme,
        active=bool(row["active"]),
        created_at=row["created_at"] or "",
        notes=_get("notes"),
    )


def _safe_enum(enum_cls, value, default):
    try:
        return enum_cls(value)
    except Exception:
        return default


def _safe_json_list(value: Optional[str]) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []
