"""
Pending-confirm state for destructive actions.

Each chat can have at most one pending confirmation at a time.
A new confirm registration for the same chat silently replaces the previous one.

TTL: 30 seconds. After expiry, resolve() returns None and the action is a no-op.

Thread safety: single-process asyncio; no locking needed.
"""
import time
from typing import Any, Dict, Optional, Tuple

# chat_id -> (action_id, expires_at, action_args)
_pending: Dict[int, Tuple[str, float, Any]] = {}

CONFIRM_TTL = 30  # seconds


def register(chat_id: int, action_id: str, args: Any = None) -> None:
    """
    Register a pending confirmation for chat_id.

    Overwrites any existing pending confirm for that chat.
    action_id identifies which action is being confirmed (e.g. "pause", "resume").
    args is any extra data needed to execute the action.
    """
    expires_at = time.monotonic() + CONFIRM_TTL
    _pending[chat_id] = (action_id, expires_at, args)


def resolve(chat_id: int, action_id: str) -> Optional[Any]:
    """
    Attempt to resolve a pending confirmation.

    Returns args if chat_id has a live pending confirm for action_id.
    Returns None if:
      - no pending confirm for chat_id
      - action_id does not match
      - confirmation has expired

    Always removes the pending entry on resolution (successful or expired).
    """
    entry = _pending.pop(chat_id, None)
    if entry is None:
        return None

    stored_action_id, expires_at, args = entry

    if stored_action_id != action_id:
        # Different action — put the original back and reject.
        # (Edge case: user typed /pause, bot asked for confirm, user typed /resume
        # confirm instead — treat as mismatch.)
        _pending[chat_id] = (stored_action_id, expires_at, args)
        return None

    if time.monotonic() > expires_at:
        return None  # Expired — already popped above.

    return args


def peek(chat_id: int) -> Optional[Tuple[str, float, Any]]:
    """
    Return the pending entry for chat_id without consuming it, or None.
    Used by the 'yes' text handler to find which action to confirm.
    """
    entry = _pending.get(chat_id)
    if entry is None:
        return None
    _, expires_at, _ = entry
    if time.monotonic() > expires_at:
        _pending.pop(chat_id, None)
        return None
    return entry


def expire_old() -> int:
    """
    Remove all expired entries. Returns count removed.
    Call periodically to avoid unbounded growth (rare in practice — 30s TTL
    means entries self-expire on next resolve, but this keeps memory clean).
    """
    now = time.monotonic()
    to_delete = [
        chat_id
        for chat_id, (_, expires_at, _) in _pending.items()
        if now > expires_at
    ]
    for chat_id in to_delete:
        _pending.pop(chat_id, None)
    return len(to_delete)


def pending_action_id(chat_id: int) -> Optional[str]:
    """Return the action_id for the live pending confirm, or None."""
    entry = peek(chat_id)
    if entry is None:
        return None
    return entry[0]
