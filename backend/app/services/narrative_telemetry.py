"""Narrative telemetry — capture thesis decisions + composed combos for
the self-learning loop.

A separate SQLite table that lives alongside `candidates` but doesn't
entangle with the `CandidateCard` pydantic model (avoids the recurring
schema-drift bug — `pulse-schema-drift-check`). Capture is opt-in:
`save_narrative_thesis()` is called explicitly by the composer when it
fires; today's hand-coded themes don't call it (we'd be guessing
their archetype).

## What we capture

  * Per-thesis: news_item_id, archetype, confidence, alternatives,
    subject info, resolved signals, matched keywords, is_uncertain
  * Per-composition: candidate_card_id (when the composer's combo gets
    promoted to a card), the chosen legs, signal overlap count, score

## What this enables (later, no model code yet)

  * Aggregate `archetype × engagement` once engagement events flow
  * Identify low-confidence theses that nonetheless produced
    high-engagement cards → promote keywords / refine matchers
  * Identify high-confidence theses that drove zero engagement →
    challenge the rule, look for over-firing patterns
  * Surface "we keep firing UNCATEGORISED on this news pattern" →
    new archetype candidates

## What this is NOT

  * Not a learning model. The data exists; the model is a future PR
    once we have ≥ 4 weeks of engagement data.
  * Not engagement capture. That's a separate `card_engagement`
    surface — wired via WebSocket / iframe events; lands when AT is
    live and the click attribution is agreed.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS narrative_theses (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_item_id                TEXT NOT NULL,
    captured_at                 REAL NOT NULL,
    archetype_key               TEXT,
    confidence                  REAL NOT NULL,
    alternatives_json           TEXT NOT NULL,
    subject_type                TEXT NOT NULL,
    subject_player_id           TEXT,
    subject_team_id             TEXT,
    subject_player_name         TEXT,
    fixture_id                  TEXT,
    resolved_signals_json       TEXT NOT NULL,
    matched_keywords_json       TEXT NOT NULL,
    is_uncertain                INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS narrative_theses_news_item_id_idx
    ON narrative_theses (news_item_id);

CREATE INDEX IF NOT EXISTS narrative_theses_archetype_key_idx
    ON narrative_theses (archetype_key);

CREATE TABLE IF NOT EXISTS narrative_compositions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id                   INTEGER NOT NULL,
    candidate_card_id           TEXT,
    captured_at                 REAL NOT NULL,
    legs_json                   TEXT NOT NULL,   -- list of {market_meta_key, direction, market_id, selection_id, selection_name}
    signal_overlap_count        INTEGER NOT NULL,
    archetype_affinity_total    REAL NOT NULL,
    conflict_penalty            INTEGER NOT NULL,
    orphan_legs                 INTEGER NOT NULL,
    score                       REAL NOT NULL,
    FOREIGN KEY (thesis_id) REFERENCES narrative_theses(id)
);

CREATE INDEX IF NOT EXISTS narrative_compositions_thesis_id_idx
    ON narrative_compositions (thesis_id);

CREATE INDEX IF NOT EXISTS narrative_compositions_candidate_card_id_idx
    ON narrative_compositions (candidate_card_id);
"""


class NarrativeTelemetry:
    """Async SQLite-backed capture for thesis + composition events."""

    def __init__(self, db_path: str):
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def save_thesis(self, thesis: Any) -> Optional[int]:
        """Persist a NarrativeThesis. Returns the inserted id (or None on
        failure — never raises; telemetry must not break the cycle).

        `thesis` is a `narrative_thesis.NarrativeThesis` — passed as
        Any to avoid an import cycle.
        """
        try:
            arch_key = thesis.archetype.key if thesis.archetype else None
            async with aiosqlite.connect(self._db_path) as db:
                cursor = await db.execute(
                    """INSERT INTO narrative_theses (
                        news_item_id, captured_at, archetype_key,
                        confidence, alternatives_json, subject_type,
                        subject_player_id, subject_team_id,
                        subject_player_name, fixture_id,
                        resolved_signals_json, matched_keywords_json,
                        is_uncertain
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        thesis.news_item_id, time.time(), arch_key,
                        float(thesis.confidence),
                        json.dumps(list(thesis.alternatives)),
                        thesis.subject_type,
                        thesis.subject_player_id,
                        thesis.subject_team_id,
                        thesis.subject_player_name,
                        thesis.fixture_id,
                        json.dumps(list(thesis.resolved_signals)),
                        json.dumps(list(thesis.matched_keywords)),
                        1 if thesis.is_uncertain else 0,
                    ),
                )
                await db.commit()
                return cursor.lastrowid
        except Exception:
            logger.exception("[narrative_telemetry] save_thesis failed (non-fatal)")
            return None

    async def save_composition(self, *, thesis_id: int,
                                candidate_card_id: Optional[str],
                                combination: Any) -> None:
        """Persist a Combination produced by the composer."""
        try:
            legs_payload = [
                {
                    "market_meta_key": l.market_meta_key,
                    "direction": l.direction,
                    "market_id": l.market_id,
                    "market_name": l.market_name,
                    "selection_id": l.selection_id,
                    "selection_name": l.selection_name,
                    "emitted_signals": list(l.emitted_signals),
                }
                for l in combination.legs
            ]
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT INTO narrative_compositions (
                        thesis_id, candidate_card_id, captured_at,
                        legs_json, signal_overlap_count,
                        archetype_affinity_total, conflict_penalty,
                        orphan_legs, score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        thesis_id, candidate_card_id, time.time(),
                        json.dumps(legs_payload),
                        int(combination.signal_overlap_count),
                        float(combination.archetype_affinity_total),
                        int(combination.conflict_penalty),
                        int(combination.orphan_legs),
                        float(combination.score),
                    ),
                )
                await db.commit()
        except Exception:
            logger.exception("[narrative_telemetry] save_composition failed (non-fatal)")

    async def archetype_summary(self) -> dict[str, Any]:
        """Diagnostic — count theses per archetype + uncertainty rate.

        Read from `/admin/narrative-telemetry` (next PR will add the
        endpoint). Useful for observability before any engagement
        signal exists.
        """
        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(
                    """SELECT archetype_key, COUNT(*) as n,
                              AVG(confidence) as avg_conf,
                              SUM(is_uncertain) as uncertain_n
                       FROM narrative_theses
                       GROUP BY archetype_key
                       ORDER BY n DESC"""
                ) as cursor:
                    rows = await cursor.fetchall()
            return {
                "by_archetype": [
                    {
                        "archetype_key": r[0] or "(uncategorised)",
                        "count": r[1],
                        "avg_confidence": round(r[2] or 0.0, 3),
                        "uncertain_count": r[3] or 0,
                    }
                    for r in rows
                ],
            }
        except Exception:
            logger.exception("[narrative_telemetry] summary failed (non-fatal)")
            return {"by_archetype": []}
