"""
Digest schedule configuration.

Default times: 09:00 UTC and 22:00 UTC.

Override via env var:
  OPS_BOT_DIGEST_TIMES_UTC=09:00,22:00

Each entry is "HH:MM" in 24-hour UTC.  Any number of times is accepted;
duplicates are silently deduplicated.
"""
import os
from typing import List, Tuple


def _parse_hhmm(s: str) -> Tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute).  Raises ValueError on bad input."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"out-of-range time {s!r}")
    return h, m


def get_digest_times_utc() -> List[Tuple[int, int]]:
    """
    Return list of (hour, minute) in UTC for the daily digest schedule.

    Reads OPS_BOT_DIGEST_TIMES_UTC env var; falls back to [(9, 0), (22, 0)].
    Deduplicates and sorts ascending.
    """
    raw = os.environ.get("OPS_BOT_DIGEST_TIMES_UTC", "").strip()
    if not raw:
        return [(9, 0), (22, 0)]

    times: List[Tuple[int, int]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            times.append(_parse_hhmm(token))
        except ValueError:
            # Silently skip malformed entries and use the default if empty.
            pass

    if not times:
        return [(9, 0), (22, 0)]

    # Sort and deduplicate.
    return sorted(set(times))
