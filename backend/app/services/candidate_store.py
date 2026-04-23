"""SQLite-backed store for NewsItem + CandidateCard.

Thin wrapper over aiosqlite. Minimal schema on purpose — we'll migrate to
Postgres (Stage 9-ish) once concurrent writes and cross-process access
matter. Until then this is a file on disk the engine writes to and the
admin table reads from.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.models.news import (
    BetType,
    CandidateCard,
    CandidateStatus,
    HookType,
    NewsItem,
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
    FOREIGN KEY (news_item_id) REFERENCES news_items(id)
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
"""


class CandidateStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            # Schema migrations — additive only, idempotent. The candidates
            # table grew over multiple PRs:
            #   - PR #8 added total_odds + price_source
            #   - PR #16 (this) adds virtual_selection
            # We use ALTER TABLE ADD COLUMN (preserves existing rows) when
            # only the newest column is missing, and fall back to a
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
                # Mid (post-PR-#8, pre-PR-#16) schema: ALTER ADD COLUMN
                elif "virtual_selection" not in cols:
                    logger.info(
                        "[CandidateStore] Migrating candidates: ADD COLUMN virtual_selection"
                    )
                    await db.execute("ALTER TABLE candidates ADD COLUMN virtual_selection TEXT")
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

            await db.executescript(_SCHEMA)
            await db.commit()

    # ── News items ──

    async def save_news_items(self, items: list[NewsItem]) -> None:
        if not items:
            return
        rows = [_news_to_row(item) for item in items]
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM news_items WHERE id = ?", (news_item_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_news(row) if row else None

    # ── Candidates ──

    async def save_candidates(self, candidates: list[CandidateCard]) -> None:
        if not candidates:
            return
        rows = [_candidate_to_row(c) for c in candidates]
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                """
                INSERT OR REPLACE INTO candidates (
                    id, created_at, expires_at, news_item_id, hook_type,
                    bet_type, game_id, market_ids_json, selection_ids_json,
                    score, threshold_passed, reason, status, narrative,
                    supporting_stats_json, total_odds, price_source,
                    virtual_selection
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE candidates SET status = 'expired' WHERE status = 'published'"
            )
            await db.commit()
            return cur.rowcount or 0

    async def latest_verdict_by_candidate(self) -> dict[str, str]:
        """Return {candidate_id: latest_verdict} for fast render in the admin table."""
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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

    # ── Ingest cache ──

    async def get_cached_ingest(
        self, fixture_id: str, cache_key: str, max_age_seconds: float
    ) -> Optional[list[dict[str, Any]]]:
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO ingest_cache
                (fixture_id, cache_key, ingested_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (fixture_id, cache_key, _t.time(), json.dumps(payload)),
            )
            await db.commit()


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
