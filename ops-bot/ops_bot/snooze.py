"""
Snooze state for alert suppression.

Supported kinds: "cost", "health" (health alerts ship in Stage 2B).

Snooze rules are persisted to SQLite at /data/snooze.db so a redeploy
does not clear them. If the volume is absent or the DB cannot be opened,
the module falls back to in-memory only and logs a warning.

Public API
----------
snooze(kind, duration_seconds)   set or extend a snooze
is_snoozed(kind)                 -> bool
clear(kind)                      remove snooze for kind
current()                        -> dict[kind, {"expires_at": float, "remaining_seconds": int}]
"""
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VALID_KINDS = {"cost", "health"}

# In-memory state: kind -> expires_at (monotonic)
_snoozed: Dict[str, float] = {}

# SQLite persistence path.
_DB_PATH = os.environ.get("SNOOZE_DB_PATH", "/data/snooze.db")
_db_available = False


def _init_db() -> bool:
    """Attempt to open/create the SQLite DB. Returns True on success."""
    global _db_available
    try:
        db_dir = os.path.dirname(_DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snooze "
            "(kind TEXT PRIMARY KEY, expires_epoch REAL NOT NULL)"
        )
        conn.commit()
        conn.close()
        _db_available = True
        logger.info("snooze: SQLite DB ready at %s", _DB_PATH)
        return True
    except Exception as exc:
        logger.warning(
            "snooze: SQLite unavailable (%s) — using in-memory state only "
            "(snoozes will not survive a redeploy)",
            exc,
        )
        _db_available = False
        return False


def _load_from_db() -> None:
    """Load persisted snooze rules into _snoozed. Call once at startup."""
    if not _db_available:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute("SELECT kind, expires_epoch FROM snooze").fetchall()
        conn.close()
        now_epoch = time.time()
        now_mono = time.monotonic()
        loaded = 0
        for kind, expires_epoch in rows:
            if expires_epoch > now_epoch:
                # Convert wall-clock epoch back to monotonic offset.
                remaining = expires_epoch - now_epoch
                _snoozed[kind] = now_mono + remaining
                loaded += 1
        if loaded:
            logger.info("snooze: loaded %d active rule(s) from DB", loaded)
    except Exception as exc:
        logger.warning("snooze: failed to load from DB: %s", exc)


def _persist(kind: str, expires_at_mono: float) -> None:
    """Write a snooze entry to SQLite."""
    if not _db_available:
        return
    try:
        # Convert monotonic time to wall-clock epoch for persistence.
        now_mono = time.monotonic()
        now_epoch = time.time()
        remaining = expires_at_mono - now_mono
        expires_epoch = now_epoch + remaining
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO snooze (kind, expires_epoch) VALUES (?, ?)",
            (kind, expires_epoch),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("snooze: failed to persist %s: %s", kind, exc)


def _delete_from_db(kind: str) -> None:
    """Remove a snooze entry from SQLite."""
    if not _db_available:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("DELETE FROM snooze WHERE kind = ?", (kind,))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("snooze: failed to delete %s from DB: %s", kind, exc)


# ---------------------------------------------------------------------------
# Initialise on import
# ---------------------------------------------------------------------------

_init_db()
_load_from_db()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snooze(kind: str, duration_seconds: int) -> None:
    """Set or extend a snooze for kind by duration_seconds."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown snooze kind: {kind!r}. valid: {sorted(VALID_KINDS)}")
    expires_at = time.monotonic() + duration_seconds
    _snoozed[kind] = expires_at
    _persist(kind, expires_at)
    logger.info("snooze: %s snoozed for %ds", kind, duration_seconds)


def is_snoozed(kind: str) -> bool:
    """Return True if kind is currently snoozed."""
    expires_at = _snoozed.get(kind)
    if expires_at is None:
        return False
    if time.monotonic() < expires_at:
        return True
    # Expired — clean up.
    _snoozed.pop(kind, None)
    _delete_from_db(kind)
    return False


def clear(kind: str) -> None:
    """Remove snooze for kind. No-op if not snoozed."""
    _snoozed.pop(kind, None)
    _delete_from_db(kind)
    logger.info("snooze: %s cleared", kind)


def current() -> Dict[str, Any]:
    """
    Return a dict of currently active snoozes.
    Shape: {kind: {"expires_at": float (monotonic), "remaining_seconds": int}}
    Expired entries are pruned before returning.
    """
    now = time.monotonic()
    expired = [k for k, exp in _snoozed.items() if now >= exp]
    for k in expired:
        _snoozed.pop(k, None)
        _delete_from_db(k)

    result = {}
    for kind, expires_at in _snoozed.items():
        remaining = max(0, int(expires_at - now))
        result[kind] = {"expires_at": expires_at, "remaining_seconds": remaining}
    return result


def parse_duration(s: str) -> Optional[int]:
    """
    Parse a human duration string into seconds.

    Accepted: "30m", "1h", "2h", "off" (returns 0 meaning clear), "1.5h" etc.
    Returns None if the string is not recognised.
    """
    s = s.strip().lower()
    if s == "off":
        return 0
    if s.endswith("m"):
        try:
            return int(float(s[:-1]) * 60)
        except ValueError:
            return None
    if s.endswith("h"):
        try:
            return int(float(s[:-1]) * 3600)
        except ValueError:
            return None
    return None
