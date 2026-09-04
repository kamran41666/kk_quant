"""WebSocket connection manager with channel-based broadcasting"""
from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    """Manages WebSocket connections grouped by channel.

    Channels: 'dashboard', 'backtest:{run_id}'
    """

    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, channel: str):
        """Accept a new WebSocket connection on a channel"""
        await websocket.accept()
        if channel not in self._connections:
            self._connections[channel] = set()
        self._connections[channel].add(websocket)

    def disconnect(self, websocket: WebSocket, channel: str):
        """Remove a disconnected WebSocket"""
        if channel in self._connections:
            self._connections[channel].discard(websocket)
            if not self._connections[channel]:
                del self._connections[channel]

    async def broadcast(self, channel: str, message: dict):
        """Send a JSON message to ALL clients on a channel"""
        if channel not in self._connections:
            return
        dead = set()
        # Snapshot the set before awaiting sends. A disconnect can mutate the
        # live set while an individual socket is yielding.
        for ws in tuple(self._connections.get(channel, ())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        # Clean up dead connections
        current = self._connections.get(channel)
        if current is not None:
            for ws in dead:
                current.discard(ws)
            if not current:
                self._connections.pop(channel, None)

    async def broadcast_to_all(self, message: dict):
        """Send to all connected clients on all channels"""
        for channel in list(self._connections.keys()):
            await self.broadcast(channel, message)

    @property
    def active_channels(self) -> list[str]:
        return list(self._connections.keys())

    @property
    def total_connections(self) -> int:
        return sum(len(v) for v in self._connections.values())


# Global singleton
manager = ConnectionManager()
