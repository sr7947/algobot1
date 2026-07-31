"""
WebSocket manager for real-time dashboard updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter()


class WebSocketManager:
    """
    Manages WebSocket connections for real-time dashboard updates.
    Broadcasts events to all connected clients.
    """

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and track a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._connections.append(websocket)
        logger.info(f"WebSocket connected. Total: {len(self._connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket."""
        async with self._lock:
            if websocket in self._connections:
                self._connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Total: {len(self._connections)}")

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Broadcast a message to all connected clients."""
        if not self._connections:
            return

        message = json.dumps(
            {
                "event": event_type,
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            default=str,
        )

        dead: list[WebSocket] = []
        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)

            for ws in dead:
                self._connections.remove(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# Global singleton
ws_manager = WebSocketManager()


@router.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.

    Events sent to clients:
    - market_update: Live price updates
    - new_signal: New trade signal generated
    - order_update: Order status change
    - position_update: Position P&L update
    - alert: SL hit, target hit, kill switch
    - system: System status changes
    """
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; listen for client messages
            data = await websocket.receive_text()
            # Handle ping/pong or client commands
            if data == "ping":
                await websocket.send_text(json.dumps({"event": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}))
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await ws_manager.disconnect(websocket)
