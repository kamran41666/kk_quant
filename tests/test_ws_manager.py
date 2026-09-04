import asyncio

from server.ws.manager import ConnectionManager


class _DisconnectingSocket:
    def __init__(self, manager: ConnectionManager, channel: str):
        self.manager = manager
        self.channel = channel

    async def accept(self):
        return None

    async def send_json(self, message):
        self.manager.disconnect(self, self.channel)


def test_broadcast_uses_connection_snapshot_when_socket_disconnects():
    async def run():
        manager = ConnectionManager()
        socket = _DisconnectingSocket(manager, "paper:test")
        await manager.connect(socket, "paper:test")
        await manager.broadcast("paper:test", {"type": "ping"})
        assert manager.total_connections == 0

    asyncio.run(run())
