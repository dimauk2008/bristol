import logging

from fastapi import WebSocket

log = logging.getLogger("avito.ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    @property
    def count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        log.info("ws подключён (клиентов: %s)", self.count)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._clients:
            self._clients.discard(websocket)
            log.info("ws отключён (клиентов: %s)", self.count)

    async def broadcast(self, message: dict) -> None:
        if not self._clients:
            return
        dead: list[WebSocket] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(message)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self._clients.discard(websocket)


manager = ConnectionManager()
