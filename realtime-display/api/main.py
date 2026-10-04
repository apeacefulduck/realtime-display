from __future__ import annotations

import json
import asyncio
import hashlib
import time
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from spotify_session import seal, unseal, make_session

app = FastAPI(title="ESP32 Live Display")

# --- CORS Ayarı ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://realtime-display-lime.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --------------------------------

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SPOTIFY_SCOPES = "user-read-currently-playing user-read-playback-state"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

FRONTEND_ORIGIN = "https://realtime-display-lime.vercel.app"
spotify_access_cache: dict[str, dict[str, Any]] = {}
WEATHER_CACHE_TTL_SECONDS = 15 * 60

weather_cache: dict[str, Any] = {
    "data": None,
    "updated_at": 0.0,
}

def weather_label(code: int) -> tuple[str, str]:
    if code == 0:
        return "clear", "Açık"
    if code in {1, 2}:
        return "partly_cloudy", "Parçalı bulutlu"
    if code == 3:
        return "cloudy", "Bulutlu"
    if code in {45, 48}:
        return "fog", "Sisli"
    if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
        return "rain", "Yağmurlu"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow", "Karlı"
    if code in {95, 96, 99}:
        return "storm", "Fırtına"
    return "unknown", "Bilinmiyor"


def short_day(date_text: str) -> str:
    # YYYY-MM-DD -> MM/DD keeps the ESP payload compact and language-neutral.
    return f"{date_text[5:7]}/{date_text[8:10]}"


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


async def spotify_get(url: str, session: str) -> tuple[httpx.Response, str]:
    tokens = unseal(session, "session")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise HTTPException(401, "Spotify account is not connected.")
    cache_key = hashlib.sha256(refresh_token.encode()).hexdigest()
    tokens = spotify_access_cache.get(cache_key, tokens)

    async with httpx.AsyncClient(timeout=15) as client:
        if tokens.get("expires_at", 0) <= time.time() + 30:
            response = await client.post(SPOTIFY_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=spotify_token_auth())
            if response.status_code in (400, 401):
                raise HTTPException(401, "Spotify session expired. Connect Spotify again.")
            if response.status_code >= 400:
                raise HTTPException(502, "Spotify token service unavailable.")
            refreshed = response.json()
            tokens = {"purpose": "session", "access_token": refreshed["access_token"],
                "refresh_token": refreshed.get("refresh_token", refresh_token),
                "expires_at": time.time() + refreshed.get("expires_in", 3600)}
            if len(spotify_access_cache) >= 100:
                spotify_access_cache.clear()
            spotify_access_cache[cache_key] = tokens
        response = await client.get(url, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        if response.status_code == 401:
            spotify_access_cache.pop(cache_key, None)
            tokens["expires_at"] = 0
            # One refresh attempt; never recurse on a rejected replacement token.
            refreshed = await client.post(SPOTIFY_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
                auth=spotify_token_auth())
            if refreshed.status_code >= 400:
                raise HTTPException(401, "Spotify session expired. Connect Spotify again.")
            payload = refreshed.json()
            tokens.update(access_token=payload["access_token"],
                refresh_token=payload.get("refresh_token", tokens["refresh_token"]),
                expires_at=time.time() + payload.get("expires_in", 3600))
            spotify_access_cache[cache_key] = tokens
            response = await client.get(url, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    return response, seal(tokens)


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
        self.roles: dict[WebSocket, str] = {}
        self.latest: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.active_connections.add(websocket)
            self.roles[websocket] = websocket.query_params.get("role", "unknown")
            for message in self.latest.values():
                await websocket.send_text(message)
            await self.send_status()

    def disconnect(self, websocket: WebSocket) -> None:
        self.active_connections.discard(websocket)
        self.roles.pop(websocket, None)

    async def send_status(self) -> None:
        status = json.dumps({"type": "status", "devices": sum(role == "device" for role in self.roles.values())})
        for connection in tuple(self.active_connections):
            if self.roles.get(connection) != "browser":
                continue
            try:
                await connection.send_text(status)
            except Exception:
                self.disconnect(connection)

    async def broadcast(self, message: str, sender: WebSocket) -> None:
        async with self.lock:
            payload = json.loads(message)
            kind = payload.get("type") if isinstance(payload, dict) else None
            if kind in {"display", "weather", "spotify"}:
                self.latest[kind] = message
            for connection in tuple(self.active_connections):
                if connection is sender:
                    continue
                try:
                    await connection.send_text(message)
                except Exception:
                    self.disconnect(connection)


manager = ConnectionManager()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "2026-10-04-data-recovery", "worker": os.getpid(),
        "clients": len(manager.active_connections),
        "devices": sum(role == "device" for role in manager.roles.values()),
        "streams": list(manager.latest)}


@app.get("/spotify/login")
async def spotify_login() -> RedirectResponse:
    client_id, _, redirect_uri = spotify_config()
    state = seal({"purpose": "oauth-state", "nonce": secrets.token_urlsafe(24)})
    query = urlencode({"client_id": client_id, "response_type": "code",
        "redirect_uri": redirect_uri, "scope": SPOTIFY_SCOPES, "state": state})
    response = RedirectResponse(f"{SPOTIFY_AUTH_URL}?{query}")
    response.set_cookie("homeflow_spotify_state", state, max_age=600,
        httponly=True, secure=True, samesite="lax", path="/spotify")
    return response


@app.get("/spotify/callback")
async def spotify_callback(request: Request) -> RedirectResponse:
    if request.query_params.get("error"):
        return RedirectResponse(FRONTEND_ORIGIN + "/#spotify_error=authorization_denied")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    cookie_state = request.cookies.get("homeflow_spotify_state", "")
    if not code or not state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(400, "Invalid Spotify callback.")
    unseal(state, "oauth-state", ttl=600)
    _, _, redirect_uri = spotify_config()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(SPOTIFY_TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            auth=spotify_token_auth())
    if response.status_code >= 400:
        raise HTTPException(400, "Spotify authorization failed.")
    session = make_session(response.json())
    # A URL fragment is never sent to Vercel or included in HTTP request logs.
    result = RedirectResponse(FRONTEND_ORIGIN + "/#spotify_session=" + session,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    result.delete_cookie("homeflow_spotify_state", path="/spotify", secure=True, samesite="lax")
    return result


@app.get("/spotify/current")
async def spotify_current(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Spotify account is not connected.")
    try:
        response, session = await spotify_get(SPOTIFY_NOW_PLAYING_URL, authorization[7:])
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Spotify service unavailable.") from exc
    if response.status_code == 204:
        return {"connected": True, "playing": False, "session_token": session}
    if response.status_code >= 400:
        raise HTTPException(response.status_code, "Spotify request failed.")
    payload = response.json()
    item = payload.get("item") or {}
    artists = ", ".join(artist.get("name", "") for artist in item.get("artists", [])).strip(", ")
    return {"connected": True, "playing": bool(payload.get("is_playing")),
        "title": item.get("name", ""), "artist": artists,
        "album": (item.get("album") or {}).get("name", ""),
        "progress_ms": payload.get("progress_ms", 0), "duration_ms": item.get("duration_ms", 0),
        "session_token": session}


@app.get("/weather")
async def weather(lat: float = 41.0082, lon: float = 28.9784) -> dict[str, Any]:
    import time

    now = time.time()

    # Cache'de güncel veri varsa dış API'ye tekrar gitme.
    if (
        weather_cache["data"] is not None
        and now - weather_cache["updated_at"] < WEATHER_CACHE_TTL_SECONDS
    ):
        return weather_cache["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 7,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                OPEN_METEO_FORECAST_URL,
                params=params,
            )

        response.raise_for_status()

    except httpx.HTTPStatusError as exc:
        # Open-Meteo rate limit veya geçici hata verirse
        # elimizde eski veri varsa onu kullan.
        if weather_cache["data"] is not None:
            return weather_cache["data"]

        raise HTTPException(
            status_code=502,
            detail="Weather provider unavailable.",
        ) from exc

    except httpx.HTTPError as exc:
        if weather_cache["data"] is not None:
            return weather_cache["data"]

        raise HTTPException(
            status_code=502,
            detail="Weather provider unavailable.",
        ) from exc

    payload = response.json()
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    condition, label = weather_label(
        int(current.get("weather_code", -1))
    )

    forecast: list[dict[str, Any]] = []

    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    max_temps = daily.get("temperature_2m_max", [])
    min_temps = daily.get("temperature_2m_min", [])
    rain_probs = daily.get("precipitation_probability_max", [])

    for index, date_text in enumerate(dates[:7]):
        day_code = (
            int(codes[index])
            if index < len(codes)
            else -1
        )

        day_condition, day_label = weather_label(day_code)

        forecast.append(
            {
                "day": short_day(str(date_text)),
                "condition": day_condition,
                "label": day_label,
                "code": day_code,
                "max": (
                    round(float(max_temps[index]))
                    if index < len(max_temps)
                    else 0
                ),
                "min": (
                    round(float(min_temps[index]))
                    if index < len(min_temps)
                    else 0
                ),
                "rain": (
                    int(rain_probs[index])
                    if (
                        index < len(rain_probs)
                        and rain_probs[index] is not None
                    )
                    else 0
                ),
            }
        )

    result = {
        "type": "weather",
        "location": "Istanbul",
        "temperature": round(
            float(current.get("temperature_2m", 0))
        ),
        "humidity": int(
            current.get("relative_humidity_2m", 0)
        ),
        "wind": round(
            float(current.get("wind_speed_10m", 0))
        ),
        "condition": condition,
        "label": label,
        "forecast": forecast,
    }

    # Başarılı sonucu cache'e koy.
    weather_cache["data"] = result
    weather_cache["updated_at"] = now

    return result


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    try:
        await manager.connect(websocket)
        while True:
            raw_message = await websocket.receive_text()

            # NOTE: previously this only broadcast when text was non-empty.
            # Clearing the shopping list sends text="" (falsy in Python),
            # so that event was silently dropped and never reached the
            # ESP32 (or any other connected client). Broadcasting
            # unconditionally lets an empty text act as a real "clear"
            # signal downstream.
            message = normalize_websocket_message(raw_message)
            payload = json.loads(message)
            if isinstance(payload, dict) and payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await manager.broadcast(message, sender=websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        await websocket.close(code=1011)

    finally:
        manager.disconnect(websocket)
        async with manager.lock:
            await manager.send_status()
