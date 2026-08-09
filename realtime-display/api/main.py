from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware  # <-- Buraya ekle
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="ESP32 Live Display")

# --- CORS Ayarı ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# --------------------------------

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"

app = FastAPI(title="ESP32 Live Display")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-playback-state"

spotify_state: str | None = None
spotify_tokens: dict[str, Any] = {}


def spotify_config() -> tuple[str, str, str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail=(
                "Spotify is not configured. Set SPOTIFY_CLIENT_ID, "
                "SPOTIFY_CLIENT_SECRET, and SPOTIFY_REDIRECT_URI."
            ),
        )

    return client_id, client_secret, redirect_uri


def spotify_token_auth() -> tuple[str, str]:
    client_id, client_secret, _ = spotify_config()
    return client_id, client_secret


async def refresh_spotify_token() -> None:
    refresh_token = spotify_tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Spotify account is not connected.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=spotify_token_auth(),
        )

    if response.status_code >= 400:
        spotify_tokens.clear()
        raise HTTPException(status_code=401, detail="Spotify token refresh failed.")

    payload = response.json()
    spotify_tokens["access_token"] = payload["access_token"]
    if payload.get("refresh_token"):
        spotify_tokens["refresh_token"] = payload["refresh_token"]


async def spotify_get(url: str) -> httpx.Response:
    access_token = spotify_tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="Spotify account is not connected.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if response.status_code == 401:
        await refresh_spotify_token()
        access_token = spotify_tokens["access_token"]
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    return response


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


def normalize_websocket_message(raw_message: str) -> str:
    try:
        payload: Any = json.loads(raw_message)
    except json.JSONDecodeError:
        return DisplayMessage(type="display", text=raw_message.strip()).to_json()

    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    return DisplayMessage(type="display", text=str(payload).strip()).to_json()


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


@app.get("/spotify/login")
async def spotify_login() -> RedirectResponse:
    global spotify_state

    client_id, _, redirect_uri = spotify_config()
    spotify_state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": spotify_state,
        }
    )
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}")


@app.get("/spotify/callback")
async def spotify_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    _, _, redirect_uri = spotify_config()

    if not code or state != spotify_state:
        raise HTTPException(status_code=400, detail="Invalid Spotify callback.")

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=spotify_token_auth(),
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=400, detail="Spotify authorization failed.")

    payload = response.json()
    spotify_tokens["access_token"] = payload["access_token"]
    spotify_tokens["refresh_token"] = payload["refresh_token"]

    return HTMLResponse(
        "<!doctype html><title>Spotify connected</title>"
        "<p>Spotify baglandi. Bu pencereyi kapatip HomeFlow'a donebilirsin.</p>"
    )


@app.get("/spotify/current")
async def spotify_current() -> dict[str, Any]:
    response = await spotify_get(SPOTIFY_NOW_PLAYING_URL)

    if response.status_code == 204:
        return {"connected": True, "playing": False}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail="Spotify request failed.")

    payload = response.json()
    item = payload.get("item") or {}
    artists = ", ".join(artist.get("name", "") for artist in item.get("artists", [])).strip(", ")

    return {
        "connected": True,
        "playing": bool(payload.get("is_playing")),
        "title": item.get("name", ""),
        "artist": artists,
        "album": (item.get("album") or {}).get("name", ""),
        "progress_ms": payload.get("progress_ms", 0),
        "duration_ms": item.get("duration_ms", 0),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)

    try:
        while True:
            raw_message = await websocket.receive_text()

            # NOTE: previously this only broadcast when text was non-empty.
            # Clearing the shopping list sends text="" (falsy in Python),
            # so that event was silently dropped and never reached the
            # ESP32 (or any other connected client). Broadcasting
            # unconditionally lets an empty text act as a real "clear"
            # signal downstream.
            await manager.broadcast(normalize_websocket_message(raw_message), sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close(code=1011)
