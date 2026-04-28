"""
Auth middleware for aiogram v3.

Rejects any update whose effective chat ID is not in the allowlist.
Rejected senders receive one message and the chat ID is logged so Ian
can grep Railway logs and add it to OPS_BOT_ALLOWED_CHAT_IDS.
"""
import logging
from typing import Any, Awaitable, Callable, Dict, List

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)


class AllowlistMiddleware(BaseMiddleware):
    """Only allow updates from chat IDs in the configured allowlist."""

    def __init__(self, allowed_chat_ids: List[int]) -> None:
        self._allowed = set(allowed_chat_ids)
        super().__init__()

    def _effective_chat_id(self, event: TelegramObject) -> int | None:
        """Extract the chat ID from whatever update type we received."""
        if isinstance(event, Message):
            return event.chat.id
        # For other update types, try common attribute paths.
        for attr in ("chat", "message"):
            obj = getattr(event, attr, None)
            if obj is not None:
                cid = getattr(obj, "id", None)
                if cid is not None:
                    return cid
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        chat_id = self._effective_chat_id(event)

        if chat_id is None:
            # Cannot determine chat; let it pass (e.g. channel post with no chat ref).
            return await handler(event, data)

        if chat_id in self._allowed:
            return await handler(event, data)

        # Unauthorised — log the chat ID so it can be allowlisted.
        logger.info("unauthorised chat: %s", chat_id)

        # Reply if this is a message we can respond to.
        if isinstance(event, Message):
            try:
                await event.answer("not authorised. your chat id has been logged.")
            except Exception:
                pass  # Best-effort; never crash on rejection path.

        return None
