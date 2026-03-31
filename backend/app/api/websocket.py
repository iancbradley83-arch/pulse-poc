"""WebSocket endpoint for live feed updates."""

from fastapi import WebSocket, WebSocketDisconnect
from app.services.feed_manager import FeedManager


async def feed_websocket(websocket: WebSocket, feed: FeedManager):
    await websocket.accept()
    feed.register_ws(websocket)
    try:
        while True:
            # Keep connection alive, listen for client messages
            data = await websocket.receive_text()
            # Client can send filter preferences, etc.
    except WebSocketDisconnect:
        feed.unregister_ws(websocket)
