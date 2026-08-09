from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


app = FastAPI(title="ESP32 Live Display")


@dataclass(frozen=True)
class DisplayMessage:
    type: str   
    text: str
    color: str = "white"
    size: int = 3

    @classmethod
    def from_raw(cls, raw_message: str) -> "DisplayMessage":
        try:
            payload: Any = json.loads(raw_message)
        except json.JSONDecodeError:
            return cls(type="display", text=raw_message.strip())

        if isinstance(payload, dict):
            return cls(
                type=str(payload.get("type", "display")),
                text=str(payload.get("text", "")).strip(),
                color=str(payload.get("color", "white")),
                size=int(payload.get("size", 3)),
            )

        return cls(type="display", text=str(payload).strip())

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "text": self.text,
                "color": self.color,
                "size": self.size,
            },
            ensure_ascii=False,
        )


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)

    async def broadcast(self, message: str, sender: WebSocket) -> None:
        disconnected_clients: list[WebSocket] = []

        for connection in self.active_connections:
            if connection is sender:
                continue

            try:
                await connection.send_text(message)
            except RuntimeError:
                disconnected_clients.append(connection)

        for connection in disconnected_clients:
            self.disconnect(connection)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)

    try:
        while True:
            raw_message = await websocket.receive_text()
            display_message = DisplayMessage.from_raw(raw_message)

            # NOTE: previously this only broadcast when text was non-empty.
            # Clearing the shopping list sends text="" (falsy in Python),
            # so that event was silently dropped and never reached the
            # ESP32 (or any other connected client). Broadcasting
            # unconditionally lets an empty text act as a real "clear"
            # signal downstream.
            await manager.broadcast(display_message.to_json(), sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close(code=1011)
