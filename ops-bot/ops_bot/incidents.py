"""
In-memory incident log for /incident start / note / close.

One open incident per chat_id. No SQLite — in-memory only.

Public API
----------
start(chat_id, title)    -> slug (str)
note(chat_id, text)      -> True | raises NoOpenIncident
append_alert(chat_id, text) -> (no-op if no open incident)
close(chat_id)           -> IncidentLog | raises NoOpenIncident
get_open(chat_id)        -> IncidentLog | None
"""
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class NoOpenIncident(Exception):
    """Raised when an operation requires an open incident but none exists."""


@dataclass
class TimelineEntry:
    ts: str       # ISO UTC string e.g. "2026-04-29 18:30 UTC"
    kind: str     # "start" | "note" | "alert" | "close"
    text: str


@dataclass
class IncidentLog:
    chat_id: int
    slug: str
    title: str
    started_at: str        # ISO UTC string
    closed_at: Optional[str] = None
    timeline: List[TimelineEntry] = field(default_factory=list)


# chat_id -> open IncidentLog
_open: Dict[int, IncidentLog] = {}


def _now_utc() -> str:
    """Return a short UTC timestamp string: '2026-04-29 18:30 UTC'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _make_slug(title: str, dt: datetime) -> str:
    """
    Derive a URL-safe slug from title + date.

    e.g. "Cost spike LLM" + 2026-04-29 -> "2026-04-29-cost-spike-llm"
    """
    date_str = dt.strftime("%Y-%m-%d")
    safe = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not safe:
        safe = "incident"
    return f"{date_str}-{safe}"


def start(chat_id: int, title: str) -> str:
    """
    Register a new incident for chat_id with the given title.

    Returns the slug. Replaces any existing open incident (log is lost).
    """
    if not title or not title.strip():
        raise ValueError("incident title must not be empty")
    title = title.strip()
    now = datetime.now(timezone.utc)
    slug = _make_slug(title, now)
    ts = _now_utc()
    log = IncidentLog(
        chat_id=chat_id,
        slug=slug,
        title=title,
        started_at=ts,
    )
    log.timeline.append(TimelineEntry(ts=ts, kind="start", text=title))
    _open[chat_id] = log
    logger.info("incident: started %s for chat %s", slug, chat_id)
    return slug


def note(chat_id: int, text: str) -> bool:
    """
    Append a timestamped note to the open incident.

    Raises NoOpenIncident if no incident is open for chat_id.
    """
    log = _open.get(chat_id)
    if log is None:
        raise NoOpenIncident(f"no open incident for chat {chat_id}")
    ts = _now_utc()
    log.timeline.append(TimelineEntry(ts=ts, kind="note", text=text.strip()))
    logger.info("incident: note added to %s", log.slug)
    return True


def append_alert(chat_id: int, text: str) -> None:
    """
    Append an alert event to the open incident if one is active.

    No-op if no incident is open for chat_id.
    """
    log = _open.get(chat_id)
    if log is None:
        return
    ts = _now_utc()
    log.timeline.append(TimelineEntry(ts=ts, kind="alert", text=text.strip()))
    logger.debug("incident: alert appended to %s", log.slug)


def close(chat_id: int) -> IncidentLog:
    """
    Finalise and return the open incident for chat_id.

    Raises NoOpenIncident if no incident is open.
    """
    log = _open.pop(chat_id, None)
    if log is None:
        raise NoOpenIncident(f"no open incident for chat {chat_id}")
    ts = _now_utc()
    log.closed_at = ts
    log.timeline.append(TimelineEntry(ts=ts, kind="close", text=""))
    logger.info("incident: closed %s", log.slug)
    return log


def get_open(chat_id: int) -> Optional[IncidentLog]:
    """Return the open IncidentLog for chat_id, or None."""
    return _open.get(chat_id)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _duration_str(started_at: str, closed_at: str) -> str:
    """
    Return a human-readable duration like "1h 12m".

    Parses the short UTC strings emitted by _now_utc().
    Falls back to "(unknown)" on parse error.
    """
    try:
        fmt = "%Y-%m-%d %H:%M UTC"
        start_dt = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
        close_dt = datetime.strptime(closed_at, fmt).replace(tzinfo=timezone.utc)
        delta = close_dt - start_dt
        total_minutes = int(delta.total_seconds() // 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "(unknown)"


def render_markdown(log: IncidentLog) -> str:
    """
    Render an IncidentLog as a markdown postmortem file.

    Format matches the spec in DESIGN.md §9.
    """
    lines = [
        f"# {log.title}",
        "",
        f"- Started: {log.started_at}",
        f"- Closed:  {log.closed_at or '(open)'}",
    ]
    if log.closed_at:
        lines.append(f"- Duration: {_duration_str(log.started_at, log.closed_at)}")
    lines.append("")
    lines.append("## Timeline")

    for entry in log.timeline:
        if entry.kind == "close":
            lines.append(f"- {entry.ts} — close")
        else:
            lines.append(f"- {entry.ts} — {entry.kind}: {entry.text}")

    lines.extend([
        "",
        "## Resolution",
        "(Ian fills this in by editing the file post-incident.)",
    ])

    return "\n".join(lines)
